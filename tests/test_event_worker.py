"""Tests for event delivery worker (Mission 11)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest

from api.event_worker.worker import EventDeliveryWorker
from core.events.events import InvestigationStarted, StageCompleted
from core.persistence.outbox import OutboxEvent


class MockOutboxRepository:
    """Mock outbox repository implementing the atomic-claim delivery contract."""

    def __init__(self):
        self._events = []
        self._claimed = set()
        self._processed = set()
        self._failed = {}
        self._scheduled = {}
        self._released = 0
        self.release_calls = 0
        self.claim_calls = []

    def save_event(self, event):
        pass

    def release_expired_claims(self):
        self.release_calls += 1
        return self._released

    def claim_pending(self, worker_id, limit=100, claim_ttl_seconds=300.0):
        self.claim_calls.append((worker_id, limit, claim_ttl_seconds))
        claimed = []
        for e in self._events:
            if len(claimed) >= limit:
                break
            if e.event_id in self._processed or e.event_id in self._failed:
                continue
            if e.event_id in self._claimed:
                continue
            self._claimed.add(e.event_id)
            claimed.append(e)
        return claimed

    def mark_processed(self, event_id):
        self._processed.add(event_id)
        self._claimed.discard(event_id)

    def mark_failed(self, event_id, error_message):
        self._failed[event_id] = error_message
        self._claimed.discard(event_id)

    def schedule_retry(self, event_id, next_attempt_at, error_message):
        for e in self._events:
            if e.event_id == event_id:
                e.retry_count += 1
        self._scheduled[event_id] = (next_attempt_at, error_message)
        self._claimed.discard(event_id)

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
    """Worker durably reschedules a failed delivery and increments the attempt count."""
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
    # Durably scheduled for a later attempt rather than terminally failed.
    assert outbox_event.event_id in repo._scheduled
    assert outbox_event.event_id not in repo._failed


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


def test_worker_claims_atomically_with_its_identity():
    """Worker recovers expired claims, then claims with its identity, limit and TTL."""
    repo = MockOutboxRepository()
    event_bus = MagicMock()
    repo._events.append(
        OutboxEvent.from_domain_event(InvestigationStarted(investigation_id="inc_01", initial_stage="logs"))
    )

    worker = EventDeliveryWorker(
        repository=repo,
        event_bus=event_bus,
        worker_id="worker-xyz",
        batch_size=7,
        claim_ttl_seconds=42.0,
        poll_interval=0.01,
    )
    worker._process_batch()

    assert repo.release_calls == 1
    assert repo.claim_calls == [("worker-xyz", 7, 42.0)]
    assert event_bus.publish.call_count == 1


def test_worker_schedules_retry_without_sleeping():
    """A failed delivery is rescheduled in the database, never by sleeping in the batch."""
    repo = MockOutboxRepository()
    event_bus = MagicMock()
    event_bus.publish.side_effect = RuntimeError("boom")

    outbox_event = OutboxEvent.from_domain_event(
        InvestigationStarted(investigation_id="inc_01", initial_stage="logs")
    )
    repo._events.append(outbox_event)

    fixed_now = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    worker = EventDeliveryWorker(
        repository=repo,
        event_bus=event_bus,
        retry_base_delay=2.0,
        retry_max_delay=60.0,
        clock=lambda: fixed_now,
    )

    with patch("api.event_worker.worker.time.sleep") as mock_sleep:
        worker._process_batch()
        mock_sleep.assert_not_called()

    next_attempt_at, error_message = repo._scheduled[outbox_event.event_id]
    # Full jitter: the first-attempt delay lies within [0, base_delay].
    assert fixed_now <= next_attempt_at <= fixed_now + timedelta(seconds=2.0)
    assert "boom" in error_message


def test_worker_continues_batch_after_delivery_failure():
    """One failed event does not block delivery of the rest of the claimed batch."""
    repo = MockOutboxRepository()
    event_bus = MagicMock()

    failing = OutboxEvent.from_domain_event(
        InvestigationStarted(investigation_id="inc_01", initial_stage="logs")
    )
    healthy = OutboxEvent.from_domain_event(
        StageCompleted(investigation_id="inc_01", stage="logs", total_tokens=1)
    )
    repo._events.extend([failing, healthy])

    def _publish(event):
        if event.event_id == failing.event_id:
            raise RuntimeError("boom")

    event_bus.publish.side_effect = _publish

    worker = EventDeliveryWorker(repository=repo, event_bus=event_bus, poll_interval=0.01)
    worker._process_batch()

    assert event_bus.publish.call_count == 2
    assert failing.event_id in repo._scheduled
    assert healthy.event_id in repo._processed


def test_worker_fails_terminally_at_attempt_limit():
    """An event is terminally failed once the attempt limit is reached, retaining the error."""
    repo = MockOutboxRepository()
    event_bus = MagicMock()
    event_bus.publish.side_effect = RuntimeError("fatal")

    outbox_event = OutboxEvent.from_domain_event(
        InvestigationStarted(investigation_id="inc_01", initial_stage="logs")
    )
    repo._events.append(outbox_event)

    worker = EventDeliveryWorker(
        repository=repo, event_bus=event_bus, max_retry_attempts=1, poll_interval=0.01
    )
    worker._process_batch()

    assert outbox_event.event_id in repo._failed
    assert outbox_event.event_id not in repo._scheduled
