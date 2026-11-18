"""Integration-store factory for Sentinel 2.0 (Mission 27.3.1).

Selects the integration persistence backend using the same configuration
conventions as :func:`core.persistence.factory.get_repository`:

* Filesystem remains the default backend.
* PostgreSQL is selected when the existing PostgreSQL persistence configuration
  is active (``SENTINEL_PERSISTENCE_BACKEND=postgres`` with
  ``SENTINEL_DATABASE_URL`` set).

No GitHub token is required to initialise persistence, so an unconfigured
GitHub integration never blocks startup.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

from core.integrations.filesystem_store import FilesystemIntegrationStore
from core.integrations.postgres_store import PostgresIntegrationStore
from core.integrations.repository_store import IntegrationStore, IntegrationStoreError
from core.persistence.factory import DEFAULT_PERSISTENCE_ROOT
from core.persistence.postgres import pool_config_from_env


def get_integration_store(
    backend: Optional[str] = None,
    persistence_root: Optional[str | Path] = None,
    database_url: Optional[str] = None,
) -> IntegrationStore:
    """Construct and return the configured :class:`IntegrationStore`.

    Args:
        backend: Optional backend override ('filesystem' or 'postgres'). Defaults
            to the value of ``SENTINEL_PERSISTENCE_BACKEND`` or 'filesystem'.
        persistence_root: Optional persistence root override for the filesystem
            backend.
        database_url: Optional PostgreSQL connection URL override for the
            postgres backend.

    Returns:
        A configured IntegrationStore instance.

    Raises:
        IntegrationStoreError: If an unsupported backend is selected or required
            configuration is missing.
    """
    selected = (backend or os.getenv("SENTINEL_PERSISTENCE_BACKEND", "filesystem")).strip().lower()

    if selected == "filesystem":
        root = persistence_root or os.getenv("SENTINEL_PERSISTENCE_ROOT", DEFAULT_PERSISTENCE_ROOT)
        return FilesystemIntegrationStore(persistence_root=root)

    if selected == "postgres":
        conn_str = database_url or os.getenv("SENTINEL_DATABASE_URL")
        if not conn_str or not conn_str.strip():
            raise IntegrationStoreError(
                "PostgreSQL backend requested, but SENTINEL_DATABASE_URL is not configured. "
                "Set SENTINEL_DATABASE_URL or pass database_url."
            )
        config = pool_config_from_env()
        return PostgresIntegrationStore(
            conn_str.strip(),
            min_size=config.min_size,
            max_size=config.max_size,
            timeout=config.timeout,
        )

    raise IntegrationStoreError(
        f"Unsupported integration store backend: {selected!r}. "
        "Supported backends are 'filesystem' and 'postgres'."
    )
