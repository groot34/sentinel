"""Concrete domain events for Sentinel 2.0.

Provides immutable events for the investigation and stage lifecycles.
"""

from __future__ import annotations

from typing import Literal, Optional
from core.events.base import DomainEvent


class InvestigationStarted(DomainEvent):
    """Emitted when an investigation starts a fresh run."""
    event_type: Literal["InvestigationStarted"] = "InvestigationStarted"
    initial_stage: str = "logs"


class InvestigationResumed(DomainEvent):
    """Emitted when an incomplete or failed investigation is resumed."""
    event_type: Literal["InvestigationResumed"] = "InvestigationResumed"
    resume_stage: Optional[str] = None
    resumed_from_status: str


class StageStarted(DomainEvent):
    """Emitted when a pipeline stage begins execution."""
    event_type: Literal["StageStarted"] = "StageStarted"
    stage: str


class StageCompleted(DomainEvent):
    """Emitted when a pipeline stage completes successfully."""
    event_type: Literal["StageCompleted"] = "StageCompleted"
    stage: str
    llm_calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    item_count: Optional[int] = None


class StageFailed(DomainEvent):
    """Emitted when a pipeline stage encounters an error."""
    event_type: Literal["StageFailed"] = "StageFailed"
    stage: str
    error: str


class StageCached(DomainEvent):
    """Emitted when a pipeline stage reuses cached results without running."""
    event_type: Literal["StageCached"] = "StageCached"
    stage: str


class StageSkipped(DomainEvent):
    """Emitted when a pipeline stage is skipped due to upstream conditions."""
    event_type: Literal["StageSkipped"] = "StageSkipped"
    stage: str
    reason: Optional[str] = None


class InvestigationCompleted(DomainEvent):
    """Emitted when an investigation reaches its terminal state (COMPLETED, PARTIAL, or FAILED)."""
    event_type: Literal["InvestigationCompleted"] = "InvestigationCompleted"
    status: str
    total_llm_calls: int = 0
    total_tokens: int = 0
    confirmed_hypotheses: int = 0
    proposals_generated: int = 0
    proposals_approved: int = 0
    error: Optional[str] = None
