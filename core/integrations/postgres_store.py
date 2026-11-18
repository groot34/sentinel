"""PostgreSQL-backed integration store for Sentinel 2.0 (Mission 27.3.1).

Durable counterpart to :class:`FilesystemIntegrationStore`, backed by the
``github_integrations`` table created by migration ``005_github_integrations.sql``.

The implementation mirrors the conventions of :class:`core.persistence.postgres.PostgresRepository`:
a pooled connection, explicit transactions, parameterised SQL throughout, and
``psycopg.Error`` translated into the integration error hierarchy. As with the
filesystem backend, no credential is ever stored - ``credential_ref`` is a
non-secret environment reference and there is deliberately no token column.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple, Union

import psycopg
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

from core.integrations.repository_store import (
    DuplicateConnectionError,
    GitHubConnection,
    IntegrationStore,
    IntegrationStoreError,
    _utcnow_iso,
    validate_connection_id,
)
from core.persistence.postgres import (
    DEFAULT_POOL_MAX_SIZE,
    DEFAULT_POOL_MIN_SIZE,
    DEFAULT_POOL_TIMEOUT_SECONDS,
    pool_config_from_env,
)

#: Explicit column order used by every SELECT / RETURNING clause.
_COLUMNS = (
    "connection_id",
    "provider",
    "label",
    "owner",
    "repo",
    "default_branch",
    "visibility",
    "status",
    "credential_ref",
    "scopes",
    "last_verified_at",
    "created_at",
    "updated_at",
)

_SELECT_COLUMNS = ", ".join(_COLUMNS)


def _parse_timestamp(value: Optional[str]) -> Optional[datetime]:
    """Parse an ISO-8601 string into an aware datetime, or None."""
    if value is None:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError) as e:
        raise IntegrationStoreError(f"Invalid timestamp value: {value!r}") from e
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


class PostgresIntegrationStore(IntegrationStore):
    """PostgreSQL implementation of :class:`IntegrationStore`."""

    def __init__(
        self,
        pool_or_conninfo: Union[ConnectionPool, str],
        min_size: int = DEFAULT_POOL_MIN_SIZE,
        max_size: int = DEFAULT_POOL_MAX_SIZE,
        timeout: float = DEFAULT_POOL_TIMEOUT_SECONDS,
    ) -> None:
        """Initialize the store with a ConnectionPool or a connection string.

        Args:
            pool_or_conninfo: An existing psycopg_pool.ConnectionPool or a
                PostgreSQL conninfo string.
            min_size: Minimum connections in the pool (when created from conninfo).
            max_size: Maximum connections in the pool (when created from conninfo).
            timeout: Connection-acquisition timeout in seconds.
        """
        if isinstance(pool_or_conninfo, str):
            if not pool_or_conninfo.strip():
                raise IntegrationStoreError("PostgreSQL connection string cannot be empty")
            self._pool = ConnectionPool(
                conninfo=pool_or_conninfo,
                min_size=min_size,
                max_size=max_size,
                timeout=timeout,
                open=True,
            )
            self._owns_pool = True
        elif hasattr(pool_or_conninfo, "connection") or isinstance(pool_or_conninfo, ConnectionPool):
            self._pool = pool_or_conninfo
            self._owns_pool = False
        else:
            raise IntegrationStoreError(
                f"Expected ConnectionPool or conninfo string, got {type(pool_or_conninfo).__name__}"
            )

    @property
    def pool(self) -> ConnectionPool:
        """Return the underlying connection pool."""
        return self._pool

    @classmethod
    def from_env(cls, env_var: str = "SENTINEL_DATABASE_URL") -> "PostgresIntegrationStore":
        """Construct the store from environment configuration.

        Raises:
            IntegrationStoreError: If the environment variable is unset/empty or
                the effective pool configuration is invalid.
        """
        import os

        conn_str = os.getenv(env_var)
        if not conn_str or not conn_str.strip():
            raise IntegrationStoreError(
                f"Environment variable '{env_var}' is not set or empty. "
                "Configure a valid PostgreSQL connection URL."
            )
        config = pool_config_from_env()
        return cls(
            conn_str.strip(),
            min_size=config.min_size,
            max_size=config.max_size,
            timeout=config.timeout,
        )

    def close(self) -> None:
        """Close the connection pool if owned by this store."""
        if self._owns_pool and self._pool is not None:
            self._pool.close()

    def __enter__(self) -> "PostgresIntegrationStore":
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        self.close()

    def initialize_schema(self) -> List[str]:
        """Apply all pending schema migrations (including 005). Idempotent."""
        from core.persistence.schema import apply_migrations

        return apply_migrations(self._pool)

    def verify_schema(self, raise_on_error: bool = False) -> Dict[str, Any]:
        """Verify the database schema, including the ``github_integrations`` table."""
        from core.persistence.schema import verify_schema

        report = verify_schema(self._pool, raise_on_error=raise_on_error)
        return report.to_dict()

    @staticmethod
    def _row_to_connection(row: Tuple[Any, ...]) -> GitHubConnection:
        """Build a GitHubConnection from a 13-column row in ``_COLUMNS`` order."""
        import json

        scopes = row[9]
        if isinstance(scopes, str):
            scopes = json.loads(scopes)
        if not isinstance(scopes, list):
            scopes = []

        return GitHubConnection(
            connection_id=row[0],
            provider=row[1],
            label=row[2],
            owner=row[3],
            repo=row[4],
            default_branch=row[5],
            visibility=row[6],
            status=row[7],
            credential_ref=row[8],
            scopes=list(scopes),
            last_verified_at=row[10].isoformat() if row[10] else None,
            created_at=row[11].isoformat() if row[11] else _utcnow_iso(),
            updated_at=row[12].isoformat() if row[12] else _utcnow_iso(),
        )

    # ------------------------------------------------------------------
    # IntegrationStore interface
    # ------------------------------------------------------------------

    def create(self, connection: GitHubConnection) -> None:
        """Persist a new connection record."""
        if not isinstance(connection, GitHubConnection):
            raise IntegrationStoreError(
                f"create() accepts only GitHubConnection, got {type(connection).__name__}"
            )

        try:
            with self._pool.connection() as conn:
                with conn.transaction():
                    with conn.cursor() as cur:
                        cur.execute(
                            f"""
                            INSERT INTO github_integrations ({_SELECT_COLUMNS})
                            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s);
                            """,
                            (
                                connection.connection_id,
                                connection.provider,
                                connection.label,
                                connection.owner,
                                connection.repo,
                                connection.default_branch,
                                connection.visibility,
                                connection.status,
                                connection.credential_ref,
                                Jsonb(list(connection.scopes)),
                                _parse_timestamp(connection.last_verified_at),
                                _parse_timestamp(connection.created_at),
                                _parse_timestamp(connection.updated_at),
                            ),
                        )
        except psycopg.errors.UniqueViolation as e:
            raise DuplicateConnectionError(
                f"A connection with the same ID or (owner, repo) already exists "
                f"({connection.owner}/{connection.repo})."
            ) from e
        except psycopg.Error as e:
            raise IntegrationStoreError(
                f"Database error creating connection '{connection.connection_id}': {e}"
            ) from e

    def get(self, connection_id: str) -> Optional[GitHubConnection]:
        """Retrieve a connection by ID, or None when it does not exist."""
        safe_id = validate_connection_id(connection_id)
        try:
            with self._pool.connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        f"SELECT {_SELECT_COLUMNS} FROM github_integrations WHERE connection_id = %s;",
                        (safe_id,),
                    )
                    row = cur.fetchone()
                    if row is None:
                        return None
                    return self._row_to_connection(row)
        except psycopg.Error as e:
            raise IntegrationStoreError(f"Database error loading connection '{safe_id}': {e}") from e

    def list(self) -> List[GitHubConnection]:
        """Return all connections ordered deterministically by connection ID."""
        try:
            with self._pool.connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        f"SELECT {_SELECT_COLUMNS} FROM github_integrations ORDER BY connection_id ASC;"
                    )
                    return [self._row_to_connection(row) for row in cur.fetchall()]
        except psycopg.Error as e:
            raise IntegrationStoreError(f"Database error listing connections: {e}") from e

    def update(
        self,
        connection_id: str,
        *,
        label: Optional[str] = None,
        status: Optional[str] = None,
        default_branch: Optional[str] = None,
        visibility: Optional[str] = None,
        scopes: Optional[List[str]] = None,
        last_verified_at: Optional[str] = None,
    ) -> GitHubConnection:
        """Update mutable metadata and/or status of an existing connection.

        The complete resulting record is validated through the shared
        :class:`GitHubConnection` model (exactly as the filesystem backend does)
        before any write, so both backends accept and reject identical values.
        Validation failures raise before the UPDATE, leaving the stored record
        untouched. Identity/immutable fields are never modified.
        """
        safe_id = validate_connection_id(connection_id)

        try:
            with self._pool.connection() as conn:
                with conn.transaction():
                    with conn.cursor() as cur:
                        # Lock the existing row so the read-modify-write is atomic.
                        cur.execute(
                            f"SELECT {_SELECT_COLUMNS} FROM github_integrations "
                            "WHERE connection_id = %s FOR UPDATE;",
                            (safe_id,),
                        )
                        row = cur.fetchone()
                        if row is None:
                            raise IntegrationStoreError(f"Connection '{safe_id}' not found")

                        current = self._row_to_connection(row)
                        data = current.to_dict()
                        if label is not None:
                            data["label"] = label
                        if status is not None:
                            data["status"] = status
                        if default_branch is not None:
                            data["default_branch"] = default_branch
                        if visibility is not None:
                            data["visibility"] = visibility
                        if scopes is not None:
                            data["scopes"] = list(scopes)
                        if last_verified_at is not None:
                            data["last_verified_at"] = last_verified_at
                        data["updated_at"] = _utcnow_iso()

                        # Single source of truth: raises IntegrationStoreError on
                        # any invalid field (status, visibility, timestamps, ...).
                        updated = GitHubConnection.from_dict(data)

                        cur.execute(
                            f"""
                            UPDATE github_integrations
                            SET label = %s,
                                status = %s,
                                default_branch = %s,
                                visibility = %s,
                                scopes = %s,
                                last_verified_at = %s,
                                updated_at = %s
                            WHERE connection_id = %s
                            RETURNING {_SELECT_COLUMNS};
                            """,
                            (
                                updated.label,
                                updated.status,
                                updated.default_branch,
                                updated.visibility,
                                Jsonb(list(updated.scopes)),
                                _parse_timestamp(updated.last_verified_at),
                                _parse_timestamp(updated.updated_at),
                                safe_id,
                            ),
                        )
                        updated_row = cur.fetchone()
                        if updated_row is None:
                            raise IntegrationStoreError(f"Connection '{safe_id}' not found")
                        return self._row_to_connection(updated_row)
        except IntegrationStoreError:
            raise
        except psycopg.Error as e:
            raise IntegrationStoreError(f"Database error updating connection '{safe_id}': {e}") from e

    def delete(self, connection_id: str) -> None:
        """Delete a connection. Idempotent."""
        try:
            safe_id = validate_connection_id(connection_id)
        except IntegrationStoreError:
            return

        try:
            with self._pool.connection() as conn:
                with conn.transaction():
                    with conn.cursor() as cur:
                        cur.execute(
                            "DELETE FROM github_integrations WHERE connection_id = %s;",
                            (safe_id,),
                        )
        except psycopg.Error as e:
            raise IntegrationStoreError(f"Database error deleting connection '{safe_id}': {e}") from e

    def exists(self, connection_id: str) -> bool:
        """Return True when a connection with the given ID exists."""
        safe_id = validate_connection_id(connection_id)
        try:
            with self._pool.connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT 1 FROM github_integrations WHERE connection_id = %s;",
                        (safe_id,),
                    )
                    return cur.fetchone() is not None
        except psycopg.Error as e:
            raise IntegrationStoreError(f"Database error checking connection '{safe_id}': {e}") from e
