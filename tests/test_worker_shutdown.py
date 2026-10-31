"""Mission 22 Task 5: worker signal handling and graceful shutdown.

Covers the shutdown contract of the event delivery worker: SIGTERM/SIGINT
request a clean stop, the loop begins no further batch once a shutdown is
requested, the batch already in flight finishes, an interrupted claim stays
recoverable through claim-TTL expiry, shutdown never marks an undelivered event
as delivered, and repeated shutdown requests are harmless.

The outbox is modelled in memory — including a real claim-TTL sweep — so these
tests need no live PostgreSQL service.
"""

from __future__ import annotations

import signal
import threading
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock

import pytest

from api.event_worker.worker import EventDeliveryWorker, _close_repository
from core.events.events import InvestigationStarted, StageCompleted
from core.persistence.outbox import OutboxEvent
from tests.test_event_worker import MockOutboxRepository


def _event(index: int = 0) -> OutboxEvent:
    """Return a pending outbox event wrapping an InvestigationStarted."""
    return OutboxEvent.from_domain_event(
        InvestigationStarted(investigation_id=f"inc_{index:02d}", initial_stage="logs")
    )


# ===========================================================================
# 1 & 2. SIGTERM / SIGINT request a clean shutdown
# ===========================================================================

def _capture_signal_handlers(monkeypatch: pytest.MonkeyPatch) -> Dict[int, Any]:
    """Replace signal.signal with a recorder and return the handler registry."""
    handlers: Dict[int, Any] = {}
    monkeypatch.setattr(
        signal, "signal", lambda sig, handler: handlers.__setitem__(sig, handler)
    )
    return handlers


def test_sigterm_requests_a_clean_shutdown(monkeypatch: pytest.MonkeyPatch):
    handlers = _capture_signal_handlers(monkeypatch)
    worker = EventDeliveryWorker(repository=MockOutboxRepository(), event_bus=MagicMock())

    worker.install_signal_handlers()

    assert set(handlers) == {signal.SIGTERM, signal.SIGINT}
    assert not worker.shutdown_requested

    handlers[signal.SIGTERM](signal.SIGTERM, None)

    assert worker.shutdown_requested


def test_sigint_requests_a_clean_shutdown(monkeypatch: pytest.MonkeyPatch):
    handlers = _capture_signal_handlers(monkeypatch)
    worker = EventDeliveryWorker(repository=MockOutboxRepository(), event_bus=MagicMock())

    worker.install_signal_handlers()
    handlers[signal.SIGINT](signal.SIGINT, None)

    assert worker.shutdown_requested


def test_install_signal_handlers_tolerates_unavailable_signals(
    monkeypatch: pytest.MonkeyPatch,
):
    """A platform or thread that cannot take the signal must not abort startup."""

    def _refuse(sig, handler):
        raise ValueError("signal only works in the main thread")

    monkeypatch.setattr(signal, "signal", _refuse)
    worker = EventDeliveryWorker(repository=MockOutboxRepository(), event_bus=MagicMock())

    worker.install_signal_handlers()  # must not raise

    assert not worker.shutdown_requested


# ===========================================================================
# 3. No new batch is started after a shutdown request
# ===========================================================================

def test_worker_starts_no_batch_after_shutdown_is_requested():
    repo = MockOutboxRepository()
    repo._events.append(_event())
    worker = EventDeliveryWorker(repository=repo, event_bus=MagicMock(), poll_interval=0.0)

    batches: List[int] = []
    original = worker._process_batch

    def counting_batch() -> None:
        batches.append(1)
        worker.request_shutdown()  # requested while the first batch runs
        original()

    worker._process_batch = counting_batch
    worker.start()

    assert batches == [1]


def test_start_runs_nothing_when_shutdown_already_requested():
    repo = MockOutboxRepository()
    repo._events.append(_event())
    worker = EventDeliveryWorker(repository=repo, event_bus=MagicMock(), poll_interval=0.0)

    worker.request_shutdown()
    worker.start()

    assert repo.claim_calls == []
    assert repo.release_calls == 0


# ===========================================================================
# 4. An in-flight batch finishes safely
# ===========================================================================

def test_in_flight_batch_finishes_after_shutdown_is_requested():
    repo = MockOutboxRepository()
    for i in range(3):
        repo._events.append(_event(i))

    bus = MagicMock()
    worker = EventDeliveryWorker(repository=repo, event_bus=bus, poll_interval=0.0)

    published: List[str] = []

    def publish(event) -> None:
        published.append(event.event_id)
        worker.request_shutdown()  # requested mid-batch

    bus.publish.side_effect = publish
    worker._process_batch()

    assert len(published) == 3
    assert len(repo._processed) == 3


def test_shutdown_interrupts_the_poll_wait():
    """A request during the idle poll wakes the loop without waiting it out."""
    entered_batch = threading.Event()

    class SignallingRepo(MockOutboxRepository):
        def claim_pending(self, *args, **kwargs):
            entered_batch.set()
            return []

    worker = EventDeliveryWorker(
        repository=SignallingRepo(), event_bus=MagicMock(), poll_interval=30.0
    )
    thread = threading.Thread(target=worker.start)
    thread.start()
    try:
        assert entered_batch.wait(timeout=5.0)
        worker.request_shutdown()
        thread.join(timeout=5.0)
        assert not thread.is_alive()
    finally:
        worker.request_shutdown()
        thread.join(timeout=5.0)


# ===========================================================================
# 5. An interrupted claim stays recoverable through TTL expiry
# ===========================================================================

class _Record:
    __slots__ = ("event", "status", "claimed_by", "claim_expires_at")

    def __init__(self, event: OutboxEvent) -> None:
        self.event = event
        self.status = "pending"
        self.claimed_by: Optional[str] = None
        self.claim_expires_at: Optional[datetime] = None


class TtlOutboxRepository:
    """In-memory outbox modelling the claim-TTL sweep, with no live database."""

    def __init__(self, clock) -> None:
        self._clock = clock
        self._records: Dict[str, _Record] = {}
        self.delivered: set = set()

    def add(self, outbox_event: OutboxEvent) -> None:
        self._records[outbox_event.event_id] = _Record(outbox_event)

    def release_expired_claims(self) -> int:
        now = self._clock()
        released = 0
        for record in self._records.values():
            if (
                record.status == "processing"
                and record.claim_expires_at is not None
                and record.claim_expires_at <= now
            ):
                record.status = "pending"
                record.claimed_by = None
                record.claim_expires_at = None
                released += 1
        return released

    def claim_pending(
        self, worker_id: str, limit: int = 100, claim_ttl_seconds: float = 300.0
    ) -> List[OutboxEvent]:
        claimed: List[OutboxEvent] = []
        for record in self._records.values():
            if len(claimed) >= limit:
                break
            if record.status != "pending":
                continue
            record.status = "processing"
            record.claimed_by = worker_id
            record.claim_expires_at = self._clock() + timedelta(seconds=claim_ttl_seconds)
            claimed.append(record.event)
        return claimed

    def mark_processed(self, event_id: str) -> None:
        self._records[event_id].status = "processed"
        self.delivered.add(event_id)

    def mark_failed(self, event_id: str, error_message: str) -> None:
        self._records[event_id].status = "failed"

    def schedule_retry(
        self, event_id: str, next_attempt_at: datetime, error_message: str
    ) -> None:
        record = self._records[event_id]
        record.status = "pending"
        record.claim_expires_at = None
        record.event.retry_count += 1


def test_interrupted_claim_is_recoverable_after_ttl_expiry():
    now = [datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)]
    repo = TtlOutboxRepository(lambda: now[0])
    repo.add(_event(0))
    repo.add(_event(1))

    # A worker claims the batch, then the process dies before delivering anything.
    claimed = repo.claim_pending(worker_id="dead-worker", claim_ttl_seconds=10.0)
    assert len(claimed) == 2
    assert all(record.status == "processing" for record in repo._records.values())

    # Before the visibility timeout elapses, nothing is recovered.
    assert repo.release_expired_claims() == 0

    # After it elapses, the abandoned claims return to pending ...
    now[0] += timedelta(seconds=11)
    assert repo.release_expired_claims() == 2

    # ... and a live worker claims and delivers them exactly as normal.
    bus = MagicMock()
    worker = EventDeliveryWorker(
        repository=repo, event_bus=bus, worker_id="live-worker", poll_interval=0.0
    )
    worker._process_batch()

    assert bus.publish.call_count == 2
    assert len(repo.delivered) == 2


# ===========================================================================
# 6. Shutdown never marks an unfinished event as delivered
# ===========================================================================

def test_shutdown_never_marks_unfinished_events_as_delivered():
    repo = MockOutboxRepository()
    delivered = _event(0)
    interrupted = OutboxEvent.from_domain_event(
        StageCompleted(investigation_id="inc_00", stage="logs", total_tokens=1)
    )
    repo._events.extend([delivered, interrupted])

    bus = MagicMock()

    def publish(event) -> None:
        if event.event_id == interrupted.event_id:
            # Simulate an abrupt interruption (worker death) mid-batch.
            raise KeyboardInterrupt("interrupted")

    bus.publish.side_effect = publish
    worker = EventDeliveryWorker(repository=repo, event_bus=bus, poll_interval=0.0)

    with pytest.raises(KeyboardInterrupt):
        worker._process_batch()

    assert delivered.event_id in repo._processed
    # The unfinished event was never delivered and never recorded as delivered.
    assert interrupted.event_id not in repo._processed
    assert interrupted.event_id not in repo._scheduled
    assert interrupted.event_id not in repo._failed
    # Its claim is left in place for the TTL sweep to recover.
    assert interrupted.event_id in repo._claimed


# ===========================================================================
# 7. Repeated shutdown requests are safe
# ===========================================================================

def test_repeated_shutdown_requests_are_safe():
    worker = EventDeliveryWorker(repository=MockOutboxRepository(), event_bus=MagicMock())

    for _ in range(3):
        worker.request_shutdown()
        worker.stop()

    assert worker.shutdown_requested
    worker.start()  # returns immediately; must not raise or block


def test_close_repository_is_a_noop_without_close_and_never_raises():
    _close_repository(object())  # no close() at all -> no-op

    class Failing:
        def close(self) -> None:
            raise RuntimeError("boom")

    _close_repository(Failing())  # a failing close must not abort exit
