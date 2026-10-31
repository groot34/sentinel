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
    create_incident,
    create_investigation,
    create_readiness_runtime,
    get_investigation,
    get_investigation_result,
    health,
    list_investigation_events,
    list_investigations,
    readiness,
    resume_investigation,
    shutdown_readiness_executor,
    submit_approval,
)
from agents.bundle_ingest import (
    IngestionLimits,
    ingestion_enabled as env_ingestion_enabled,
    load_ingestion_limits,
)
from agents.bundle_validator import InvalidIncidentBundleError
from core.llm.base import BaseLLMProvider
from core.persistence import (
    ConcurrencyError,
    FilesystemRepository,
    PersistenceError,
    PersistenceRepository,
)

logger = logging.getLogger(__name__)


async def _shutdown_application(app: Starlette) -> None:
    """Release every resource owned by the application. Single-shot.

    Runs in the lifespan's ``finally`` block, so teardown still happens when
    startup or request handling has failed. The call is idempotent: a second
    shutdown (for example a duplicate signal handler) must not close the
    repository pool or the readiness executor twice.

    Closing a connection pool is blocking — it waits for the pool's worker
    threads, bounded by the pool's own close timeout — so it is off-loaded to a
    worker thread rather than stalling the event loop. Each resource is released
    independently: a failure in one is logged and must not abort the other.
    """
    if getattr(app.state, "shutdown_complete", False):
        return
    app.state.shutdown_complete = True

    repository = getattr(app.state, "repository", None)
    runtime = getattr(app.state, "readiness_runtime", None)

    if repository is not None:
        try:
            # PersistenceRepository.close() is a no-op for backends without a
            # pool (filesystem, in-memory) and closes the pool for PostgreSQL.
            await asyncio.to_thread(repository.close)
        except Exception as exc:  # noqa: BLE001 - teardown must not raise
            logger.warning("Repository close failed (%s)", type(exc).__name__)

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
    app.state.ingestion_enabled = (
        env_ingestion_enabled() if ingestion_enabled is None else bool(ingestion_enabled)
    )
    app.state.ingestion_limits = (
        ingestion_limits if ingestion_limits is not None else load_ingestion_limits()
    )

    return app
