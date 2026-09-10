"""In-memory event bus and event recorder for Sentinel 2.0.

Provides synchronous, deterministic, fault-isolated publish/subscribe messaging
for Sentinel domain events.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from typing import Callable, List, Optional, Tuple, Type, TypeVar, Union

from core.events.base import DomainEvent

logger = logging.getLogger(__name__)

EventHandler = Callable[[DomainEvent], None]
T = TypeVar("T", bound=DomainEvent)


class EventBus(ABC):
    """Abstract interface for publishing and subscribing to DomainEvents."""

    @abstractmethod
    def publish(self, event: DomainEvent) -> None:
        """Publish an event to all matching subscribers.

        Args:
            event: DomainEvent instance.
        """
        raise NotImplementedError

    @abstractmethod
    def subscribe(self, event_type: Union[Type[DomainEvent], str], handler: EventHandler) -> None:
        """Subscribe a handler to a specific event type.

        Args:
            event_type: DomainEvent class or its string type name.
            handler: Callable taking the event.
        """
        raise NotImplementedError

    @abstractmethod
    def subscribe_all(self, handler: EventHandler) -> None:
        """Subscribe a handler to all published events.

        Args:
            handler: Callable taking any DomainEvent.
        """
        raise NotImplementedError


class InMemoryEventBus(EventBus):
    """Synchronous in-memory EventBus with subscriber failure isolation."""

    def __init__(self, strict: bool = False) -> None:
        """Initialize event bus.

        Args:
            strict: If True, subscriber exceptions are re-raised immediately (useful for testing).
                    If False (default), exceptions are caught, logged, and isolated.
        """
        self.strict = strict
        # Preserves strict FIFO registration order across both specific and global handlers:
        # Tuple of (event_type_name_or_None, handler)
        self._subscriptions: List[Tuple[Optional[str], EventHandler]] = []
        self._errors: List[Exception] = []

    @property
    def errors(self) -> List[Exception]:
        """List of recorded subscriber exceptions (when running with strict=False)."""
        return list(self._errors)

    def subscribe(self, event_type: Union[Type[DomainEvent], str], handler: EventHandler) -> None:
        """Subscribe handler to a specific event type."""
        type_name = event_type if isinstance(event_type, str) else event_type.__name__
        self._subscriptions.append((type_name, handler))

    def subscribe_all(self, handler: EventHandler) -> None:
        """Subscribe handler to all events."""
        self._subscriptions.append((None, handler))

    def publish(self, event: DomainEvent) -> None:
        """Publish an event to registered subscribers in deterministic FIFO order.

        Subscriber exceptions are isolated unless strict mode is enabled.
        """
        if not isinstance(event, DomainEvent):
            raise TypeError(f"Expected DomainEvent, got {type(event).__name__}")

        event_name = event.event_type

        for target_type, handler in self._subscriptions:
            if target_type is None or target_type == event_name:
                try:
                    handler(event)
                except Exception as exc:
                    self._errors.append(exc)
                    logger.exception(
                        "Subscriber %r failed handling event %s: %s",
                        handler,
                        event_name,
                        exc,
                    )
                    if self.strict:
                        raise

    def clear(self) -> None:
        """Clear all registered subscriptions and error history."""
        self._subscriptions.clear()
        self._errors.clear()


class EventRecorder:
    """Helper that subscribes to all events and records them in chronological order."""

    def __init__(self, bus: Optional[EventBus] = None) -> None:
        self.events: List[DomainEvent] = []
        if bus is not None:
            bus.subscribe_all(self.record)

    def record(self, event: DomainEvent) -> None:
        """Record an event."""
        self.events.append(event)

    def clear(self) -> None:
        """Clear recorded events."""
        self.events.clear()

    def filter(self, event_type: Union[Type[T], str]) -> List[T]:
        """Filter recorded events by event type."""
        type_name = event_type if isinstance(event_type, str) else event_type.__name__
        return [e for e in self.events if e.event_type == type_name]  # type: ignore
