"""Background event delivery worker for Sentinel 2.0.

Polls the transactional outbox for eligible pending events, atomically claims
them (safe across concurrent worker processes), delivers them through the
EventBus, and durably records each delivery outcome. Retries are scheduled in
the database rather than by sleeping inside the delivery loop, so retry timing
survives worker restarts and abandoned claims can be recovered after a crash.

Delivery guarantee: durable, concurrency-safe claims with **at-least-once**
delivery. Because the destination is an in-process EventBus, exactly-once
external delivery is not claimed; consumers must be idempotent and key on
``event_id``.
"""

from __future__ import annotations

import logging
import os
import random
import socket
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional

from core.events.base import DomainEvent
from core.events.bus import EventBus, InMemoryEventBus
from core.persistence.outbox import OutboxEvent
from core.persistence.postgres import PostgresRepository

logger = logging.getLogger(__name__)

# Delivery configuration
DEFAULT_POLL_INTERVAL_SECONDS = 5.0
DEFAULT_BATCH_SIZE = 100
MAX_RETRY_ATTEMPTS = 5
# Durable retry backoff: bounded exponential with full jitter (in-DB scheduling).
DEFAULT_RETRY_BASE_DELAY_SECONDS = 2.0
DEFAULT_RETRY_MAX_DELAY_SECONDS = 60.0
# Visibility timeout for a delivery claim. A claim older than this is treated as
# abandoned (crashed worker) and may be recovered by another worker.
DEFAULT_CLAIM_TTL_SECONDS = 300.0

# Every delivery-path method the worker requires from a repository. Only the
# PostgreSQL backend implements the atomic claim protocol; a repository missing
# any of these must be rejected outright rather than silently downgraded to a
# non-claiming read path (``fetch_pending``).
REQUIRED_DELIVERY_METHODS = (
    "release_expired_claims",
    "claim_pending",
    "mark_processed",
    "mark_failed",
    "schedule_retry",
)


def _default_worker_id() -> str:
    """Return a process-unique worker identity for delivery claims."""
    return f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"


def _missing_delivery_methods(repository: object) -> list:
    """Return the required delivery methods the repository does not implement.

    A method is considered implemented only when the attribute exists and is
    callable, so a bare attribute of the wrong type is not mistaken for support.
    """
    return [
        name
        for name in REQUIRED_DELIVERY_METHODS
        if not callable(getattr(repository, name, None))
    ]


class EventDeliveryWorker:
    """Background worker that delivers outbox events to EventBus."""

    def __init__(
        self,
        repository: PostgresRepository,
        event_bus: Optional[EventBus] = None,
        poll_interval: float = DEFAULT_POLL_INTERVAL_SECONDS,
        batch_size: int = DEFAULT_BATCH_SIZE,
        worker_id: Optional[str] = None,
        claim_ttl_seconds: float = DEFAULT_CLAIM_TTL_SECONDS,
        max_retry_attempts: int = MAX_RETRY_ATTEMPTS,
        retry_base_delay: float = DEFAULT_RETRY_BASE_DELAY_SECONDS,
        retry_max_delay: float = DEFAULT_RETRY_MAX_DELAY_SECONDS,
        clock: Optional[Callable[[], datetime]] = None,
        rng: Optional[random.Random] = None,
    ):
        """Initialize event delivery worker.

        Args:
            repository: PostgreSQL repository with outbox claim/delivery methods.
            event_bus: EventBus for event delivery. Defaults to InMemoryEventBus.
            poll_interval: Seconds between polling cycles (never inside a batch).
            batch_size: Maximum events to claim per poll.
            worker_id: Stable identity for delivery claims. Defaults to a
                process-unique value.
            claim_ttl_seconds: Visibility timeout applied to each claim.
            max_retry_attempts: Attempt limit before an event is terminally failed.
            retry_base_delay: Base delay for exponential retry backoff.
            retry_max_delay: Upper bound for the retry backoff ceiling.
            clock: Injectable UTC clock (for deterministic tests).
            rng: Injectable random source for jitter (for deterministic tests).
        """
        self.repository = repository
        self.event_bus = event_bus or InMemoryEventBus()
        self.poll_interval = poll_interval
        self.batch_size = batch_size
        self.worker_id = worker_id or _default_worker_id()
        self.claim_ttl_seconds = claim_ttl_seconds
        self.max_retry_attempts = max_retry_attempts
        self.retry_base_delay = retry_base_delay
        self.retry_max_delay = retry_max_delay
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._rng = rng or random.Random()
        self._running = False

    def start(self) -> None:
        """Start the event delivery worker loop until :meth:`stop` is called."""
        self._running = True
        logger.info("Event delivery worker %s started", self.worker_id)

        try:
            while self._running:
                try:
                    self._process_batch()
                except Exception as e:  # noqa: BLE001 - keep the loop alive
                    logger.exception("Error in event delivery worker: %s", e)
                # Sleep between polling cycles only; never inside the batch loop.
                time.sleep(self.poll_interval)
        finally:
            logger.info("Event delivery worker %s stopped", self.worker_id)

    def stop(self) -> None:
        """Request the worker loop to stop at the next iteration."""
        self._running = False

    def _process_batch(self) -> None:
        """Recover abandoned claims, then claim and deliver a batch of events."""
        released = self.repository.release_expired_claims()
        if released:
            logger.info("Recovered %d expired outbox claim(s)", released)

        events = self.repository.claim_pending(
            worker_id=self.worker_id,
            limit=self.batch_size,
            claim_ttl_seconds=self.claim_ttl_seconds,
        )
        if not events:
            return

        logger.debug("Claimed %d outbox event(s) for delivery", len(events))

        for outbox_event in events:
            try:
                self._deliver_event(outbox_event)
                self.repository.mark_processed(outbox_event.event_id)
                logger.debug("Event %s delivered and marked processed", outbox_event.event_id)
            except Exception as e:
                self._handle_delivery_failure(outbox_event, e)

    def _deliver_event(self, outbox_event: OutboxEvent) -> None:
        """Reconstruct DomainEvent and deliver to EventBus."""
        # Reconstruct DomainEvent from payload
        event = self._reconstruct_event(outbox_event)
        if event is None:
            raise ValueError(f"Failed to reconstruct event from payload: {outbox_event.event_id}")

        # Deliver to EventBus
        self.event_bus.publish(event)

    def _reconstruct_event(self, outbox_event: OutboxEvent) -> Optional[DomainEvent]:
        """Reconstruct DomainEvent from OutboxEvent payload.

        Returns None if event type is unknown.
        """
        from core.events.events import (
            InvestigationCompleted,
            InvestigationResumed,
            InvestigationStarted,
            StageCached,
            StageCompleted,
            StageFailed,
            StageSkipped,
            StageStarted,
        )

        event_type = outbox_event.event_type
        payload = outbox_event.payload

        try:
            if event_type == "InvestigationStarted":
                return InvestigationStarted(**payload)
            elif event_type == "InvestigationResumed":
                return InvestigationResumed(**payload)
            elif event_type == "InvestigationCompleted":
                return InvestigationCompleted(**payload)
            elif event_type == "StageStarted":
                return StageStarted(**payload)
            elif event_type == "StageCompleted":
                return StageCompleted(**payload)
            elif event_type == "StageFailed":
                return StageFailed(**payload)
            elif event_type == "StageCached":
                return StageCached(**payload)
            elif event_type == "StageSkipped":
                return StageSkipped(**payload)
            else:
                logger.warning("Unknown event type: %s", event_type)
                return None
        except Exception as e:
            logger.error("Failed to reconstruct event %s: %s", outbox_event.event_id, e)
            return None

    def _handle_delivery_failure(self, outbox_event: OutboxEvent, error: Exception) -> None:
        """Record a failed delivery: reschedule durably, or terminally fail it.

        No sleeping occurs here. Retry timing is persisted so it survives worker
        restarts and never blocks the rest of the claimed batch.
        """
        attempt = outbox_event.retry_count + 1

        if attempt >= self.max_retry_attempts:
            self.repository.mark_failed(
                outbox_event.event_id,
                f"Max retry attempts ({self.max_retry_attempts}) exceeded: {error}",
            )
            logger.error(
                "Event %s marked as failed after %d attempt(s): %s",
                outbox_event.event_id,
                attempt,
                error,
            )
            return

        delay = self._compute_retry_delay(attempt)
        next_attempt_at = self._clock() + timedelta(seconds=delay)
        self.repository.schedule_retry(
            outbox_event.event_id,
            next_attempt_at,
            f"Delivery attempt {attempt} failed: {error}",
        )
        logger.warning(
            "Event %s delivery failed (attempt %d/%d): %s. Rescheduled for %s.",
            outbox_event.event_id,
            attempt,
            self.max_retry_attempts,
            error,
            next_attempt_at.isoformat(),
        )

    def _compute_retry_delay(self, attempt: int) -> float:
        """Bounded exponential backoff with full jitter for a 1-based attempt."""
        ceiling = self.retry_base_delay * (2 ** (attempt - 1))
        ceiling = min(ceiling, self.retry_max_delay)
        if ceiling <= 0:
            return 0.0
        return self._rng.uniform(0.0, ceiling)


def main() -> None:
    """Initialize and start the event delivery worker."""
    import sys
    from dotenv import load_dotenv

    load_dotenv()

    # Configure logging
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )

    from core.persistence.factory import get_repository
    from core.persistence.repository import PersistenceError

    try:
        repository = get_repository()
    except PersistenceError as exc:
        print(f"ERROR: Failed to initialise persistence backend: {exc}", file=sys.stderr)
        sys.exit(1)

    # Check the repository implements the full atomic outbox delivery protocol.
    # Anything less cannot provide concurrency-safe claims, so fail clearly
    # instead of silently falling back to non-claiming reads.
    missing_methods = _missing_delivery_methods(repository)
    if missing_methods:
        print(
            "ERROR: Repository does not support atomic outbox delivery; missing "
            f"method(s): {', '.join(missing_methods)}. PostgreSQL backend required.",
            file=sys.stderr,
        )
        sys.exit(1)

    # Initialize outbox schema if using PostgreSQL
    if hasattr(repository, "initialize_outbox_schema"):
        try:
            repository.initialize_outbox_schema()
        except Exception as exc:
            print(f"WARNING: Failed to initialize outbox schema: {exc}", file=sys.stderr)

    # Create and start worker
    worker = EventDeliveryWorker(repository=repository)
    try:
        worker.start()
    except KeyboardInterrupt:
        worker.stop()


if __name__ == "__main__":
    main()
