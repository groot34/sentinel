"""Tests for transactional outbox persistence (Mission 11)."""

from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta, timezone
from typing import List
from unittest.mock import MagicMock, patch

import pytest

from core.events.events import InvestigationStarted, StageCompleted
from core.persistence.outbox import OutboxEvent, OutboxRepository
from core.persistence.postgres import PostgresRepository


class MockOutboxRepository(OutboxRepository):
    """In-memory mock outbox for testing."""

    def __init__(self) -> None:
        self._events: List[OutboxEvent] = []

    def save_event(self, event) -> None:
        self._events.append(OutboxEvent.from_domain_event(event))

    def save_events(self, events) -> None:
        for event in events:
            self._events.append(OutboxEvent.from_domain_event(event))

    def fetch_pending(self, limit: int = 100) -> List[OutboxEvent]:
        return [e for e in self._events if e.status == "pending"][:limit]

    def claim_pending(self, worker_id: str, limit: int = 100, claim_ttl_seconds: float = 300.0) -> List[OutboxEvent]:
        claimed: List[OutboxEvent] = []
        for e in self._events:
            if len(claimed) >= limit:
                break
            if e.status == "pending":
                e.status = "processing"
                claimed.append(e)
        return claimed

    def release_expired_claims(self) -> int:
        released = 0
        for e in self._events:
            if e.status == "processing":
                e.status = "pending"
                released += 1
        return released

    def schedule_retry(self, event_id, next_attempt_at, error_message) -> None:
        for e in self._events:
            if e.event_id == event_id:
                e.status = "pending"
                e.retry_count += 1
                e.error_message = error_message

    def mark_processed(self, event_id: str) -> None:
        for e in self._events:
            if e.event_id == event_id:
                e.status = "processed"
                e.processed_at = "2024-01-01T00:00:00Z"

    def mark_failed(self, event_id: str, error_message: str) -> None:
        for e in self._events:
            if e.event_id == event_id:
                e.status = "failed"
                e.error_message = error_message

    def increment_retry(self, event_id: str) -> None:
        for e in self._events:
            if e.event_id == event_id:
                e.retry_count += 1


def test_outbox_event_from_domain_event():
    """Convert DomainEvent to OutboxEvent."""
    domain_event = InvestigationStarted(
        investigation_id="inc_01",
        initial_stage="logs",
    )
    outbox_event = OutboxEvent.from_domain_event(domain_event)

    assert outbox_event.event_id == domain_event.event_id
    assert outbox_event.investigation_id == domain_event.investigation_id
    assert outbox_event.event_type == domain_event.event_type
    assert outbox_event.payload == domain_event.model_dump()
    assert outbox_event.status == "pending"
    assert outbox_event.created_at == domain_event.timestamp


def test_mock_outbox_save_and_fetch():
    """Mock outbox saves and fetches pending events."""
    repo = MockOutboxRepository()

    event1 = InvestigationStarted(investigation_id="inc_01", initial_stage="logs")
    event2 = StageCompleted(
        investigation_id="inc_01",
        stage="logs",
        llm_calls=1,
        prompt_tokens=100,
        completion_tokens=25,
        total_tokens=125,
    )

    repo.save_events([event1, event2])

    pending = repo.fetch_pending()
    assert len(pending) == 2
    assert pending[0].event_type == "InvestigationStarted"
    assert pending[1].event_type == "StageCompleted"


def test_mock_outbox_mark_processed():
    """Mock outbox marks event as processed."""
    repo = MockOutboxRepository()

    event = InvestigationStarted(investigation_id="inc_01", initial_stage="logs")
    repo.save_event(event)

    repo.mark_processed(event.event_id)

    pending = repo.fetch_pending()
    assert len(pending) == 0


def test_mock_outbox_mark_failed():
    """Mock outbox marks event as failed."""
    repo = MockOutboxRepository()

    event = InvestigationStarted(investigation_id="inc_01", initial_stage="logs")
    repo.save_event(event)

    repo.mark_failed(event.event_id, "Delivery failed")

    pending = repo.fetch_pending()
    assert len(pending) == 0

    failed = [e for e in repo._events if e.status == "failed"]
    assert len(failed) == 1
    assert failed[0].error_message == "Delivery failed"


def test_mock_outbox_increment_retry():
    """Mock outbox increments retry count."""
    repo = MockOutboxRepository()

    event = InvestigationStarted(investigation_id="inc_01", initial_stage="logs")
    repo.save_event(event)

    repo.increment_retry(event.event_id)

    pending = repo.fetch_pending()
    assert len(pending) == 1
    assert pending[0].retry_count == 1


from tests.test_postgres_persistence import PG_URL, POSTGRES_AVAILABLE
from core.persistence.postgres import PostgresRepository
from core.domain.state import InvestigationState


@pytest.mark.skipif(
    not POSTGRES_AVAILABLE,
    reason="Requires PostgreSQL connection at " + PG_URL,
)
def test_postgres_outbox_save_unique_event_id():
    """PostgreSQL outbox enforces unique event_id constraint."""
    with PostgresRepository(PG_URL) as repo:
        repo.initialize_schema()
        event = InvestigationStarted(investigation_id="int_test_outbox_uniq", initial_stage="logs")
        try:
            repo.save_event(event)
            # Idempotent repeat save of same event_id
            repo.save_event(event)
            pending = repo.fetch_pending()
            matching = [e for e in pending if e.event_id == event.event_id]
            assert len(matching) == 1
        finally:
            with repo.pool.connection() as conn:
                with conn.transaction():
                    with conn.cursor() as cur:
                        cur.execute("DELETE FROM event_outbox WHERE investigation_id = 'int_test_outbox_uniq';")


@pytest.mark.skipif(
    not POSTGRES_AVAILABLE,
    reason="Requires PostgreSQL connection at " + PG_URL,
)
def test_postgres_atomic_state_and_event_persistence():
    """State and events are persisted atomically in same transaction."""
    inv_id = "int_test_outbox_atomic"
    with PostgresRepository(PG_URL) as repo:
        repo.initialize_schema()
        state = InvestigationState(incident_id=inv_id)
        state.start_stage("logs")
        state.complete_stage("logs", output={"stage": "logs"})
        event = InvestigationStarted(investigation_id=inv_id, initial_stage="logs")

        try:
            repo.save(state, events=[event])

            loaded = repo.load(inv_id)
            assert loaded is not None
            assert loaded.version == 1

            pending = repo.fetch_pending()
            matching = [e for e in pending if e.investigation_id == inv_id]
            assert len(matching) == 1
            assert matching[0].event_id == event.event_id
        finally:
            with repo.pool.connection() as conn:
                with conn.transaction():
                    with conn.cursor() as cur:
                        cur.execute("DELETE FROM investigations WHERE investigation_id = %s;", (inv_id,))
                        cur.execute("DELETE FROM event_outbox WHERE investigation_id = %s;", (inv_id,))


# ============================================================================
# Mission 16: concurrent & crash-safe outbox delivery (live PostgreSQL)
# ============================================================================


def _clean_live_records(inv_id: str) -> None:
    """Best-effort removal of live test rows for an investigation."""
    try:
        with PostgresRepository(PG_URL) as repo:
            with repo.pool.connection() as conn:
                with conn.transaction():
                    with conn.cursor() as cur:
                        cur.execute("DELETE FROM investigations WHERE investigation_id = %s;", (inv_id,))
                        cur.execute("DELETE FROM event_outbox WHERE investigation_id = %s;", (inv_id,))
    except Exception:
        pass


@pytest.mark.skipif(
    not POSTGRES_AVAILABLE,
    reason="Requires PostgreSQL connection at " + PG_URL,
)
def test_postgres_concurrent_workers_claim_disjoint_event_sets():
    """Two independent workers claiming concurrently never own the same event."""
    inv_id = "int_test_claim_concurrent"
    total = 20
    per_worker = 10
    _clean_live_records(inv_id)

    try:
        created_ids = set()
        with PostgresRepository(PG_URL) as repo:
            repo.initialize_schema()
            for _ in range(total):
                event = InvestigationStarted(investigation_id=inv_id, initial_stage="logs")
                repo.save_event(event)
                created_ids.add(event.event_id)

        barrier = threading.Barrier(2)
        claimed_batches: List[List[OutboxEvent]] = [[], []]

        def _claim(index: int) -> None:
            # Independent pool/connection per worker, as separate processes would use.
            with PostgresRepository(PG_URL) as worker_repo:
                barrier.wait(timeout=10)
                claimed_batches[index] = worker_repo.claim_pending(
                    worker_id=f"worker-{index}",
                    limit=per_worker,
                    claim_ttl_seconds=60.0,
                )

        threads = [threading.Thread(target=_claim, args=(i,)) for i in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)

        all_claimed = [e.event_id for batch in claimed_batches for e in batch]
        # No event is owned by both workers.
        assert len(all_claimed) == len(set(all_claimed))
        # Each worker filled its own quota, proving the 20 events were split.
        assert [len(batch) for batch in claimed_batches] == [per_worker, per_worker]
        # Together the workers cover every eligible event exactly once.
        ours = {e.event_id for batch in claimed_batches for e in batch if e.investigation_id == inv_id}
        assert ours == created_ids
    finally:
        _clean_live_records(inv_id)


@pytest.mark.skipif(
    not POSTGRES_AVAILABLE,
    reason="Requires PostgreSQL connection at " + PG_URL,
)
def test_postgres_claim_excludes_future_retry_and_completed_events():
    """Future retry scheduling and completed events are ineligible for claiming."""
    inv_id = "int_test_claim_eligibility"
    _clean_live_records(inv_id)

    try:
        with PostgresRepository(PG_URL) as repo:
            repo.initialize_schema()
            event = InvestigationStarted(investigation_id=inv_id, initial_stage="logs")
            repo.save_event(event)

            now = datetime.now(timezone.utc)

            # Scheduled in the future -> not eligible.
            repo.schedule_retry(event.event_id, now + timedelta(hours=1), "deferred")
            claimed = [
                e for e in repo.claim_pending("worker-a", limit=1000) if e.investigation_id == inv_id
            ]
            assert claimed == []

            # Scheduled in the past -> eligible again.
            repo.schedule_retry(event.event_id, now - timedelta(seconds=1), "retry now")
            claimed = [
                e for e in repo.claim_pending("worker-a", limit=1000) if e.investigation_id == inv_id
            ]
            assert [e.event_id for e in claimed] == [event.event_id]
            assert claimed[0].retry_count == 2

            # A completed event can never be reclaimed.
            repo.mark_processed(event.event_id)
            assert [
                e for e in repo.claim_pending("worker-b", limit=1000) if e.investigation_id == inv_id
            ] == []
    finally:
        _clean_live_records(inv_id)


@pytest.mark.skipif(
    not POSTGRES_AVAILABLE,
    reason="Requires PostgreSQL connection at " + PG_URL,
)
def test_postgres_release_expired_claims_recovers_after_worker_crash():
    """An abandoned claim past its visibility timeout is recovered for redelivery."""
    inv_id = "int_test_claim_recovery"
    _clean_live_records(inv_id)

    try:
        with PostgresRepository(PG_URL) as repo:
            repo.initialize_schema()
            event = InvestigationStarted(investigation_id=inv_id, initial_stage="logs")
            repo.save_event(event)

            # Claim with a very short visibility timeout, then "crash" (never finalise).
            claimed = [
                e
                for e in repo.claim_pending("crashed-worker", limit=1000, claim_ttl_seconds=0.5)
                if e.investigation_id == inv_id
            ]
            assert [e.event_id for e in claimed] == [event.event_id]

            # Still held: a second worker cannot claim it before expiry.
            assert [
                e for e in repo.claim_pending("worker-b", limit=1000) if e.investigation_id == inv_id
            ] == []

            # Sleep strictly beyond the visibility timeout so the claim is abandoned.
            time.sleep(1.0)

            assert repo.release_expired_claims() >= 1
            reclaimed = [
                e for e in repo.claim_pending("worker-b", limit=1000) if e.investigation_id == inv_id
            ]
            assert [e.event_id for e in reclaimed] == [event.event_id]
    finally:
        _clean_live_records(inv_id)


@pytest.mark.skipif(
    not POSTGRES_AVAILABLE,
    reason="Requires PostgreSQL connection at " + PG_URL,
)
def test_postgres_claim_deterministic_order_for_tied_timestamps():
    """Events sharing a created_at are claimed in ascending event_id order."""
    inv_id = "int_test_claim_ordering"
    _clean_live_records(inv_id)
    tied_ts = datetime.now(timezone.utc)
    # Deliberately insert in reverse event_id order to prove the ORDER BY matters.
    early_id = "evt_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    late_id = "evt_zzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzz"

    try:
        with PostgresRepository(PG_URL) as repo:
            repo.initialize_schema()
            with repo.pool.connection() as conn:
                with conn.transaction():
                    with conn.cursor() as cur:
                        for eid in (late_id, early_id):
                            cur.execute(
                                """
                                INSERT INTO event_outbox (
                                    event_id, investigation_id, event_type, payload,
                                    status, created_at, retry_count
                                ) VALUES (%s, %s, %s, %s, 'pending', %s, 0);
                                """,
                                (eid, inv_id, "InvestigationStarted", "{}", tied_ts),
                            )

            claimed = [
                e for e in repo.claim_pending("worker-order", limit=1000) if e.investigation_id == inv_id
            ]
            assert [e.event_id for e in claimed] == [early_id, late_id]
    finally:
        _clean_live_records(inv_id)


@pytest.mark.skipif(
    not POSTGRES_AVAILABLE,
    reason="Requires PostgreSQL connection at " + PG_URL,
)
def test_postgres_duplicate_event_id_does_not_abort_state_and_events():
    """A duplicate event ID is skipped without aborting the valid state change."""
    inv_id = "int_test_dup_event"
    _clean_live_records(inv_id)

    try:
        with PostgresRepository(PG_URL) as repo:
            repo.initialize_schema()

            duplicate = InvestigationStarted(investigation_id=inv_id, initial_stage="logs")
            repo.save_event(duplicate)  # first insert of this event_id

            state = InvestigationState(incident_id=inv_id)
            state.start_stage("logs")
            state.complete_stage("logs", output={"stage": "logs"})
            fresh_event = StageCompleted(investigation_id=inv_id, stage="logs", total_tokens=1)

            # The duplicate would previously abort the whole transaction.
            repo.save(state, events=[duplicate, fresh_event])

            loaded = repo.load(inv_id)
            assert loaded is not None
            assert loaded.version == 1
            assert "logs" in loaded.stages

            ids = {
                e.event_id
                for e in repo.fetch_pending(limit=1000)
                if e.investigation_id == inv_id
            }
            assert duplicate.event_id in ids
            assert fresh_event.event_id in ids
    finally:
        _clean_live_records(inv_id)


@pytest.mark.skipif(
    not POSTGRES_AVAILABLE,
    reason="Requires PostgreSQL connection at " + PG_URL,
)
def test_postgres_mark_failed_retains_error_and_excludes_from_claims():
    """Terminal failure retains the last error and is never claimed again."""
    inv_id = "int_test_terminal_failure"
    _clean_live_records(inv_id)

    try:
        with PostgresRepository(PG_URL) as repo:
            repo.initialize_schema()
            event = InvestigationStarted(investigation_id=inv_id, initial_stage="logs")
            repo.save_event(event)

            repo.mark_failed(event.event_id, "Max retry attempts (5) exceeded: boom")

            history = [
                e for e in repo.list_events(inv_id, limit=10) if e.event_id == event.event_id
            ]
            assert len(history) == 1
            assert history[0].status == "failed"
            assert history[0].error_message == "Max retry attempts (5) exceeded: boom"

            assert [
                e for e in repo.claim_pending("worker-x", limit=1000) if e.investigation_id == inv_id
            ] == []
    finally:
        _clean_live_records(inv_id)
