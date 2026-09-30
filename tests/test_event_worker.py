"""Tests for event delivery worker (Mission 11)."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from api.event_worker.worker import EventDeliveryWorker
from core.events.events import InvestigationStarted, StageCompleted
from core.persistence.outbox import OutboxEvent


class MockOutboxRepository:
    """Mock outbox repository for worker testing."""

    def __init__(self):
        self._events = []
        self._processed = set()
        self._failed = {}

    def save_event(self, event):
        pass

    def fetch_pending(self, limit=100):
        return [e for e in self._events if e.event_id not in self._processed and e.event_id not in self._failed][:limit]

    def mark_processed(self, event_id):
        self._processed.add(event_id)

    def mark_failed(self, event_id, error_message):
        self._failed[event_id] = error_message

    def increment_retry(self, event_id):
        for e in self._events:
            if e.event_id == event_id:
                e.retry_count += 1


def test_worker_delivers_single_event():
    """Worker delivers a single pending event to EventBus."""
    repo = MockOutboxRepository()
    event_bus = MagicMock()

    event = InvestigationStarted(investigation_id="inc_01", initial_stage="logs")
    outbox_event = OutboxEvent.from_domain_event(event)
    repo._events.append(outbox_event)

    worker = EventDeliveryWorker(repository=repo, event_bus=event_bus, poll_interval=0.01)
    worker._process_batch()

    assert event_bus.publish.call_count == 1
    assert outbox_event.event_id in repo._processed


def test_worker_delivers_multiple_events():
    """Worker delivers multiple pending events in order."""
    repo = MockOutboxRepository()
    event_bus = MagicMock()

    event1 = InvestigationStarted(investigation_id="inc_01", initial_stage="logs")
    event2 = StageCompleted(
        investigation_id="inc_01",
        stage="logs",
        llm_calls=1,
        prompt_tokens=100,
        completion_tokens=25,
        total_tokens=125,
    )

    repo._events.append(OutboxEvent.from_domain_event(event1))
    repo._events.append(OutboxEvent.from_domain_event(event2))

    worker = EventDeliveryWorker(repository=repo, event_bus=event_bus, poll_interval=0.01)
    worker._process_batch()

    assert event_bus.publish.call_count == 2
    assert len(repo._processed) == 2


def test_worker_skips_processed_events():
    """Worker does not re-deliver already processed events."""
    repo = MockOutboxRepository()
    event_bus = MagicMock()

    event = InvestigationStarted(investigation_id="inc_01", initial_stage="logs")
    outbox_event = OutboxEvent.from_domain_event(event)
    repo._events.append(outbox_event)
    repo._processed.add(outbox_event.event_id)

    worker = EventDeliveryWorker(repository=repo, event_bus=event_bus, poll_interval=0.01)
    worker._process_batch()

    assert event_bus.publish.call_count == 0


def test_worker_handles_delivery_failure():
    """Worker increments retry count on delivery failure."""
    repo = MockOutboxRepository()
    event_bus = MagicMock()

    event = InvestigationStarted(investigation_id="inc_01", initial_stage="logs")
    outbox_event = OutboxEvent.from_domain_event(event)
    repo._events.append(outbox_event)

    event_bus.publish.side_effect = RuntimeError("Delivery failed")

    worker = EventDeliveryWorker(repository=repo, event_bus=event_bus, poll_interval=0.01)
    worker._process_batch()

    assert outbox_event.retry_count == 1
    assert outbox_event.event_id not in repo._processed


def test_worker_marks_failed_after_max_retries():
    """Worker marks event as failed after max retry attempts."""
    repo = MockOutboxRepository()
    event_bus = MagicMock()

    event = InvestigationStarted(investigation_id="inc_01", initial_stage="logs")
    outbox_event = OutboxEvent.from_domain_event(event)
    outbox_event.retry_count = 4  # One short of max
    repo._events.append(outbox_event)

    event_bus.publish.side_effect = RuntimeError("Delivery failed")

    worker = EventDeliveryWorker(repository=repo, event_bus=event_bus, poll_interval=0.01)
    worker._process_batch()

    assert outbox_event.event_id in repo._failed
    assert "Max retry attempts" in repo._failed[outbox_event.event_id]


def test_worker_idempotent_delivery():
    """Worker does not deliver same event twice if already processed."""
    repo = MockOutboxRepository()
    event_bus = MagicMock()

    event = InvestigationStarted(investigation_id="inc_01", initial_stage="logs")
    outbox_event = OutboxEvent.from_domain_event(event)
    repo._events.append(outbox_event)

    worker = EventDeliveryWorker(repository=repo, event_bus=event_bus, poll_interval=0.01)

    # First delivery
    worker._process_batch()
    assert event_bus.publish.call_count == 1

    # Second delivery (should skip)
    worker._process_batch()
    assert event_bus.publish.call_count == 1  # No additional calls


def test_worker_respects_batch_size():
    """Worker respects batch size limit when fetching events."""
    repo = MockOutboxRepository()
    event_bus = MagicMock()

    for i in range(10):
        event = InvestigationStarted(investigation_id=f"inc_{i}", initial_stage="logs")
        repo._events.append(OutboxEvent.from_domain_event(event))

    worker = EventDeliveryWorker(repository=repo, event_bus=event_bus, batch_size=3, poll_interval=0.01)
    worker._process_batch()

    assert event_bus.publish.call_count == 3
