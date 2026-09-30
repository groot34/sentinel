"""Background event delivery worker for Sentinel 2.0.

Polls the transactional outbox for pending events and delivers them
through the EventBus with retry logic and idempotency.
"""

from __future__ import annotations

import logging
import time
from typing import Optional

from core.events.base import DomainEvent
from core.events.bus import EventBus, InMemoryEventBus
from core.persistence.outbox import OutboxEvent
from core.persistence.postgres import PostgresRepository

logger = logging.getLogger(__name__)

# Delivery configuration
DEFAULT_POLL_INTERVAL_SECONDS = 5.0
DEFAULT_BATCH_SIZE = 100
MAX_RETRY_ATTEMPTS = 5
RETRY_BACKOFF_SECONDS = 2.0


class EventDeliveryWorker:
    """Background worker that delivers outbox events to EventBus."""

    def __init__(
        self,
        repository: PostgresRepository,
        event_bus: Optional[EventBus] = None,
        poll_interval: float = DEFAULT_POLL_INTERVAL_SECONDS,
        batch_size: int = DEFAULT_BATCH_SIZE,
    ):
        """Initialize event delivery worker.

        Args:
            repository: PostgresRepository with outbox methods.
            event_bus: EventBus for event delivery. Defaults to InMemoryEventBus.
            poll_interval: Seconds between polling cycles.
            batch_size: Maximum events to fetch per poll.
        """
        self.repository = repository
        self.event_bus = event_bus or InMemoryEventBus()
        self.poll_interval = poll_interval
        self.batch_size = batch_size
        self._running = False

    def start(self) -> None:
        """Start the event delivery worker loop."""
        self._running = True
        logger.info("Event delivery worker started")

        while self._running:
            try:
                self._process_batch()
            except Exception as e:
                logger.exception("Error in event delivery worker: %s", e)
            time.sleep(self.poll_interval)

        logger.info("Event delivery worker stopped")

    def stop(self) -> None:
        """Stop the event delivery worker loop."""
        self._running = False

    def _process_batch(self) -> None:
        """Fetch and deliver a batch of pending events."""
        events = self.repository.fetch_pending(limit=self.batch_size)
        if not events:
            return

        logger.debug("Processing %d pending events", len(events))

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
        """Handle failed event delivery with retry logic."""
        retry_count = outbox_event.retry_count + 1

        if retry_count >= MAX_RETRY_ATTEMPTS:
            # Max retries exceeded - mark as failed
            self.repository.mark_failed(
                outbox_event.event_id,
                f"Max retry attempts ({MAX_RETRY_ATTEMPTS}) exceeded: {error}",
            )
            logger.error(
                "Event %s marked as failed after %d attempts: %s",
                outbox_event.event_id,
                retry_count,
                error,
            )
        else:
            # Increment retry count for next attempt
            self.repository.increment_retry(outbox_event.event_id)
            logger.warning(
                "Event %s delivery failed (attempt %d/%d): %s. Will retry.",
                outbox_event.event_id,
                retry_count,
                MAX_RETRY_ATTEMPTS,
                error,
            )
            # Exponential backoff
            time.sleep(RETRY_BACKOFF_SECONDS * (2 ** (retry_count - 1)))


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

    # Check if repository supports outbox operations
    if not hasattr(repository, "fetch_pending"):
        print("ERROR: Repository does not support outbox operations. PostgreSQL backend required.", file=sys.stderr)
        sys.exit(1)

    # Initialize outbox schema if using PostgreSQL
    if hasattr(repository, "initialize_outbox_schema"):
        try:
            repository.initialize_outbox_schema()
        except Exception as exc:
            print(f"WARNING: Failed to initialize outbox schema: {exc}", file=sys.stderr)

    # Create and start worker
    worker = EventDeliveryWorker(repository=repository)
    worker.start()


if __name__ == "__main__":
    main()
