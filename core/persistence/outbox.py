"""Transactional outbox repository for durable event delivery.

Provides atomic event persistence within state transactions for reliable
event delivery with background worker support.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from core.events.base import DomainEvent
from core.persistence.repository import PersistenceError, validate_investigation_id


@dataclass
class OutboxEvent:
    """Represent an event in the outbox."""
    event_id: str
    investigation_id: str
    event_type: str
    payload: Dict[str, Any]
    status: str = "pending"
    created_at: str = ""
    processed_at: Optional[str] = None
    retry_count: int = 0
    error_message: Optional[str] = None

    @classmethod
    def from_domain_event(cls, event: DomainEvent) -> "OutboxEvent":
        """Convert a DomainEvent to an OutboxEvent."""
        return cls(
            event_id=event.event_id,
            investigation_id=event.investigation_id,
            event_type=event.event_type,
            payload=event.model_dump(),
            created_at=event.timestamp,
        )


class OutboxRepository:
    """Abstract interface for outbox event persistence."""

    @property
    def supports_event_history(self) -> bool:
        """Return True if this backend durably persists event history.

        Backends that do not store outbox events (e.g. FilesystemRepository)
        should override this to return False so callers can surface the
        limitation to API consumers rather than returning a misleading empty
        list.
        """
        return True

    def save_event(self, event: DomainEvent) -> None:
        """Persist a domain event to the outbox.

        Args:
            event: DomainEvent to persist.

        Raises:
            PersistenceError: If save fails.
        """
        raise NotImplementedError

    def save_events(self, events: List[DomainEvent]) -> None:
        """Persist multiple domain events to the outbox.

        Args:
            events: List of DomainEvents to persist.

        Raises:
            PersistenceError: If save fails.
        """
        raise NotImplementedError

    def fetch_pending(self, limit: int = 100) -> List[OutboxEvent]:
        """Fetch pending events for delivery.

        Observational query only: it does not claim or mutate events, so it is
        safe for auditing/tests but must not be used to drive delivery. Use
        :meth:`claim_pending` to obtain an exclusive delivery claim.

        Args:
            limit: Maximum number of events to fetch.

        Returns:
            List of pending OutboxEvents in canonical (created_at, event_id) order.
        """
        raise NotImplementedError

    def claim_pending(
        self,
        worker_id: str,
        limit: int = 100,
        claim_ttl_seconds: float = 300.0,
    ) -> List[OutboxEvent]:
        """Atomically claim up to ``limit`` eligible pending events for delivery.

        A claim transfers ownership of an event to ``worker_id`` for a bounded
        visibility timeout. Concurrent claimers must not receive the same event;
        events scheduled for a future retry (:attr:`next_attempt_at`) and events
        already claimed, processed, or failed are ineligible.

        Args:
            worker_id: Identity of the claiming worker.
            limit: Maximum number of events to claim.
            claim_ttl_seconds: Visibility timeout for the resulting claim.

        Returns:
            List of claimed OutboxEvents in canonical (created_at, event_id) order.

        Raises:
            PersistenceError: If claiming fails.
        """
        raise NotImplementedError

    def release_expired_claims(self) -> int:
        """Return abandoned claims (past their visibility timeout) to pending.

        Enables crash recovery: if a worker dies mid-delivery, its claim expires
        and another worker can reclaim the event.

        Returns:
            Number of events whose claims were released.

        Raises:
            PersistenceError: If recovery fails.
        """
        raise NotImplementedError

    def schedule_retry(
        self, event_id: str, next_attempt_at: datetime, error_message: str
    ) -> None:
        """Durably reschedule a failed event for a later attempt.

        Increments the attempt counter, records the error, clears the claim, and
        sets the earliest time the event may be claimed again. No sleeping occurs
        in the delivery loop.

        Args:
            event_id: Event ID to reschedule.
            next_attempt_at: Earliest time the event may be claimed again.
            error_message: Error description from the failed attempt.

        Raises:
            PersistenceError: If the update fails.
        """
        raise NotImplementedError

    def mark_processed(self, event_id: str) -> None:
        """Mark an event as successfully processed.

        Args:
            event_id: Event ID to mark.

        Raises:
            PersistenceError: If update fails.
        """
        raise NotImplementedError

    def mark_failed(self, event_id: str, error_message: str) -> None:
        """Mark an event as failed with error message.

        Args:
            event_id: Event ID to mark.
            error_message: Error description.

        Raises:
            PersistenceError: If update fails.
        """
        raise NotImplementedError

    def increment_retry(self, event_id: str) -> None:
        """Increment retry count for an event.

        Args:
            event_id: Event ID to increment.

        Raises:
            PersistenceError: If update fails.
        """
        raise NotImplementedError

    def list_events(
        self,
        investigation_id: str,
        limit: int = 200,
        after_cursor: Optional[Tuple[str, str]] = None,
    ) -> List[OutboxEvent]:
        """Return all durable events for an investigation in chronological order.

        Returns events regardless of delivery status (pending, processed, failed).
        This is an audit-history query, not a delivery-queue query.

        Args:
            investigation_id: Investigation whose events to retrieve.
            limit: Maximum number of events to return (default 200).
            after_cursor: Optional (created_at, event_id) keyset cursor to
                retrieve only events strictly after this point in canonical
                order (created_at ASC, event_id ASC).

        Returns:
            List of OutboxEvents ordered by created_at ASC, event_id ASC.

        Raises:
            PersistenceError: If the investigation ID is invalid or a database
                error occurs.
        """
        raise NotImplementedError
