"""Domain events package for Sentinel 2.0.

Provides immutable lifecycle events and a synchronous event bus.
"""

from core.events.base import DomainEvent
from core.events.bus import EventBus, EventHandler, EventRecorder, InMemoryEventBus
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

__all__ = [
    "DomainEvent",
    "EventBus",
    "InMemoryEventBus",
    "EventHandler",
    "EventRecorder",
    "InvestigationStarted",
    "InvestigationResumed",
    "StageStarted",
    "StageCompleted",
    "StageFailed",
    "StageCached",
    "StageSkipped",
    "InvestigationCompleted",
]
