"""Base domain event model for Sentinel 2.0.

Provides an immutable, typed foundation for all domain events in Sentinel.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from pydantic import ConfigDict, Field

from core.domain.models import BaseDomainModel


def _utcnow_iso() -> str:
    """Return current UTC timestamp in ISO-8601 format."""
    return datetime.now(timezone.utc).isoformat()


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
