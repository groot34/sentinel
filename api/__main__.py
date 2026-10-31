"""Sentinel 2.0 API server entry point.

Starts the Starlette/Uvicorn server for the Sentinel investigation API.

Usage:
    python -m api
    python -m api --host 127.0.0.1 --port 8000 --log-level info
    python -m api --incidents-root /path/to/incidents

Environment variables (read after loading .env):
    LOG_LEVEL                      Application log level for the API and the
                                   event worker (DEBUG/INFO/WARNING/ERROR/
                                   CRITICAL; default: INFO)
    INCIDENTS_DIR                  Incident-bundles directory (default:
                                   'incidents'); overridden by --incidents-root
    SENTINEL_PERSISTENCE_BACKEND   'filesystem' (default) or 'postgres'
    SENTINEL_PERSISTENCE_ROOT      Root dir for filesystem backend
                                   (default: .sentinel_persistence)
    SENTINEL_DATABASE_URL          PostgreSQL connection URL (postgres backend only)
    GROQ_API_KEY                   Required when investigations are submitted
    GROQ_MODEL                     LLM model override (default: openai/gpt-oss-120b)

Incident-bundle ingestion is DISABLED unless explicitly enabled:
    SENTINEL_ALLOW_INCIDENT_INGESTION   '1'/'true'/'yes'/'on' enables POST /incidents
    SENTINEL_INGEST_MAX_FILES           Max files per submission (default: 200)
    SENTINEL_INGEST_MAX_FILE_BYTES      Max UTF-8 bytes per file (default: 2097152)
    SENTINEL_INGEST_MAX_TOTAL_BYTES     Max total UTF-8 bytes (default: 20971520)
    SENTINEL_INGEST_MAX_REQUEST_BYTES   Max request body bytes (default: 25165824)

API-key authentication is DISABLED unless explicitly enabled:
    SENTINEL_API_AUTH_ENABLED           '1'/'true'/'yes'/'on' enables the guard
    SENTINEL_API_KEY                    Required (non-empty) when the guard is enabled
    SENTINEL_API_AUTH_PROTECT_READS     '1'/'true'/'yes'/'on' also protects GET/HEAD/OPTIONS
                                        (/health stays public)

When the guard is enabled, every method other than GET/HEAD/OPTIONS must send
``Authorization: Bearer <SENTINEL_API_KEY>``.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

#: Environment variable holding the incident-bundles directory.
ENV_INCIDENTS_DIR = "INCIDENTS_DIR"


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m api",
        description="Start the Sentinel 2.0 API server.",
    )
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="Bind address (default: 127.0.0.1 — localhost only).",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=8000,
        help="Listen port (default: 8000).",
    )
    parser.add_argument(
        "--log-level",
        default="info",
        choices=["debug", "info", "warning", "error"],
        dest="log_level",
        help="Uvicorn log level (default: info).",
    )
    parser.add_argument(
        "--incidents-root",
        default=None,
        dest="incidents_root",
        help=(
            "Path to the incident bundles directory. Overrides the "
            "INCIDENTS_DIR environment variable "
            "(default: 'incidents' relative to the working directory)."
        ),
    )
    return parser


def main() -> None:
    """Parse arguments, load configuration, and start the Uvicorn server."""
    parser = _build_parser()
    args = parser.parse_args()

    # Load .env before any environment variable is read.
    # python-dotenv does not override variables already set in the shell.
    # This must happen before get_repository() reads SENTINEL_PERSISTENCE_BACKEND.
    from dotenv import load_dotenv
    load_dotenv()

    # Configure application logging from LOG_LEVEL. The same helper is used by
    # the event worker, so both processes honour one setting. Only Sentinel's own
    # loggers are levelled; uvicorn keeps its own --log-level.
    from core.logging_config import configure_logging
    configure_logging()

    # Select persistence backend from environment (populated above from .env if present).
    # SENTINEL_PERSISTENCE_BACKEND: 'filesystem' (default) or 'postgres'
    # SENTINEL_PERSISTENCE_ROOT:   filesystem root dir (default: .sentinel_persistence)
    # SENTINEL_DATABASE_URL:       postgres connection string (postgres backend only)
    from core.persistence.factory import get_repository
    from core.persistence.repository import PersistenceError

    try:
        repository = get_repository()
    except PersistenceError as exc:
        print(f"ERROR: Failed to initialise persistence backend: {exc}", file=sys.stderr)
        sys.exit(1)

    # Resolve incidents root. Precedence: --incidents-root, then INCIDENTS_DIR,
    # then None so create_app() keeps its own Path("incidents") default. That
    # default matches the Compose volume mounted at /app/incidents.
    incidents_root_value = args.incidents_root
    if incidents_root_value is None:
        raw_incidents_dir = os.getenv(ENV_INCIDENTS_DIR)
        incidents_root_value = raw_incidents_dir.strip() if raw_incidents_dir else None
        incidents_root_value = incidents_root_value or None
    incidents_root: Path | None = (
        Path(incidents_root_value) if incidents_root_value is not None else None
    )

    # Construct the Starlette application.
    # llm_client is intentionally omitted — the orchestrator creates its own client
    # lazily via get_llm_client() when an investigation is submitted.
    from api.app import create_app
    from api.auth import AuthConfigurationError

    try:
        app = create_app(repository=repository, incidents_root=incidents_root)
    except AuthConfigurationError as exc:
        # Fail closed: never start with authentication enabled but unusable.
        # The message is authored by the auth config loader and never contains
        # the configured key value.
        print(f"ERROR: Invalid authentication configuration: {exc}", file=sys.stderr)
        sys.exit(1)

    # Start Uvicorn.
    import uvicorn

    uvicorn.run(
        app,
        host=args.host,
        port=args.port,
        log_level=args.log_level,
    )


if __name__ == "__main__":
    main()
