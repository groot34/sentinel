"""PostgreSQL persistence repository for Sentinel 2.0.

Provides durable, atomic, and concurrency-controlled persistence of InvestigationState
backed by a PostgreSQL database.
"""

from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

import psycopg
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

from core.domain.state import InvestigationState
from core.events.base import DomainEvent
from core.persistence.outbox import OutboxEvent, OutboxRepository
from core.persistence.repository import (
    ConcurrencyError,
    PersistenceError,
    PersistenceRepository,
    check_persistence_safety,
    validate_investigation_id,
)
from core.persistence.retry import (
    RetryExhaustedError,
    RetryPolicy,
    maybe_inject_fault,
    policy_from_env,
    run_with_retry,
)

SCHEMA_FILE = Path(__file__).parent / "sql" / "001_initial_schema.sql"

#: Bounded connection-acquisition timeout (seconds) for the readiness probe.
#: Caps how long the probe waits for a pooled connection, independently of the
#: HTTP response deadline enforced by the API layer.
_PROBE_ACQUISITION_TIMEOUT_SECONDS = 2.0


class PostgresRepository(PersistenceRepository):
    """PostgreSQL implementation of PersistenceRepository."""

    def __init__(
        self,
        pool_or_conninfo: Union[ConnectionPool, str],
        min_size: int = 1,
        max_size: int = 10,
        timeout: float = 30.0,
        retry_policy: Optional[RetryPolicy] = None,
    ) -> None:
        """Initialize PostgresRepository with a ConnectionPool or connection string.

        Args:
            pool_or_conninfo: An existing psycopg_pool.ConnectionPool or PostgreSQL conninfo string.
            min_size: Minimum connections in pool (if creating from conninfo).
            max_size: Maximum connections in pool (if creating from conninfo).
            timeout: Connection acquisition timeout in seconds.
            retry_policy: Optional bounded retry policy for transient PostgreSQL
                contention errors (SQLSTATE 40001/40P01). Defaults to the
                conservative policy read from the SENTINEL_PG_RETRY_* env vars.
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

        # Conservative, bounded retry for transient contention (SQLSTATE 40001/40P01).
        self._retry_policy = retry_policy or policy_from_env()

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

    def initialize_schema(self) -> List[str]:
        """Execute all pending schema migrations in deterministic order. Idempotent.

        Uses the authoritative schema migration engine to discover and apply migrations
        transactionally, tracking applied versions in schema_migrations.
        """
        from core.persistence.schema import apply_migrations
        return apply_migrations(self._pool)

    def initialize_outbox_schema(self) -> List[str]:
        """Ensure outbox schema is applied. Idempotent.

        Delegates to the authoritative migration runner, ensuring both initial schema
        and outbox schema are applied safely in order.
        """
        from core.persistence.schema import apply_migrations
        return apply_migrations(self._pool)

    def verify_schema(self, raise_on_error: bool = False) -> Dict[str, Any]:
        """Verify that all required tables, columns, and operational indexes are valid.

        Args:
            raise_on_error: If True, raises SchemaVerificationError on discrepancies.

        Returns:
            Dictionary containing verification report.
        """
        from core.persistence.schema import verify_schema
        report = verify_schema(self._pool, raise_on_error=raise_on_error)
        return report.to_dict()

    def save(self, state: InvestigationState, events: Optional[List[DomainEvent]] = None) -> None:
        """Persist InvestigationState with bounded transient-failure retry.

        Retries the *entire* transaction on transient PostgreSQL contention
        errors (SQLSTATE 40001 serialization_failure, 40P01 deadlock_detected)
        with a conservative bounded backoff. OCC ``ConcurrencyError`` and
        permanent errors are never retried. Delegates the work to
        :meth:`_save_transaction`, re-executed with fresh transactional state.

        Args:
            state: Valid InvestigationState instance.
            events: Optional list of DomainEvents to persist atomically in the same transaction.

        Raises:
            ConcurrencyError: If an optimistic concurrency conflict or concurrent insert occurs (never retried).
            RetryExhaustedError: If transient PostgreSQL failures exhaust the retry budget.
            PersistenceError: If validation, safety check, or a permanent database error fails.
        """
        if not isinstance(state, InvestigationState):
            raise PersistenceError(
                f"save() accepts only InvestigationState, got {type(state).__name__}"
            )

        entry_version = state.version

        def _attempt(attempt: int) -> None:
            # Restore the entry version before every attempt so retries never
            # double-increment the OCC token or duplicate persisted state.
            state.version = entry_version
            self._save_transaction(state, events, attempt)

        try:
            run_with_retry(_attempt, policy=self._retry_policy)
        except RetryExhaustedError:
            # Leave the in-memory state at its entry version on exhaustion.
            state.version = entry_version
            raise

    def _save_transaction(
        self,
        state: InvestigationState,
        events: Optional[List[DomainEvent]],
        attempt: int,
    ) -> None:
        """Execute exactly one atomic transaction attempt (state + outbox).

        Invoked by :func:`core.persistence.retry.run_with_retry`, which re-runs
        this *entire* transaction with a fresh connection on transient
        PostgreSQL failures. This method performs only database work, so
        re-execution cannot repeat external side effects. OCC semantics, lease
        ownership, and outbox atomicity are preserved across attempts.
        """
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
                    # Deterministic test-only fault-injection seam at the
                    # transaction boundary (no-op unless a test installs a hook).
                    maybe_inject_fault(attempt)
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

                        # Insert events into outbox table within the same transaction.
                        # ON CONFLICT DO NOTHING makes duplicate event IDs a no-op
                        # instead of raising UniqueViolation, which would abort the
                        # whole transaction and prevent the valid state change from
                        # committing.
                        if events:
                            for event in events:
                                outbox_event = OutboxEvent.from_domain_event(event)
                                cur.execute(
                                    """
                                    INSERT INTO event_outbox (
                                        event_id, investigation_id, event_type, payload,
                                        status, created_at, retry_count
                                    ) VALUES (
                                        %s, %s, %s, %s,
                                        %s, %s, %s
                                    )
                                    ON CONFLICT (event_id) DO NOTHING;
                                    """,
                                    (
                                        outbox_event.event_id,
                                        outbox_event.investigation_id,
                                        outbox_event.event_type,
                                        Jsonb(outbox_event.payload),
                                        outbox_event.status,
                                        outbox_event.created_at,
                                        outbox_event.retry_count,
                                    ),
                                )
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

    def probe(self) -> None:
        """Lightweight, read-only readiness probe backed by ``SELECT 1``.

        Acquires a pooled connection with a bounded acquisition timeout and
        caps the server-side statement via a transaction-scoped
        ``SET LOCAL statement_timeout``, so neither a saturated pool nor a
        stuck query can pin the probe indefinitely.

        Limitations: ``SET LOCAL`` bounds *server-side* statement execution and
        the acquisition timeout bounds *pool waits*, but neither bounds every
        possible network stall — a partially established connection can still
        block beyond the HTTP response deadline. The API layer keeps such
        abandoned probes bounded by its fixed readiness admission capacity.

        Raises:
            PersistenceError: If the database is unreachable or the probe fails.
        """
        statement_timeout_ms = max(1, int(_PROBE_ACQUISITION_TIMEOUT_SECONDS * 1000))
        try:
            with self._pool.connection(timeout=_PROBE_ACQUISITION_TIMEOUT_SECONDS) as conn:
                with conn.cursor() as cur:
                    # Transaction-scoped: applies only while this implicit
                    # transaction is open, so it cannot leak to other users of
                    # the pooled connection. Skipped in autocommit mode, where
                    # a local setting would be a no-op.
                    if not conn.autocommit:
                        cur.execute(f"SET LOCAL statement_timeout = {statement_timeout_ms}")
                    cur.execute("SELECT 1;")
                    cur.fetchone()
        except psycopg.Error as e:
            raise PersistenceError("Database readiness probe failed") from e

    # -----------------------------------------------------------------------
    # Outbox Event Persistence Methods
    # -----------------------------------------------------------------------

    def save_event(self, event: DomainEvent) -> None:
        """Persist a single domain event to the outbox.

        Args:
            event: DomainEvent to persist.

        Raises:
            PersistenceError: If save fails.
        """
        self.save_events([event])

    def save_events(self, events: List[DomainEvent]) -> None:
        """Persist multiple domain events to the outbox.

        Args:
            events: List of DomainEvents to persist.

        Raises:
            PersistenceError: If save fails.
        """
        if not events:
            return

        try:
            with self._pool.connection() as conn:
                with conn.transaction():
                    with conn.cursor() as cur:
                        for event in events:
                            outbox_event = OutboxEvent.from_domain_event(event)
                            # Conflict-safe: a duplicate event ID is ignored without
                            # raising, so one duplicate cannot abort the transaction
                            # and discard the other valid inserts.
                            cur.execute(
                                """
                                INSERT INTO event_outbox (
                                    event_id, investigation_id, event_type, payload,
                                    status, created_at, retry_count
                                ) VALUES (
                                    %s, %s, %s, %s,
                                    %s, %s, %s
                                )
                                ON CONFLICT (event_id) DO NOTHING;
                                """,
                                (
                                    outbox_event.event_id,
                                    outbox_event.investigation_id,
                                    outbox_event.event_type,
                                    Jsonb(outbox_event.payload),
                                    outbox_event.status,
                                    outbox_event.created_at,
                                    outbox_event.retry_count,
                                ),
                            )
        except psycopg.Error as e:
            raise PersistenceError(f"Database error saving events to outbox: {e}") from e

    def fetch_pending(self, limit: int = 100) -> List[OutboxEvent]:
        """Fetch pending events for delivery.

        Observational query only: it does not claim or mutate events, so it is
        safe for auditing and tests but must not drive delivery. The worker uses
        :meth:`claim_pending` to obtain exclusive delivery claims.

        Args:
            limit: Maximum number of events to fetch.

        Returns:
            List of pending OutboxEvents in canonical (created_at, event_id) order.
        """
        try:
            with self._pool.connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        SELECT event_id, investigation_id, event_type, payload,
                               status, created_at, processed_at, retry_count, error_message
                        FROM event_outbox
                        WHERE status = 'pending'
                        ORDER BY created_at ASC, event_id ASC
                        LIMIT %s;
                        """,
                        (limit,),
                    )
                    return [self._row_to_outbox_event(row) for row in cur.fetchall()]
        except psycopg.Error as e:
            raise PersistenceError(f"Database error fetching pending events: {e}") from e

    @staticmethod
    def _row_to_outbox_event(row: Tuple[Any, ...]) -> OutboxEvent:
        """Build an OutboxEvent from a 9-column outbox row."""
        return OutboxEvent(
            event_id=row[0],
            investigation_id=row[1],
            event_type=row[2],
            payload=dict(row[3]) if isinstance(row[3], dict) else row[3],
            status=row[4],
            created_at=row[5].isoformat() if row[5] else "",
            processed_at=row[6].isoformat() if row[6] else None,
            retry_count=row[7],
            error_message=row[8],
        )

    def claim_pending(
        self,
        worker_id: str,
        limit: int = 100,
        claim_ttl_seconds: float = 300.0,
    ) -> List[OutboxEvent]:
        """Atomically claim up to ``limit`` eligible pending events for delivery.

        Uses ``FOR UPDATE SKIP LOCKED`` so that concurrent worker processes claim
        disjoint event batches without blocking or double-claiming. Eligibility
        requires ``status = 'pending'`` and ``next_attempt_at <= NOW()``, so
        events scheduled for a future retry and already-processed/failed events
        are excluded. Ordering is deterministic: ``(created_at ASC, event_id ASC)``.

        Args:
            worker_id: Identity of the claiming worker.
            limit: Maximum number of events to claim.
            claim_ttl_seconds: Visibility timeout for the resulting claim.

        Returns:
            List of claimed OutboxEvents in canonical (created_at, event_id) order.

        Raises:
            PersistenceError: If claiming fails.
        """
        if not worker_id or not str(worker_id).strip():
            raise PersistenceError("worker_id cannot be empty")
        if claim_ttl_seconds <= 0:
            raise PersistenceError("claim_ttl_seconds must be > 0")

        try:
            with self._pool.connection() as conn:
                with conn.transaction():
                    with conn.cursor() as cur:
                        # Lock the eligible rows in canonical order. FOR UPDATE
                        # SKIP LOCKED makes concurrent claimers skip rows already
                        # locked by another in-flight claim, so two workers can
                        # never own the same event. Locks are held until commit.
                        cur.execute(
                            """
                            SELECT event_id
                            FROM event_outbox
                            WHERE status = 'pending'
                              AND next_attempt_at <= NOW()
                            ORDER BY created_at ASC, event_id ASC
                            LIMIT %s
                            FOR UPDATE SKIP LOCKED;
                            """,
                            (limit,),
                        )
                        ordered_ids = [row[0] for row in cur.fetchall()]
                        if not ordered_ids:
                            return []

                        # Claim exactly the locked rows. UPDATE ... RETURNING does
                        # not guarantee row order, so the deterministic order is
                        # restored from the ordered id list below.
                        cur.execute(
                            """
                            UPDATE event_outbox
                            SET status = 'processing',
                                claimed_by = %s,
                                claimed_at = NOW(),
                                claim_expires_at = NOW() + make_interval(secs => %s)
                            WHERE event_id = ANY(%s::text[])
                            RETURNING event_id, investigation_id, event_type, payload,
                                      status, created_at, processed_at, retry_count, error_message;
                            """,
                            (worker_id, claim_ttl_seconds, ordered_ids),
                        )
                        rows_by_id = {row[0]: row for row in cur.fetchall()}
                        return [
                            self._row_to_outbox_event(rows_by_id[eid])
                            for eid in ordered_ids
                            if eid in rows_by_id
                        ]
        except psycopg.Error as e:
            raise PersistenceError(f"Database error claiming pending events: {e}") from e

    def release_expired_claims(self) -> int:
        """Return abandoned claims (past their visibility timeout) to pending.

        Crash recovery: a worker that dies mid-delivery leaves its claim to
        expire; this sweep makes those events claimable again. Completed
        (``processed``) and terminally ``failed`` events are never touched.

        Returns:
            Number of events whose claims were released.

        Raises:
            PersistenceError: If recovery fails.
        """
        try:
            with self._pool.connection() as conn:
                with conn.transaction():
                    with conn.cursor() as cur:
                        cur.execute(
                            """
                            UPDATE event_outbox
                            SET status = 'pending',
                                claimed_by = NULL,
                                claimed_at = NULL,
                                claim_expires_at = NULL
                            WHERE status = 'processing'
                              AND claim_expires_at IS NOT NULL
                              AND claim_expires_at <= NOW();
                            """
                        )
                        return cur.rowcount
        except psycopg.Error as e:
            raise PersistenceError(f"Database error releasing expired claims: {e}") from e

    def schedule_retry(
        self, event_id: str, next_attempt_at: datetime, error_message: str
    ) -> None:
        """Durably reschedule a failed event for a later attempt.

        Increments the attempt counter, records the error, clears the claim, and
        sets the earliest time the event may be claimed again. This replaces the
        previous in-process backoff sleep, so retry timing survives worker
        restarts and does not block the delivery batch.

        Args:
            event_id: Event ID to reschedule.
            next_attempt_at: Earliest time the event may be claimed again.
            error_message: Error description from the failed attempt.

        Raises:
            PersistenceError: If the update fails.
        """
        try:
            with self._pool.connection() as conn:
                with conn.transaction():
                    with conn.cursor() as cur:
                        cur.execute(
                            """
                            UPDATE event_outbox
                            SET status = 'pending',
                                retry_count = retry_count + 1,
                                next_attempt_at = %s,
                                error_message = %s,
                                claimed_by = NULL,
                                claimed_at = NULL,
                                claim_expires_at = NULL
                            WHERE event_id = %s;
                            """,
                            (next_attempt_at, error_message, event_id),
                        )
        except psycopg.Error as e:
            raise PersistenceError(f"Database error scheduling event retry: {e}") from e

    def mark_processed(self, event_id: str) -> None:
        """Mark an event as successfully processed.

        Args:
            event_id: Event ID to mark.

        Raises:
            PersistenceError: If update fails.
        """
        try:
            with self._pool.connection() as conn:
                with conn.transaction():
                    with conn.cursor() as cur:
                        cur.execute(
                            """
                            UPDATE event_outbox
                            SET status = 'processed',
                                processed_at = NOW(),
                                claimed_by = NULL,
                                claimed_at = NULL,
                                claim_expires_at = NULL
                            WHERE event_id = %s;
                            """,
                            (event_id,),
                        )
        except psycopg.Error as e:
            raise PersistenceError(f"Database error marking event processed: {e}") from e

    def mark_failed(self, event_id: str, error_message: str) -> None:
        """Mark an event as failed with error message.

        Args:
            event_id: Event ID to mark.
            error_message: Error description.

        Raises:
            PersistenceError: If update fails.
        """
        try:
            with self._pool.connection() as conn:
                with conn.transaction():
                    with conn.cursor() as cur:
                        cur.execute(
                            """
                            UPDATE event_outbox
                            SET status = 'failed',
                                error_message = %s,
                                processed_at = NOW(),
                                claimed_by = NULL,
                                claimed_at = NULL,
                                claim_expires_at = NULL
                            WHERE event_id = %s;
                            """,
                            (error_message, event_id),
                        )
        except psycopg.Error as e:
            raise PersistenceError(f"Database error marking event failed: {e}") from e

    def increment_retry(self, event_id: str) -> None:
        """Increment retry count for an event.

        Args:
            event_id: Event ID to increment.

        Raises:
            PersistenceError: If update fails.
        """
        try:
            with self._pool.connection() as conn:
                with conn.transaction():
                    with conn.cursor() as cur:
                        cur.execute(
                            """
                            UPDATE event_outbox
                            SET retry_count = retry_count + 1
                            WHERE event_id = %s;
                            """,
                            (event_id,),
                        )
        except psycopg.Error as e:
            raise PersistenceError(f"Database error incrementing retry count: {e}") from e

    def list_events(
        self,
        investigation_id: str,
        limit: int = 200,
        after_cursor: Optional[Tuple[str, str]] = None,
    ) -> List[OutboxEvent]:
        """Return all durable events for an investigation in chronological order.

        Queries the event_outbox table for all events belonging to the
        investigation, optionally resuming after a keyset cursor
        (created_at, event_id). Results are ordered deterministically by
        (created_at ASC, event_id ASC).

        Args:
            investigation_id: Validated investigation identifier.
            limit: Maximum events to return (default 200).
            after_cursor: Optional (created_at, event_id) keyset cursor to
                retrieve only events strictly after this point in canonical
                order (created_at ASC, event_id ASC).

        Returns:
            List of OutboxEvents in ascending chronological order.

        Raises:
            PersistenceError: If investigation_id is invalid or a database
                error occurs.
        """
        safe_id = validate_investigation_id(investigation_id)

        try:
            with self._pool.connection() as conn:
                with conn.cursor() as cur:
                    if after_cursor is not None:
                        cursor_created_at, cursor_event_id = after_cursor
                        safe_ts = cursor_created_at or "1970-01-01T00:00:00Z"
                        safe_eid = cursor_event_id or ""
                        cur.execute(
                            """
                            SELECT event_id, investigation_id, event_type, payload,
                                   status, created_at, processed_at, retry_count,
                                   error_message
                            FROM event_outbox
                            WHERE investigation_id = %s
                              AND (created_at > %s::timestamptz OR (created_at = %s::timestamptz AND event_id > %s))
                            ORDER BY created_at ASC, event_id ASC
                            LIMIT %s;
                            """,
                            (safe_id, safe_ts, safe_ts, safe_eid, limit),
                        )
                    else:
                        cur.execute(
                            """
                            SELECT event_id, investigation_id, event_type, payload,
                                   status, created_at, processed_at, retry_count,
                                   error_message
                            FROM event_outbox
                            WHERE investigation_id = %s
                            ORDER BY created_at ASC, event_id ASC
                            LIMIT %s;
                            """,
                            (safe_id, limit),
                        )
                    rows = cur.fetchall()
                    events: List[OutboxEvent] = []
                    for row in rows:
                        events.append(
                            OutboxEvent(
                                event_id=row[0],
                                investigation_id=row[1],
                                event_type=row[2],
                                payload=dict(row[3]) if isinstance(row[3], dict) else row[3],
                                status=row[4],
                                created_at=row[5].isoformat() if row[5] else "",
                                processed_at=row[6].isoformat() if row[6] else None,
                                retry_count=row[7],
                                error_message=row[8],
                            )
                        )
                    return events
        except psycopg.Error as e:
            raise PersistenceError(
                f"Database error listing events for investigation '{safe_id}': {e}"
            ) from e
