"""Application factory for Sentinel 2.0 API Layer."""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from starlette.applications import Starlette
from starlette.exceptions import HTTPException
from starlette.routing import Route

from api.errors import (
    concurrency_exception_handler,
    generic_exception_handler,
    http_exception_handler,
    persistence_exception_handler,
)
from api.routes import (
    create_investigation,
    get_investigation,
    health,
    list_investigations,
)
from core.llm.base import BaseLLMProvider
from core.persistence import (
    ConcurrencyError,
    FilesystemRepository,
    PersistenceError,
    PersistenceRepository,
)


def create_app(
    repository: Optional[PersistenceRepository] = None,
    incidents_root: Optional[Path | str] = None,
    llm_client: Optional[BaseLLMProvider] = None,
) -> Starlette:
    """Create and configure a Starlette API instance.

    Args:
        repository: Persistence repository instance (defaults to FilesystemRepository).
        incidents_root: Root directory where incident bundles live (defaults to 'incidents').
        llm_client: Optional LLM provider instance.

    Returns:
        Configured Starlette application.
    """
    routes = [
        Route("/health", endpoint=health, methods=["GET"]),
        Route("/investigations", endpoint=create_investigation, methods=["POST"]),
        Route("/investigations", endpoint=list_investigations, methods=["GET"]),
        Route("/investigations/{investigation_id}", endpoint=get_investigation, methods=["GET"]),
    ]

    exception_handlers = {
        HTTPException: http_exception_handler,
        ConcurrencyError: concurrency_exception_handler,
        PersistenceError: persistence_exception_handler,
        Exception: generic_exception_handler,
    }

    app = Starlette(
        debug=False,
        routes=routes,
        exception_handlers=exception_handlers,
    )

    app.state.repository = repository if repository is not None else FilesystemRepository()
    app.state.incidents_root = Path(incidents_root) if incidents_root is not None else Path("incidents")
    app.state.llm_client = llm_client

    return app
