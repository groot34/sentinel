"""Application factory for Sentinel 2.0 API Layer."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from pathlib import Path
from typing import AsyncIterator, Optional

from starlette.applications import Starlette
from starlette.exceptions import HTTPException
from starlette.middleware import Middleware
from starlette.routing import Route

from api.auth import ApiKeyAuthMiddleware, AuthConfig, load_auth_config
from api.errors import (
    concurrency_exception_handler,
    generic_exception_handler,
    http_exception_handler,
    invalid_bundle_exception_handler,
    persistence_exception_handler,
)
from api.routes import (
    connect_repository,
    create_incident,
    create_investigation,
    create_readiness_runtime,
    disconnect_repository,
    get_approval_snapshot,
    get_connection,
    get_investigation,
    get_investigation_result,
    get_repository_commit,
    health,
    list_connections,
    list_investigation_events,
    list_investigations,
    list_repository_branches,
    list_repository_pull_requests,
    readiness,
    resume_investigation,
    shutdown_readiness_executor,
    submit_approval,
    verify_connection,
)
from agents.bundle_ingest import (
    IngestionLimits,
    ingestion_enabled as env_ingestion_enabled,
    load_ingestion_limits,
)
from agents.bundle_validator import InvalidIncidentBundleError
from core.integrations.filesystem_store import FilesystemIntegrationStore
from core.integrations.github_client import GitHubClient
from core.integrations.repository_store import IntegrationStore
from core.llm.base import BaseLLMProvider
from core.persistence import (
    ConcurrencyError,
    FilesystemRepository,
    PersistenceError,
    PersistenceRepository,
)
from core.persistence.factory import DEFAULT_PERSISTENCE_ROOT

logger = logging.getLogger(__name__)


async def _shutdown_application(app: Starlette) -> None:
    """Release every resource owned by the application. Single-shot.

    Runs in the lifespan's ``finally`` block, so teardown still happens when
    startup or request handling has failed. The call is idempotent: a second
    shutdown (for example a duplicate signal handler) must not close the
    repository or integration pool or the readiness executor twice.

    Closing a connection pool is blocking — it waits for the pool's worker
    threads, bounded by the pool's own close timeout — so it is off-loaded to a
    worker thread rather than stalling the event loop. Each resource is released
    independently: a failure in one is logged and must not abort the others.
    """
    if getattr(app.state, "shutdown_complete", False):
        return
    app.state.shutdown_complete = True

    repository = getattr(app.state, "repository", None)
    runtime = getattr(app.state, "readiness_runtime", None)
    integration_store = getattr(app.state, "integration_store", None)
    github_client = getattr(app.state, "github_client", None)

    if repository is not None:
        try:
            # PersistenceRepository.close() is a no-op for backends without a
            # pool (filesystem, in-memory) and closes the pool for PostgreSQL.
            await asyncio.to_thread(repository.close)
        except Exception as exc:  # noqa: BLE001 - teardown must not raise
            logger.warning("Repository close failed (%s)", type(exc).__name__)

    if integration_store is not None:
        try:
            # IntegrationStore.close() is a no-op for the filesystem backend and
            # closes the pool for PostgreSQL.
            await asyncio.to_thread(integration_store.close)
        except Exception as exc:  # noqa: BLE001 - teardown must not raise
            logger.warning("Integration store close failed (%s)", type(exc).__name__)

    if github_client is not None:
        try:
            # Releases the read-only GitHub client's HTTP connection pool.
            await asyncio.to_thread(github_client.close)
        except Exception as exc:  # noqa: BLE001 - teardown must not raise
            logger.warning("GitHub client close failed (%s)", type(exc).__name__)

    if runtime is not None:
        shutdown_readiness_executor(runtime.executor)


def create_app(
    repository: Optional[PersistenceRepository] = None,
    incidents_root: Optional[Path | str] = None,
    llm_client: Optional[BaseLLMProvider] = None,
    ingestion_enabled: Optional[bool] = None,
    ingestion_limits: Optional[IngestionLimits] = None,
    auth_enabled: Optional[bool] = None,
    api_key: Optional[str] = None,
    auth_protect_reads: Optional[bool] = None,
    integration_store: Optional[IntegrationStore] = None,
    github_client: Optional[GitHubClient] = None,
) -> Starlette:
    """Create and configure a Starlette API instance.

    Args:
        repository: Persistence repository instance (defaults to FilesystemRepository).
        incidents_root: Root directory where incident bundles live (defaults to 'incidents').
        llm_client: Optional LLM provider instance.
        ingestion_enabled: Opt-in flag for POST /incidents. When None the flag is
            read from SENTINEL_ALLOW_INCIDENT_INGESTION (disabled by default).
        ingestion_limits: Optional resource limits for bundle ingestion
            (defaults to the environment configuration).
        auth_enabled: Enable the API-key guard. When None the flag is read from
            SENTINEL_API_AUTH_ENABLED (disabled by default).
        api_key: Static API key required when auth is enabled. When None the key
            is read from SENTINEL_API_KEY.
        auth_protect_reads: Also protect safe methods (GET/HEAD/OPTIONS) when
            True. When None the flag is read from SENTINEL_API_AUTH_PROTECT_READS
            (disabled by default). /health and /ready stay public for safe
            methods only.
        integration_store: Persistence store for GitHub connections. When None a
            filesystem store rooted at the default persistence directory is used
            (the PostgreSQL backend is selected by the entry point via the
            integration-store factory). Ownership is transferred to the
            application: the injected store (or the default) is closed on
            shutdown, so the caller must not keep using or re-close it.
        github_client: Read-only GitHub client used by the integration routes.
            When None the GitHub endpoints report that the integration is not
            configured (503) — an unconfigured GitHub integration never blocks
            startup, and the token is always resolved server-side. Ownership is
            transferred to the application: an injected client is closed on
            shutdown.

    Returns:
        Configured Starlette application.

    Raises:
        AuthConfigurationError: When authentication is enabled without a usable
            key (fails closed rather than serving unprotected requests).
    """
    # Resolve (and validate) authentication config before building the app so a
    # misconfiguration fails closed at construction time.
    auth_config: AuthConfig = load_auth_config(
        enabled=auth_enabled,
        api_key=api_key,
        protect_reads=auth_protect_reads,
    )

    routes = [
        Route("/health", endpoint=health, methods=["GET"]),
        Route("/ready", endpoint=readiness, methods=["GET"]),
        Route("/incidents", endpoint=create_incident, methods=["POST"]),
        Route("/investigations", endpoint=create_investigation, methods=["POST"]),
        Route("/investigations", endpoint=list_investigations, methods=["GET"]),
        Route("/investigations/{investigation_id}", endpoint=get_investigation, methods=["GET"]),
        Route("/investigations/{investigation_id}/result", endpoint=get_investigation_result, methods=["GET"]),
        Route("/investigations/{investigation_id}/events", endpoint=list_investigation_events, methods=["GET"]),
        Route("/investigations/{investigation_id}/resume", endpoint=resume_investigation, methods=["POST"]),
        Route("/investigations/{investigation_id}/approval", endpoint=submit_approval, methods=["POST"]),
        Route(
            "/investigations/{investigation_id}/approval/{proposal_id}/snapshot",
            endpoint=get_approval_snapshot,
            methods=["GET"],
        ),
        # GitHub integration (read-only). Non-safe methods (POST/DELETE) are
        # protected by the API-key guard exactly like the other mutating routes.
        Route(
            "/integrations/github/connections",
            endpoint=connect_repository,
            methods=["POST"],
        ),
        Route(
            "/integrations/github/connections",
            endpoint=list_connections,
            methods=["GET"],
        ),
        Route(
            "/integrations/github/connections/{connection_id}",
            endpoint=get_connection,
            methods=["GET"],
        ),
        Route(
            "/integrations/github/connections/{connection_id}",
            endpoint=disconnect_repository,
            methods=["DELETE"],
        ),
        Route(
            "/integrations/github/connections/{connection_id}/verify",
            endpoint=verify_connection,
            methods=["POST"],
        ),
        Route(
            "/integrations/github/connections/{connection_id}/branches",
            endpoint=list_repository_branches,
            methods=["GET"],
        ),
        Route(
            "/integrations/github/connections/{connection_id}/commits/{ref}",
            endpoint=get_repository_commit,
            methods=["GET"],
        ),
        # Collection form taking the (possibly slash-containing) ref as a query
        # parameter: /commits?ref=feature/add-auth. Starlette matches the
        # decoded ASGI path, so an encoded slash inside the path segment above
        # cannot reach the handler; this route exists for refs containing '/'.
        Route(
            "/integrations/github/connections/{connection_id}/commits",
            endpoint=get_repository_commit,
            methods=["GET"],
        ),
        Route(
            "/integrations/github/connections/{connection_id}/pull-requests",
            endpoint=list_repository_pull_requests,
            methods=["GET"],
        ),
    ]

    exception_handlers = {
        HTTPException: http_exception_handler,
        ConcurrencyError: concurrency_exception_handler,
        PersistenceError: persistence_exception_handler,
        InvalidIncidentBundleError: invalid_bundle_exception_handler,
        Exception: generic_exception_handler,
    }

    @contextlib.asynccontextmanager
    async def lifespan(app: Starlette) -> AsyncIterator[None]:
        """Own and release the resources belonging to this application instance.

        Startup creates the readiness probe runtime this application owns;
        requests are not served until it completes. Teardown sits in ``finally``
        so resources are still released when startup fails part-way or request
        handling has crashed. The repository is released through
        ``PersistenceRepository.close`` — a no-op for backends without a pool —
        and the readiness executor is shut down without waiting on abandoned
        probes, so shutdown cannot hang the API indefinitely.
        """
        try:
            app.state.readiness_runtime = create_readiness_runtime()
            app.state.shutdown_complete = False
            yield
        finally:
            await _shutdown_application(app)

    app = Starlette(
        debug=False,
        routes=routes,
        exception_handlers=exception_handlers,
        middleware=[Middleware(ApiKeyAuthMiddleware, config=auth_config)],
        lifespan=lifespan,
    )

    # Expose a redacted view: enablement state is introspectable, but the
    # secret is retained only inside the middleware, never on app.state.
    app.state.auth_config = auth_config.redacted()
    # The application owns the repository for the duration of its lifespan: an
    # injected repository (created by the entry point) and the default
    # filesystem repository are both released on shutdown. The default is a
    # no-op, so backends without a pool keep working unchanged.
    app.state.repository = repository if repository is not None else FilesystemRepository()
    app.state.incidents_root = Path(incidents_root) if incidents_root is not None else Path("incidents")
    app.state.llm_client = llm_client
    # GitHub integration state. The store defaults to an inert filesystem store
    # (mirroring the repository default); the PostgreSQL backend is selected by
    # the entry point through the integration-store factory. A missing client
    # leaves the GitHub routes answering 503 without affecting any other route.
    app.state.integration_store = (
        integration_store
        if integration_store is not None
        else FilesystemIntegrationStore(DEFAULT_PERSISTENCE_ROOT)
    )
    app.state.github_client = github_client
    app.state.ingestion_enabled = (
        env_ingestion_enabled() if ingestion_enabled is None else bool(ingestion_enabled)
    )
    app.state.ingestion_limits = (
        ingestion_limits if ingestion_limits is not None else load_ingestion_limits()
    )

    return app
