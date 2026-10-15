"""Tests for transactional outbox persistence (Mission 11)."""

from __future__ import annotations

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
