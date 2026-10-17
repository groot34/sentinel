"""Tests for bounded transient PostgreSQL failure recovery (Mission 15).

Offline tests exercise the retry engine, error classification, and repository
wiring with deterministic fault injection and no database (and no real sleeps).
Live PostgreSQL 16 integration tests are gated on SENTINEL_DATABASE_URL and skip
gracefully when no database is reachable.
"""

from __future__ import annotations

import os
import random
from unittest.mock import MagicMock

import psycopg
import pytest

from core.domain.models import IncidentStatus
from core.domain.state import InvestigationState
from core.events.events import InvestigationStarted
from core.persistence.postgres import PostgresRepository
from core.persistence.repository import ConcurrencyError, PersistenceError
from core.persistence.retry import (
    DEFAULT_RETRYABLE_SQLSTATES,
    RetryExhaustedError,
    RetryPolicy,
    compute_backoff_delay,
    inject_transient_fault,
    is_retryable_pg_error,
    maybe_inject_fault,
    policy_from_env,
    run_with_retry,
)

PG_URL = os.getenv(
    "SENTINEL_DATABASE_URL",
    "postgresql://sentinel:sentinel_dev_password@localhost:5432/sentinel_db",
)


def _pg_available(url: str) -> bool:
    """Check if a live PostgreSQL instance is reachable."""
    try:
        with psycopg.connect(url, connect_timeout=1) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1;")
                return cur.fetchone() == (1,)
    except Exception:
        return False


POSTGRES_AVAILABLE = _pg_available(PG_URL)


# ============================================================================
# OFFLINE: error classification, policy, bounded retry engine
# ============================================================================

class TestRetryClassification:
    """SQLSTATE classification: only transient contention errors are retryable."""

    def test_only_transient_sqlstates_are_retryable(self):
        assert is_retryable_pg_error(psycopg.errors.SerializationFailure("x")) is True
        assert is_retryable_pg_error(psycopg.errors.DeadlockDetected("x")) is True
        assert is_retryable_pg_error(psycopg.errors.UniqueViolation("x")) is False
        assert is_retryable_pg_error(ValueError("nope")) is False
        assert DEFAULT_RETRYABLE_SQLSTATES == {"40001", "40P01"}

    def test_wrapped_transient_error_is_retryable(self):
        """Repository-wrapped errors are detected via the __cause__ chain."""
        try:
            try:
                raise psycopg.errors.SerializationFailure("40001")
            except psycopg.errors.SerializationFailure as inner:
                raise PersistenceError("wrapped") from inner
        except PersistenceError as wrapped:
            assert is_retryable_pg_error(wrapped) is True

    def test_occ_concurrency_error_is_not_retryable(self):
        assert is_retryable_pg_error(ConcurrencyError("optimistic conflict")) is False

    def test_policy_from_env_clamps_and_never_raises(self, monkeypatch):
        monkeypatch.setenv("SENTINEL_PG_RETRY_MAX_ATTEMPTS", "0")
        monkeypatch.setenv("SENTINEL_PG_RETRY_MAX_DELAY", "9999")
        monkeypatch.setenv("SENTINEL_PG_RETRY_BASE_DELAY", "not-a-number")
        policy = policy_from_env()
        assert policy.max_attempts == 1
        assert policy.max_delay_seconds == 30.0
        assert policy.base_delay_seconds == 0.05

    def test_policy_from_env_defaults(self, monkeypatch):
        for var in (
            "SENTINEL_PG_RETRY_MAX_ATTEMPTS",
            "SENTINEL_PG_RETRY_BASE_DELAY",
            "SENTINEL_PG_RETRY_MAX_DELAY",
        ):
            monkeypatch.delenv(var, raising=False)
        policy = policy_from_env()
        assert policy.max_attempts == 5
        assert policy.base_delay_seconds == 0.05
        assert policy.max_delay_seconds == 1.0


class TestRunWithRetryOffline:
    """Deterministic engine behaviour with injected sleep/rng (no real waits)."""

    def test_transient_failure_then_success(self):
        calls = {"n": 0}
        delays = []

        def op(attempt):
            calls["n"] += 1
            if calls["n"] < 3:
                raise psycopg.errors.SerializationFailure("40001")
            return "ok"

        result = run_with_retry(
            op,
            policy=RetryPolicy(max_attempts=5),
            sleep=delays.append,
            rng=random.Random(0),
        )
        assert result == "ok"
        assert calls["n"] == 3          # two transient failures, then success
        assert len(delays) == 2         # one backoff between each pair

    def test_deadlock_then_success(self):
        calls = {"n": 0}

        def op(attempt):
            calls["n"] += 1
            if calls["n"] == 1:
                raise psycopg.errors.DeadlockDetected("40P01")
            return "ok"

        result = run_with_retry(
            op, policy=RetryPolicy(max_attempts=3), sleep=lambda _: None, rng=random.Random(0)
        )
        assert result == "ok"
        assert calls["n"] == 2

    def test_exhausted_budget_raises_typed_error(self):
        calls = {"n": 0}

        def op(attempt):
            calls["n"] += 1
            raise psycopg.errors.SerializationFailure("40001")

        with pytest.raises(RetryExhaustedError) as excinfo:
            run_with_retry(
                op, policy=RetryPolicy(max_attempts=3), sleep=lambda _: None, rng=random.Random(0)
            )
        assert calls["n"] == 3
        # Subclass of PersistenceError so the API maps it to 503, not 500.
        assert isinstance(excinfo.value, PersistenceError)
        assert isinstance(excinfo.value.__cause__, psycopg.errors.SerializationFailure)

    def test_permanent_error_is_not_retried(self):
        calls = {"n": 0}

        def op(attempt):
            calls["n"] += 1
            raise psycopg.errors.UniqueViolation("23505")

        with pytest.raises(psycopg.errors.UniqueViolation):
            run_with_retry(
                op, policy=RetryPolicy(max_attempts=5), sleep=lambda _: None, rng=random.Random(0)
            )
        assert calls["n"] == 1

    def test_occ_conflict_propagates_without_retry(self):
        calls = {"n": 0}

        def op(attempt):
            calls["n"] += 1
            raise ConcurrencyError("occ")

        with pytest.raises(ConcurrencyError):
            run_with_retry(
                op, policy=RetryPolicy(max_attempts=5), sleep=lambda _: None, rng=random.Random(0)
            )
        assert calls["n"] == 1

    def test_backoff_is_bounded_and_jittered(self):
        policy = RetryPolicy(max_attempts=6, base_delay_seconds=0.05, max_delay_seconds=1.0)
        rng = random.Random(42)
        observed = {round(compute_backoff_delay(i, policy, rng), 6) for i in range(1, 7)}
        assert len(observed) > 1  # jitter produces varying delays
        # Ceiling is honoured well beyond the exponential cap.
        for i in range(1, 20):
            assert 0.0 <= compute_backoff_delay(i, policy, rng) <= 1.0

    def test_zero_base_delay_yields_zero(self):
        policy = RetryPolicy(base_delay_seconds=0.0)
        assert compute_backoff_delay(1, policy, random.Random(0)) == 0.0

    def test_backoff_grows_exponentially_until_cap(self):
        policy = RetryPolicy(base_delay_seconds=0.01, max_delay_seconds=0.08)
        # upper bounds: 0.01, 0.02, 0.04, 0.08 (cap), 0.08 ...
        for idx, ceiling in ((1, 0.01), (2, 0.02), (3, 0.04), (4, 0.08), (5, 0.08)):
            assert 0.0 <= compute_backoff_delay(idx, policy, random.Random(1)) <= ceiling

    def test_fault_seam_is_noop_outside_context(self):
        # No hook installed in production: operation runs unimpeded.
        assert run_with_retry(lambda attempt: 7, sleep=lambda _: None) == 7

    def test_inject_transient_fault_applies_and_clears(self):
        def hook(attempt):
            raise psycopg.errors.SerializationFailure("injected")

        # The seam is inert with no hook installed.
        maybe_inject_fault(1)

        with inject_transient_fault(hook):
            with pytest.raises(psycopg.errors.SerializationFailure):
                maybe_inject_fault(1)

        # After the context exits the seam is inert again.
        maybe_inject_fault(1)


# ============================================================================
# OFFLINE: repository retry wiring (mock pool, deterministic fault injection)
# ============================================================================

class TestRepositoryRetryWiring:
    """Verify save() retries the whole transaction and preserves OCC/versioning."""

    def test_save_retries_entire_transaction_then_commits(self, monkeypatch):
        repo = PostgresRepository(MagicMock(), retry_policy=RetryPolicy(max_attempts=4))
        attempts = {"n": 0}

        def fake_transaction(state, events, attempt):
            attempts["n"] += 1
            if attempt < 3:
                # Wrapped exactly as the real repository wraps a psycopg error.
                raise PersistenceError("boom") from psycopg.errors.SerializationFailure("40001")
            state.version = state.version + 1

        monkeypatch.setattr(repo, "_save_transaction", fake_transaction)

        state = InvestigationState(incident_id="retry_offline_1", status=IncidentStatus.RUNNING)
        repo.save(state)
        assert attempts["n"] == 3
        assert state.version == 2

    def test_save_restores_entry_version_on_exhaustion(self, monkeypatch):
        repo = PostgresRepository(MagicMock(), retry_policy=RetryPolicy(max_attempts=2))

        def fake_transaction(state, events, attempt):
            raise PersistenceError("boom") from psycopg.errors.SerializationFailure("40001")

        monkeypatch.setattr(repo, "_save_transaction", fake_transaction)

        state = InvestigationState(incident_id="retry_offline_2", status=IncidentStatus.RUNNING)
        with pytest.raises(RetryExhaustedError):
            repo.save(state)
        assert state.version == 1  # entry version restored

    def test_save_resets_version_before_each_attempt(self, monkeypatch):
        """Every attempt must observe the same entry version (no OCC drift)."""
        repo = PostgresRepository(MagicMock(), retry_policy=RetryPolicy(max_attempts=3))
        seen_versions = []

        def fake_transaction(state, events, attempt):
            seen_versions.append(state.version)
            state.version = state.version + 1  # simulate a partially applied update
            if attempt < 3:
                raise PersistenceError("boom") from psycopg.errors.SerializationFailure("40001")

        monkeypatch.setattr(repo, "_save_transaction", fake_transaction)

        state = InvestigationState(incident_id="retry_offline_4", status=IncidentStatus.RUNNING)
        repo.save(state)
        assert seen_versions == [1, 1, 1]
        assert state.version == 2

    def test_save_never_retries_occ_conflict(self, monkeypatch):
        repo = PostgresRepository(MagicMock(), retry_policy=RetryPolicy(max_attempts=5))
        calls = {"n": 0}

        def fake_transaction(state, events, attempt):
            calls["n"] += 1
            raise ConcurrencyError("optimistic conflict")

        monkeypatch.setattr(repo, "_save_transaction", fake_transaction)

        state = InvestigationState(incident_id="retry_offline_3", status=IncidentStatus.RUNNING)
        with pytest.raises(ConcurrencyError):
            repo.save(state)
        assert calls["n"] == 1

    def test_save_rejects_non_investigation_state(self):
        repo = PostgresRepository(MagicMock(), retry_policy=RetryPolicy())
        with pytest.raises(PersistenceError, match="only InvestigationState"):
            repo.save({"incident_id": "x"})  # type: ignore[arg-type]

    def test_save_does_not_retry_permanent_wrapped_error(self, monkeypatch):
        repo = PostgresRepository(MagicMock(), retry_policy=RetryPolicy(max_attempts=5))
        calls = {"n": 0}

        def fake_transaction(state, events, attempt):
            calls["n"] += 1
            raise PersistenceError("boom") from psycopg.errors.UniqueViolation("23505")

        monkeypatch.setattr(repo, "_save_transaction", fake_transaction)

        state = InvestigationState(incident_id="retry_offline_5", status=IncidentStatus.RUNNING)
        with pytest.raises(PersistenceError):
            repo.save(state)
        assert calls["n"] == 1


# ============================================================================
# LIVE POSTGRESQL 16 INTEGRATION
# ============================================================================

@pytest.mark.skipif(not POSTGRES_AVAILABLE, reason="Live PostgreSQL not available at " + PG_URL)
class TestPostgresRetryIntegration:
    """Live integration: fault injection at the real PostgreSQL tx boundary."""

    def _clean(self, inv_id: str) -> None:
        try:
            with psycopg.connect(PG_URL, connect_timeout=2) as conn:
                with conn.transaction():
                    with conn.cursor() as cur:
                        cur.execute(
                            "DELETE FROM event_outbox WHERE investigation_id = %s;", (inv_id,)
                        )
                        cur.execute(
                            "DELETE FROM investigations WHERE investigation_id = %s;", (inv_id,)
                        )
        except Exception:
            pass

    def test_injected_40001_then_real_commit(self):
        """A transient 40001 on attempt 1 is retried and the transaction commits."""
        inv_id = "retry_live_40001"
        self._clean(inv_id)
        with PostgresRepository(PG_URL) as repo:
            repo.initialize_schema()
            state = InvestigationState(incident_id=inv_id, status=IncidentStatus.RUNNING)
            state.start_stage("logs")
            event = InvestigationStarted(investigation_id=inv_id, initial_stage="logs")
            calls = {"n": 0}

            def hook(attempt):
                calls["n"] += 1
                if attempt == 1:
                    raise psycopg.errors.SerializationFailure("injected-once")

            with inject_transient_fault(hook):
                repo.save(state, events=[event])

            assert calls["n"] >= 2  # first attempt injected, retry committed
            loaded = repo.load(inv_id)
            assert loaded is not None
            assert loaded.version == 1
            # Exactly one event persisted — no duplicates across attempts.
            pending = [e for e in repo.fetch_pending() if e.investigation_id == inv_id]
            assert len(pending) == 1
            assert pending[0].event_id == event.event_id
        self._clean(inv_id)

    def test_exhausted_retries_leave_no_partial_state(self):
        """Exhausted retries roll back cleanly: no state row, no orphaned events."""
        inv_id = "retry_live_exhausted"
        self._clean(inv_id)
        with PostgresRepository(PG_URL, retry_policy=RetryPolicy(max_attempts=2)) as repo:
            repo.initialize_schema()
            state = InvestigationState(incident_id=inv_id, status=IncidentStatus.RUNNING)
            event = InvestigationStarted(investigation_id=inv_id, initial_stage="logs")

            def always_fail(attempt):
                raise psycopg.errors.SerializationFailure("always")

            with inject_transient_fault(always_fail):
                with pytest.raises(RetryExhaustedError):
                    repo.save(state, events=[event])

            assert repo.load(inv_id) is None
            assert state.version == 1  # entry version restored on exhaustion
            assert [e for e in repo.fetch_pending() if e.investigation_id == inv_id] == []
        self._clean(inv_id)

    def test_occ_conflict_not_mistaken_for_retryable_error(self):
        """A genuine OCC conflict raises ConcurrencyError, not RetryExhaustedError."""
        inv_id = "retry_live_occ"
        self._clean(inv_id)
        with PostgresRepository(PG_URL, retry_policy=RetryPolicy(max_attempts=5)) as repo:
            repo.initialize_schema()
            base = InvestigationState(incident_id=inv_id, status=IncidentStatus.RUNNING)
            repo.save(base)

            stale = repo.load(inv_id)
            fresh = repo.load(inv_id)
            assert stale is not None and fresh is not None

            fresh.start_stage("logs")
            repo.save(fresh)  # advances to version 2

            stale.start_stage("metrics")
            with pytest.raises(ConcurrencyError):
                repo.save(stale)  # stale version 1 -> conflict, never retried
        self._clean(inv_id)

    def test_state_and_outbox_atomic_under_retry(self):
        """After an injected transient fault, state and its events commit together."""
        inv_id = "retry_live_atomic"
        self._clean(inv_id)
        with PostgresRepository(PG_URL) as repo:
            repo.initialize_schema()
            state = InvestigationState(incident_id=inv_id, status=IncidentStatus.RUNNING)
            state.start_stage("logs")
            state.complete_stage("logs", output={"stage": "logs"})
            event = InvestigationStarted(investigation_id=inv_id, initial_stage="logs")

            def hook(attempt):
                if attempt == 1:
                    raise psycopg.errors.SerializationFailure("injected-once")

            with inject_transient_fault(hook):
                repo.save(state, events=[event])

            loaded = repo.load(inv_id)
            assert loaded is not None
            assert loaded.version == 1
            assert "logs" in loaded.stages
            pending = [e for e in repo.fetch_pending() if e.investigation_id == inv_id]
            assert len(pending) == 1
        self._clean(inv_id)
