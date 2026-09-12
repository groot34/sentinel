"""PostgreSQL persistence repository for Sentinel 2.0.

Provides durable, atomic, and concurrency-controlled persistence of InvestigationState
backed by a PostgreSQL database.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

import psycopg
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

from core.domain.state import InvestigationState
from core.persistence.repository import (
    ConcurrencyError,
    PersistenceError,
    PersistenceRepository,
    check_persistence_safety,
    validate_investigation_id,
)

SCHEMA_FILE = Path(__file__).parent / "sql" / "001_initial_schema.sql"


class PostgresRepository(PersistenceRepository):
    """PostgreSQL implementation of PersistenceRepository."""

    def __init__(
        self,
        pool_or_conninfo: Union[ConnectionPool, str],
        min_size: int = 1,
        max_size: int = 10,
        timeout: float = 30.0,
    ) -> None:
        """Initialize PostgresRepository with a ConnectionPool or connection string.

        Args:
            pool_or_conninfo: An existing psycopg_pool.ConnectionPool or PostgreSQL conninfo string.
            min_size: Minimum connections in pool (if creating from conninfo).
            max_size: Maximum connections in pool (if creating from conninfo).
            timeout: Connection acquisition timeout in seconds.
        """
        if isinstance(pool_or_conninfo, str):
            if not pool_or_conninfo.strip():
                raise PersistenceError("PostgreSQL connection string cannot be empty")
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
            raise PersistenceError(
                f"Expected ConnectionPool or conninfo string, got {type(pool_or_conninfo).__name__}"
            )

    @property
    def pool(self) -> ConnectionPool:
        """Return the underlying connection pool."""
        return self._pool

    @classmethod
    def from_env(
        cls,
        env_var: str = "SENTINEL_DATABASE_URL",
        min_size: int = 1,
        max_size: int = 10,
    ) -> PostgresRepository:
        """Construct PostgresRepository from environment variable.

        Args:
            env_var: Name of the environment variable containing PostgreSQL connection string.

        Returns:
            PostgresRepository instance.

        Raises:
            PersistenceError: If the environment variable is unset or empty.
        """
        conn_str = os.getenv(env_var)
        if not conn_str or not conn_str.strip():
            raise PersistenceError(
                f"Environment variable '{env_var}' is not set or empty. "
                "Configure a valid PostgreSQL connection URL."
            )
        return cls(conn_str.strip(), min_size=min_size, max_size=max_size)

    def close(self) -> None:
        """Close the connection pool if owned by this repository."""
        if self._owns_pool and self._pool is not None:
            self._pool.close()

    def __enter__(self) -> PostgresRepository:
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        self.close()

    def initialize_schema(self) -> None:
        """Execute the initial schema migration script. Idempotent."""
        if not SCHEMA_FILE.is_file():
            raise PersistenceError(f"Schema file not found at '{SCHEMA_FILE}'")

        sql = SCHEMA_FILE.read_text(encoding="utf-8")
        try:
            with self._pool.connection() as conn:
                with conn.transaction():
                    with conn.cursor() as cur:
                        cur.execute(sql)
        except psycopg.Error as e:
            raise PersistenceError(f"Failed to initialize PostgreSQL schema: {e}") from e

    def save(self, state: InvestigationState) -> None:
        """Persist InvestigationState transactionally with optimistic concurrency control.

        Args:
            state: Valid InvestigationState instance.

        Raises:
            ConcurrencyError: If an optimistic concurrency conflict or concurrent insert occurs.
            PersistenceError: If validation, safety check, or database operation fails.
        """
        if not isinstance(state, InvestigationState):
            raise PersistenceError(
                f"save() accepts only InvestigationState, got {type(state).__name__}"
            )

        safe_id = validate_investigation_id(state.incident_id)

        try:
            state_dict = state.to_dict()
        except Exception as e:
            raise PersistenceError(f"Failed to convert InvestigationState to dict: {e}") from e

        try:
            json_str = json.dumps(state_dict, ensure_ascii=False)
        except Exception as e:
            raise PersistenceError(f"Failed to serialize state to JSON: {e}") from e

        # Apply strict safety boundary checks
        check_persistence_safety(json_str)

        status_val = state.status if isinstance(state.status, str) else state.status.value
        current_stage = state.current_stage
        llm_call_count = state.llm_call_count
        started_at = state.started_at
        completed_at = state.completed_at
        error = state.error
        expected_version = state.version

        try:
            with self._pool.connection() as conn:
                with conn.transaction():
                    with conn.cursor() as cur:
                        # Query existing version with row lock to serialize concurrent saves
                        cur.execute(
                            "SELECT version FROM investigations WHERE investigation_id = %s FOR UPDATE;",
                            (safe_id,),
                        )
                        row = cur.fetchone()

                        if row is None:
                            # Brand-new investigation: INSERT with version = 1
                            try:
                                cur.execute(
                                    """
                                    INSERT INTO investigations (
                                        investigation_id, status, current_stage, version,
                                        llm_call_count, started_at, completed_at, error,
                                        state_payload, created_at, updated_at
                                    ) VALUES (
                                        %s, %s, %s, %s,
                                        %s, %s, %s, %s,
                                        %s, NOW(), NOW()
                                    );
                                    """,
                                    (
                                        safe_id,
                                        status_val,
                                        current_stage,
                                        1,
                                        llm_call_count,
                                        started_at,
                                        completed_at,
                                        error,
                                        Jsonb(state_dict),
                                    ),
                                )
                            except psycopg.errors.UniqueViolation as uv_err:
                                raise ConcurrencyError(
                                    f"Concurrent insert conflict for investigation '{safe_id}': row already exists"
                                ) from uv_err
                            state.version = 1
                        else:
                            # Existing investigation: Verify OCC version token
                            db_version = row[0]
                            if db_version != expected_version:
                                raise ConcurrencyError(
                                    f"Optimistic concurrency conflict for investigation '{safe_id}'. "
                                    f"State version is {expected_version}, but database version is {db_version}."
                                )

                            next_version = expected_version + 1
                            payload_dict = dict(state_dict)
                            payload_dict["version"] = next_version

                            cur.execute(
                                """
                                UPDATE investigations
                                SET status = %s,
                                    current_stage = %s,
                                    version = version + 1,
                                    llm_call_count = %s,
                                    started_at = %s,
                                    completed_at = %s,
                                    error = %s,
                                    state_payload = %s,
                                    updated_at = NOW()
                                WHERE investigation_id = %s
                                  AND version = %s
                                RETURNING version;
                                """,
                                (
                                    status_val,
                                    current_stage,
                                    llm_call_count,
                                    started_at,
                                    completed_at,
                                    error,
                                    Jsonb(payload_dict),
                                    safe_id,
                                    expected_version,
                                ),
                            )
                            update_row = cur.fetchone()
                            if update_row is None:
                                raise ConcurrencyError(
                                    f"Optimistic concurrency conflict for investigation '{safe_id}'. "
                                    f"Row was modified concurrently (expected version {expected_version})."
                                )
                            state.version = update_row[0]
        except ConcurrencyError:
            raise
        except psycopg.Error as e:
            raise PersistenceError(f"Database error during save for '{safe_id}': {e}") from e

    def load(self, investigation_id: str) -> Optional[InvestigationState]:
        """Load InvestigationState from PostgreSQL.

        Args:
            investigation_id: Identifier of the investigation to load.

        Returns:
            InvestigationState if exists, None if record does not exist.

        Raises:
            PersistenceError: If state is invalid, malformed, or ID mismatched.
        """
        safe_id = validate_investigation_id(investigation_id)

        try:
            with self._pool.connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT state_payload, version FROM investigations WHERE investigation_id = %s;",
                        (safe_id,),
                    )
                    row = cur.fetchone()
                    if row is None:
                        return None
                    payload, db_version = row[0], row[1]
        except psycopg.Error as e:
            raise PersistenceError(f"Database error loading investigation '{safe_id}': {e}") from e

        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except Exception as e:
                raise PersistenceError(f"Malformed JSON in database for '{safe_id}': {e}") from e

        if not isinstance(payload, dict):
            raise PersistenceError(f"Invalid state format for '{safe_id}': root must be a dict")

        try:
            state = InvestigationState.from_dict(payload)
        except Exception as e:
            raise PersistenceError(f"Failed to deserialize InvestigationState for '{safe_id}': {e}") from e

        if state.incident_id != safe_id:
            raise PersistenceError(
                f"Investigation ID mismatch: requested '{safe_id}', state contains '{state.incident_id}'"
            )

        # Preserve the authoritative database version on the reconstructed state
        state.version = db_version
        return state

    def exists(self, investigation_id: str) -> bool:
        """Check if an investigation exists using an efficient key lookup.

        Args:
            investigation_id: Identifier to check.

        Returns:
            True if record exists, False otherwise.
        """
        safe_id = validate_investigation_id(investigation_id)

        try:
            with self._pool.connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT 1 FROM investigations WHERE investigation_id = %s;",
                        (safe_id,),
                    )
                    return cur.fetchone() is not None
        except psycopg.Error as e:
            raise PersistenceError(f"Database error checking existence for '{safe_id}': {e}") from e

    def delete(self, investigation_id: str) -> None:
        """Delete an investigation record. Idempotent.

        Args:
            investigation_id: Identifier of investigation to delete.
        """
        try:
            safe_id = validate_investigation_id(investigation_id)
        except PersistenceError:
            return

        try:
            with self._pool.connection() as conn:
                with conn.transaction():
                    with conn.cursor() as cur:
                        cur.execute(
                            "DELETE FROM investigations WHERE investigation_id = %s;",
                            (safe_id,),
                        )
        except psycopg.Error as e:
            raise PersistenceError(f"Database error deleting investigation '{safe_id}': {e}") from e

    def list(self) -> List[str]:
        """List all investigation IDs in deterministic alphabetical order.

        Returns:
            Sorted list of investigation IDs.
        """
        try:
            with self._pool.connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT investigation_id FROM investigations ORDER BY investigation_id ASC;"
                    )
                    return [row[0] for row in cur.fetchall()]
        except psycopg.Error as e:
            raise PersistenceError(f"Database error listing investigations: {e}") from e
