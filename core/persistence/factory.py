"""Repository factory for Sentinel 2.0.

Provides dynamic construction of PersistenceRepository instances based on environment configuration.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

from core.persistence.filesystem import FilesystemRepository
from core.persistence.postgres import PostgresRepository
from core.persistence.repository import PersistenceError, PersistenceRepository

DEFAULT_PERSISTENCE_ROOT = ".sentinel_persistence"


def get_repository(
    backend: Optional[str] = None,
    persistence_root: Optional[str | Path] = None,
    database_url: Optional[str] = None,
) -> PersistenceRepository:
    """Construct and return the configured PersistenceRepository.

    Args:
        backend: Optional backend override ('filesystem' or 'postgres').
                 Defaults to the value of SENTINEL_PERSISTENCE_BACKEND or 'filesystem'.
        persistence_root: Optional persistence root directory override for filesystem backend.
        database_url: Optional PostgreSQL connection URL override for postgres backend.

    Returns:
        Configured PersistenceRepository instance.

    Raises:
        PersistenceError: If an unsupported backend is specified or required configuration is missing.
    """
    selected_backend = (backend or os.getenv("SENTINEL_PERSISTENCE_BACKEND", "filesystem")).strip().lower()

    if selected_backend == "filesystem":
        root = persistence_root or os.getenv("SENTINEL_PERSISTENCE_ROOT", DEFAULT_PERSISTENCE_ROOT)
        return FilesystemRepository(persistence_root=root)

    elif selected_backend == "postgres":
        conn_str = database_url or os.getenv("SENTINEL_DATABASE_URL")
        if not conn_str or not conn_str.strip():
            raise PersistenceError(
                "PostgreSQL backend requested, but SENTINEL_DATABASE_URL is not configured. "
                "Set SENTINEL_DATABASE_URL or pass database_url."
            )
        return PostgresRepository(conn_str.strip())

    else:
        raise PersistenceError(
            f"Unsupported persistence backend: {selected_backend!r}. "
            "Supported backends are 'filesystem' and 'postgres'."
        )
