"""Transactional outbox repository for durable event delivery.

Provides atomic event persistence within state transactions for reliable
event delivery with background worker support.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

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

        Args:
            limit: Maximum number of events to fetch.

        Returns:
            List of pending OutboxEvents in created_at order.
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
