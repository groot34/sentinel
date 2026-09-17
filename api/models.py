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
    stages: Dict[str, StageSummaryResponse]


class ListInvestigationsResponse(BaseModel):
    """Allowlisted response for GET /investigations."""
    model_config = ConfigDict(extra="forbid")

    investigations: List[str]


class ErrorResponse(BaseModel):
    """Standardized error payload."""
    model_config = ConfigDict(extra="forbid")

    error: str
    detail: str
