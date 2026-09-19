"""Sentinel 2.0 API server entry point.

Starts the Starlette/Uvicorn server for the Sentinel investigation API.

Usage:
    python -m api
    python -m api --host 127.0.0.1 --port 8000 --log-level info
    python -m api --incidents-root /path/to/incidents

Environment variables (read after loading .env):
    SENTINEL_PERSISTENCE_BACKEND   'filesystem' (default) or 'postgres'
    SENTINEL_PERSISTENCE_ROOT      Root dir for filesystem backend
                                   (default: .sentinel_persistence)
    SENTINEL_DATABASE_URL          PostgreSQL connection URL (postgres backend only)
    GROQ_API_KEY                   Required when investigations are submitted
    GROQ_MODEL                     LLM model override (default: openai/gpt-oss-120b)
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path


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
            "Path to the incident bundles directory "
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

    # Resolve incidents root: None preserves create_app()'s own default of Path("incidents").
    incidents_root: Path | None = (
        Path(args.incidents_root) if args.incidents_root is not None else None
    )

    # Construct the Starlette application.
    # llm_client is intentionally omitted — the orchestrator creates its own client
    # lazily via get_llm_client() when an investigation is submitted.
    from api.app import create_app

    app = create_app(repository=repository, incidents_root=incidents_root)

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
