"""Pydantic v2 models for Sentinel 2.0 API Layer.

Enforces strict allowlist projection for all request and response schemas,
ensuring raw patches, internal stage outputs, and sensitive tracebacks
are never serialized or exposed.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional
from pydantic import BaseModel, ConfigDict, Field, field_validator

_SAFE_ID_REGEX = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,127}$")


class CreateInvestigationRequest(BaseModel):
    """Request schema for POST /investigations."""
    model_config = ConfigDict(extra="forbid")

    incident_id: str = Field(..., description="Target incident directory identifier")

    @field_validator("incident_id")
    @classmethod
    def validate_incident_id(cls, v: str) -> str:
        if not isinstance(v, str):
            raise ValueError("incident_id must be a string")
        stripped = v.strip()
        if not stripped:
            raise ValueError("incident_id cannot be empty or whitespace")
        if "/" in stripped or "\\" in stripped or ".." in stripped or ":" in stripped:
            raise ValueError(f"Path traversal characters detected in incident_id: {v!r}")
        if not _SAFE_ID_REGEX.match(stripped):
            raise ValueError(
                f"Invalid incident_id format: {v!r}. "
                "Must match ^[a-zA-Z0-9][a-zA-Z0-9_-]{0,127}$"
            )
        return stripped


class HealthResponse(BaseModel):
    """Liveness probe response for GET /health."""
    model_config = ConfigDict(extra="forbid")

    status: str = "ok"
    service: str = "sentinel"
    version: str = "2.0"


class StageSummaryResponse(BaseModel):
    """Allowlisted summary of an individual pipeline stage."""
    model_config = ConfigDict(extra="forbid")

    status: str
    llm_calls: int = Field(default=0, ge=0)
    prompt_tokens: int = Field(default=0, ge=0)
    completion_tokens: int = Field(default=0, ge=0)
    total_tokens: int = Field(default=0, ge=0)
    error: Optional[str] = None
    cache_hit: bool = False


class InvestigationSummaryResponse(BaseModel):
    """Aggregated numerical summary of hypotheses and proposals."""
    model_config = ConfigDict(extra="forbid")

    confirmed_hypotheses: int = Field(default=0, ge=0)
    rejected_hypotheses: int = Field(default=0, ge=0)
    inconclusive_hypotheses: int = Field(default=0, ge=0)
    proposals_generated: int = Field(default=0, ge=0)
    proposals_approved: int = Field(default=0, ge=0)
    proposals_rejected: int = Field(default=0, ge=0)


class CreateInvestigationResponse(BaseModel):
    """Allowlisted response for POST /investigations."""
    model_config = ConfigDict(extra="forbid")

    investigation_id: str
    pipeline_status: str
    status: str
    started_at: str
    completed_at: Optional[str] = None
    error: Optional[str] = None
    llm_call_count: int = Field(default=0, ge=0)
    prompt_tokens: int = Field(default=0, ge=0)
    completion_tokens: int = Field(default=0, ge=0)
    total_tokens: int = Field(default=0, ge=0)
    summary: InvestigationSummaryResponse
    stages: Dict[str, StageSummaryResponse]
    human_approval_notice: str


_PROPOSAL_ID_REGEX = re.compile(r"^FIX-[0-9]{3}$")


class ApprovalRecordResponse(BaseModel):
    """Allowlisted projection of a recorded human approval decision."""
    model_config = ConfigDict(extra="forbid")

    proposal_id: str
    status: str
    decision: str
    approved_by: str
    timestamp: str
    notes: Optional[str] = None


class SubmitApprovalRequest(BaseModel):
    """Request schema for POST /investigations/{id}/approval."""
    model_config = ConfigDict(extra="forbid")

    proposal_id: str = Field(..., description="Target fix proposal identifier, e.g. FIX-001")
    decision: str = Field(..., description="Human decision: 'approved' or 'rejected'")
    reviewer: str = Field(..., min_length=1, max_length=128, description="Identity of the human reviewer")
    notes: Optional[str] = Field(default=None, max_length=1000, description="Optional reviewer notes")

    @field_validator("proposal_id")
    @classmethod
    def validate_proposal_id(cls, v: str) -> str:
        if not isinstance(v, str):
            raise ValueError("proposal_id must be a string")
        stripped = v.strip()
        if not _PROPOSAL_ID_REGEX.match(stripped):
            raise ValueError(f"Invalid proposal_id format: {v!r}. Must match ^FIX-[0-9]{{3}}$")
        return stripped

    @field_validator("decision")
    @classmethod
    def validate_decision(cls, v: str) -> str:
        if not isinstance(v, str):
            raise ValueError("decision must be a string")
        lowered = v.strip().lower()
        if lowered not in ("approved", "rejected"):
            raise ValueError("decision must be either 'approved' or 'rejected'")
        return lowered

    @field_validator("reviewer")
    @classmethod
    def validate_reviewer(cls, v: str) -> str:
        if not isinstance(v, str) or not v.strip():
            raise ValueError("reviewer cannot be empty")
        return v.strip()


class SubmitApprovalResponse(BaseModel):
    """Response schema for POST /investigations/{id}/approval."""
    model_config = ConfigDict(extra="forbid")

    investigation_id: str
    approval: ApprovalRecordResponse
    status: str
    human_approval_notice: str


class GetInvestigationResponse(BaseModel):
    """Allowlisted response for GET /investigations/{id}."""
    model_config = ConfigDict(extra="forbid")

    investigation_id: str
    status: str
    current_stage: Optional[str] = None
    started_at: str
    completed_at: Optional[str] = None
    error: Optional[str] = None
    llm_call_count: int = Field(default=0, ge=0)
    version: int = Field(default=1, ge=1)
    lease_owner: Optional[str] = None
    lease_expires_at: Optional[str] = None
    stages: Dict[str, StageSummaryResponse]
    approvals: List[ApprovalRecordResponse] = Field(default_factory=list)


class ListInvestigationsResponse(BaseModel):
    """Allowlisted response for GET /investigations."""
    model_config = ConfigDict(extra="forbid")

    investigations: List[str]


class ErrorResponse(BaseModel):
    """Standardized error payload."""
    model_config = ConfigDict(extra="forbid")

    error: str
    detail: str


# ---------------------------------------------------------------------------
# Event-history safe payload allowlist
#
# Domain events never carry secrets, baseline artefacts, or filesystem paths,
# but we still project an explicit allowlist rather than exposing the raw DB
# JSON blob.  Keys are derived from inspecting every concrete DomainEvent
# subtype in core/events/events.py:
#
#   InvestigationStarted  : initial_stage
#   InvestigationResumed  : resume_stage, resumed_from_status
#   StageStarted          : stage
#   StageCompleted        : stage, llm_calls, prompt_tokens,
#                           completion_tokens, total_tokens, item_count
#   StageFailed           : stage, error
#   StageCached           : stage
#   StageSkipped          : stage, reason
#   InvestigationCompleted: status, total_llm_calls, total_tokens,
#                           confirmed_hypotheses, proposals_generated,
#                           proposals_approved, error
#
# Base DomainEvent fields (event_id, event_type, timestamp, investigation_id)
# are also included so the payload is self-contained for consumers.
# ---------------------------------------------------------------------------

_SAFE_EVENT_PAYLOAD_KEYS: frozenset[str] = frozenset({
    # Base DomainEvent fields
    "event_id",
    "event_type",
    "timestamp",
    "investigation_id",
    # Stage identity
    "stage",
    # InvestigationStarted
    "initial_stage",
    # InvestigationResumed
    "resume_stage",
    "resumed_from_status",
    # StageCompleted metrics
    "llm_calls",
    "prompt_tokens",
    "completion_tokens",
    "total_tokens",
    "item_count",
    # StageFailed / InvestigationCompleted
    "error",
    # StageSkipped
    "reason",
    # InvestigationCompleted
    "status",
    "total_llm_calls",
    "confirmed_hypotheses",
    "proposals_generated",
    "proposals_approved",
})


def _project_event_payload(raw: Dict[str, Any]) -> Dict[str, Any]:
    """Return a copy of *raw* containing only allowlisted keys.

    Any key absent from _SAFE_EVENT_PAYLOAD_KEYS is silently stripped.
    Values are accepted as-is (str, int, float, None) — no nested objects
    exist in the current event model.
    """
    return {k: v for k, v in raw.items() if k in _SAFE_EVENT_PAYLOAD_KEYS}


class EventHistoryItemResponse(BaseModel):
    """Allowlisted projection of a single outbox event for the history API.

    Represents one domain event emitted during an investigation.  The
    ``payload`` field is explicitly projected through an allowlist; it never
    contains secrets, baseline artefacts, or internal filesystem paths.
    """
    model_config = ConfigDict(extra="forbid")

    event_id: str
    investigation_id: str
    event_type: str
    # ``timestamp`` is the domain-event creation time embedded in the payload.
    # ``created_at`` is the outbox row insertion time (ISO-8601 string).
    timestamp: Optional[str] = None
    created_at: str
    processed_at: Optional[str] = None
    status: str                             # pending | processed | failed
    retry_count: int = Field(default=0, ge=0)
    error_message: Optional[str] = None     # delivery error, not event error
    payload: Dict[str, Any] = Field(default_factory=dict)


class EventHistoryResponse(BaseModel):
    """Response for GET /investigations/{investigation_id}/events.

    ``backend_supports_event_history`` is False when the persistence backend
    does not durably store domain events (e.g. FilesystemRepository).  In
    that case ``events`` will always be an empty list and consumers should
    not infer that the investigation produced zero events.
    """
    model_config = ConfigDict(extra="forbid")

    investigation_id: str
    total: int = Field(default=0, ge=0)
    events: List[EventHistoryItemResponse] = Field(default_factory=list)
    backend_supports_event_history: bool = True
