"""Base domain event model for Sentinel 2.0.

Provides an immutable, typed foundation for all domain events in Sentinel.
"""

from __future__ import annotations

import threading
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

from pydantic import ConfigDict, Field

from core.domain.models import BaseDomainModel


def _wall_clock_now() -> datetime:
    """Read the current UTC wall clock.

    Kept as a module-level indirection so tests can freeze the clock and prove
    the monotonic guarantee below without depending on real elapsed time.
    """
    return datetime.now(timezone.utc)


#: Serialises the read-modify-write of the last issued timestamp so concurrent
#: event creation from several threads still yields strictly increasing values.
_timestamp_lock = threading.Lock()

#: Most recent timestamp handed out by :func:`_utcnow_iso`.
_last_timestamp: Optional[datetime] = None


def _utcnow_iso() -> str:
    """Return the current UTC timestamp in ISO-8601 format, strictly increasing.

    Persisted events are ordered by ``(created_at ASC, event_id ASC)``, where
    ``event_id`` is a random UUID used only as a deterministic tie-breaker. The
    OS wall clock is not fine-grained enough to carry that ordering on its own:
    on Windows a single ``datetime.now()`` tick is roughly 15 ms, so events
    created back-to-back routinely receive the *same* timestamp and are then
    sorted by their random UUIDs. The observed order of such a batch would then
    differ from the order in which the events were created.

    Emitting a strictly increasing timestamp keeps the persisted order equal to
    the creation order without changing the ordering contract, the database
    schema, or the keyset cursor. Values remain wall-clock based; the only
    adjustment is a one microsecond nudge when the clock has not advanced since
    the previous event.
    """
    global _last_timestamp

    with _timestamp_lock:
        now = _wall_clock_now()
        if _last_timestamp is not None and now <= _last_timestamp:
            now = _last_timestamp + timedelta(microseconds=1)
        _last_timestamp = now

    return now.isoformat()


class DomainEvent(BaseDomainModel):
    """Immutable domain event representing a completed fact in the investigation lifecycle."""

    event_id: str = Field(default_factory=lambda: f"evt_{uuid.uuid4().hex}")
    event_type: str
    timestamp: str = Field(default_factory=_utcnow_iso)
    investigation_id: str

    model_config = ConfigDict(
        populate_by_name=True,
        use_enum_values=True,
        validate_assignment=True,
        frozen=True,
        extra="forbid",
    )
