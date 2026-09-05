"""Domain models for Sentinel 2.0.

Provides strongly typed internal representations for evidence, hypotheses,
verification results, fix proposals, approval records, and stage results.
All models support bidirectional conversion to/from JSON-compatible dictionaries.
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Dict, List, Optional, Union
from pydantic import BaseModel, ConfigDict, Field, field_validator


# ============================================================================
# Enums
# ============================================================================

class IncidentStatus(str, Enum):
    """Overall status of an incident investigation."""
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    PARTIAL = "PARTIAL"
    FAILED = "FAILED"


class StageStatus(str, Enum):
    """Execution status of an individual pipeline stage."""
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    SKIPPED = "SKIPPED"
    CACHED = "CACHED"
    REUSED = "REUSED"


class VerificationVerdict(str, Enum):
    """Verification verdict outcome for a tested hypothesis."""
    CONFIRMED = "CONFIRMED"
    REJECTED = "REJECTED"
    INCONCLUSIVE = "INCONCLUSIVE"


class CheckResult(str, Enum):
    """Result of an individual check execution."""
    PASS = "PASS"
    FAIL = "FAIL"
    INCONCLUSIVE = "INCONCLUSIVE"


class CheckType(str, Enum):
    """Category of verification check."""
    CODE_INVARIANT = "code_invariant"
    METRIC_CORRELATION = "metric_correlation"
    LOG_SEQUENCE = "log_sequence"
    STATE_TRANSITION = "state_transition"
    CONFIG_VALIDATION = "config_validation"


class ProposalStatus(str, Enum):
    """Lifecycle status of a proposed remediation patch."""
    PROPOSED = "PROPOSED"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"


class ApprovalDecision(str, Enum):
    """Approval decision values."""
    APPROVED = "approved"
    REJECTED = "rejected"


class ApprovalStatus(str, Enum):
    """Approval gate status values."""
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"


class EvidenceSourceType(str, Enum):
    """Origin category of an evidence item."""
    LOGS = "logs"
    METRICS = "metrics"
    CODE = "code"
    CONFIG = "config"
    DEPLOY = "deploy"


class LogEvidenceType(str, Enum):
    """Deterministic extraction category for log evidence."""
    ERROR = "error"
    WARNING = "warning"
    PATTERN = "pattern"
    BURST = "burst"
    CONTEXT = "context"


class MetricEvidenceType(str, Enum):
    """Deterministic extraction category for metric evidence."""
    SPIKE = "spike"
    DROP = "drop"
    THRESHOLD = "threshold"
    PERIOD_CHANGE = "period_change"
    CORRELATION = "correlation"
    ANOMALY = "anomaly"


class CodeEvidenceType(str, Enum):
    """Deterministic extraction category for code evidence."""
    ADDED_CODE = "added_code"
    REMOVED_CODE = "removed_code"
    SUSPICIOUS_PATTERN = "suspicious_pattern"
    CHANGED_CONFIG = "changed_config"


class CodeSourceType(str, Enum):
    """Source origin for code evidence."""
    GIT_DIFF = "git_diff"
    CODE = "code"
    CONFIG = "config"


# ============================================================================
# Base Model
# ============================================================================

class BaseDomainModel(BaseModel):
    """Base domain model with dictionary serialization and deserialization helpers."""
    model_config = ConfigDict(
        populate_by_name=True,
        use_enum_values=True,
        validate_assignment=True,
        extra="allow",
    )

    def to_dict(self, exclude_none: bool = False) -> Dict[str, Any]:
        """Convert domain model instance to a JSON-serializable dictionary."""
        return self.model_dump(mode="json", exclude_none=exclude_none)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> BaseDomainModel:
        """Create a domain model instance from a dictionary."""
        return cls.model_validate(data)


# ============================================================================
# Evidence Models
# ============================================================================

class EvidenceItem(BaseDomainModel):
    """Generic structured evidence item (matches evidence_schema.json)."""
    evidence_id: str = Field(..., pattern=r"^EV-[A-Z0-9_-]+$")
    source_type: Union[EvidenceSourceType, str]
    description: str
    raw_snippet: str
    timestamp: Optional[str] = None
    metadata: Optional[Dict[str, Any]] = None


class LogEvidenceItem(BaseDomainModel):
    """Structured log evidence item produced by LogsAgent (matches logs_agent_schema.json)."""
    evidence_id: str = Field(..., pattern=r"^EV-LOG-[0-9]{3,}$")
    source: str = "logs"
    reference: str
    type: Union[LogEvidenceType, str]
    excerpt: str
    timestamp: Optional[str] = None
    interpretation: Optional[str] = None

    @field_validator("source")
    @classmethod
    def validate_source(cls, v: str) -> str:
        if v != "logs":
            raise ValueError(f"Source for LogEvidenceItem must be 'logs', got '{v}'")
        return v


# Alias for backward/domain convenience
LogEvidence = LogEvidenceItem


class LogsAgentEvidence(BaseDomainModel):
    """Complete bundle of evidence produced by LogsAgent."""
    incident_id: str
    agent: str = "logs_agent"
    summary: Optional[str] = None
    evidence: List[LogEvidenceItem] = Field(default_factory=list)


class MetricEvidenceItem(BaseDomainModel):
    """Structured metric evidence item produced by MetricsAgent (matches metrics_agent_schema.json)."""
    evidence_id: str = Field(..., pattern=r"^EV-MET-[0-9]{3,}$")
    source: str = "metrics"
    reference: str
    metric: str
    value: float
    type: Union[MetricEvidenceType, str]
    timestamp: Optional[str] = None
    interpretation: Optional[str] = None

    @field_validator("source")
    @classmethod
    def validate_source(cls, v: str) -> str:
        if v != "metrics":
            raise ValueError(f"Source for MetricEvidenceItem must be 'metrics', got '{v}'")
        return v


# Alias for backward/domain convenience
MetricEvidence = MetricEvidenceItem


class MetricsAgentEvidence(BaseDomainModel):
    """Complete bundle of evidence produced by MetricsAgent."""
    incident_id: str
    agent: str = "metrics_agent"
    summary: Optional[str] = None
    evidence: List[MetricEvidenceItem] = Field(default_factory=list)


class CodeEvidenceItem(BaseDomainModel):
    """Structured code evidence item produced by CodeAgent (matches code_agent_schema.json)."""
    evidence_id: str = Field(..., pattern=r"^EV-CODE-[0-9]{3,}$")
    source: Union[CodeSourceType, str]
    reference: str
    type: Union[CodeEvidenceType, str]
    excerpt: str
    interpretation: Optional[str] = None


# Alias for backward/domain convenience
CodeEvidence = CodeEvidenceItem


class CodeAgentEvidence(BaseDomainModel):
    """Complete bundle of evidence produced by CodeAgent."""
    incident_id: str
    agent: str = "code_agent"
    summary: Optional[str] = None
    evidence: List[CodeEvidenceItem] = Field(default_factory=list)


# Any evidence item union
AnyEvidenceItem = Union[LogEvidenceItem, MetricEvidenceItem, CodeEvidenceItem, EvidenceItem]


# ============================================================================
# Hypothesis Models
# ============================================================================

class Hypothesis(BaseDomainModel):
    """Single falsifiable root-cause hypothesis (matches hypothesis_schema.json item)."""
    hypothesis_id: str = Field(..., pattern=r"^HYP-[0-9]{3}$")
    claim: str = Field(..., min_length=1)
    evidence_ids: List[str] = Field(..., min_length=1)
    supporting_reasoning: str = Field(..., min_length=1)
    falsification_criteria: List[str] = Field(..., min_length=1)
    verification_plan: List[str] = Field(..., min_length=1)


class HypothesisBundle(BaseDomainModel):
    """Collection of candidate hypotheses for an incident (matches hypothesis_schema.json)."""
    incident_id: str
    hypotheses: List[Hypothesis] = Field(default_factory=list, min_length=1)


# ============================================================================
# Verification Models
# ============================================================================

class VerificationResult(BaseDomainModel):
    """Outcome of an executable verification check (matches verification_schema.json)."""
    verification_id: str = Field(..., pattern=r"^VER-[A-Z0-9_-]+$")
    hypothesis_id: str
    status: Union[VerificationVerdict, str]
    check_type: Union[CheckType, str]
    check_code_or_query: str
    execution_output: str
    verified_evidence_ids: List[str] = Field(default_factory=list)
    reasoning: str


# Alias for domain naming clarity
VerificationCheck = VerificationResult


class VerificationBundle(BaseDomainModel):
    """Collection of verification results for an incident."""
    incident_id: Optional[str] = None
    verification_results: List[VerificationResult] = Field(default_factory=list)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> VerificationBundle:
        # Handle dicts keyed by "results", "verifications", or "verification_results"
        if "results" in data and "verification_results" not in data:
            data = dict(data)
            data["verification_results"] = data.pop("results")
        elif "verifications" in data and "verification_results" not in data:
            data = dict(data)
            data["verification_results"] = data.pop("verifications")
        return cls.model_validate(data)


# ============================================================================
# Fix Proposal Models
# ============================================================================

class FileChange(BaseDomainModel):
    """Individual file change in a proposed fix."""
    file: str
    description: str
    before: str
    after: str
    start_line: Optional[int] = None
    end_line: Optional[int] = None


class FixProposal(BaseDomainModel):
    """Proposed remediation for a confirmed root cause (matches fix_proposal_schema.json)."""
    proposal_id: str = Field(..., pattern=r"^FIX-[0-9]{3}$")
    hypothesis_id: str = Field(..., pattern=r"^HYP-[0-9]{3}$")
    incident_id: str
    status: Union[ProposalStatus, str] = ProposalStatus.PROPOSED
    human_approval_notice: str = "AWAITING HUMAN APPROVAL — this fix has not been applied."
    summary: str = Field(..., min_length=1)
    rationale: str = Field(..., min_length=1)
    changes: List[FileChange] = Field(default_factory=list, min_length=1)
    patch: str = ""
    expected_effect: str = Field(..., min_length=1)
    risks: List[str] = Field(default_factory=list)
    validation_plan: List[str] = Field(default_factory=list, min_length=1)
    rollback_plan: str = Field(..., min_length=1)
    evidence_ids: List[str] = Field(default_factory=list, min_length=1)


class FixProposalBundle(BaseDomainModel):
    """Collection of proposed remediation patches for an incident."""
    incident_id: str
    proposals: List[FixProposal] = Field(default_factory=list)


# ============================================================================
# Approval Models
# ============================================================================

class ApprovalRecord(BaseDomainModel):
    """Human approval gate record (matches approval_schema.json)."""
    proposal_id: str = Field(..., pattern=r"^FIX-[0-9]{3}$")
    status: Union[ApprovalStatus, str]
    decision: Union[ApprovalDecision, str]
    approved_by: str = Field(..., min_length=1)
    timestamp: str
    notes: Optional[str] = None


class ApprovalBundle(BaseDomainModel):
    """Collection of approval gate records for an incident."""
    incident_id: Optional[str] = None
    approvals: List[ApprovalRecord] = Field(default_factory=list)


# ============================================================================
# Stage & Orchestration Result Models
# ============================================================================

class StageResult(BaseDomainModel):
    """Result payload of a single pipeline stage."""
    status: Union[StageStatus, str]
    output: Optional[Any] = None
    error: Optional[str] = None
    llm_calls: int = Field(default=0, ge=0)
    cache_hit: Optional[bool] = None


class InvestigationResultSummary(BaseDomainModel):
    """Summary metrics of an investigation outcome."""
    confirmed_hypotheses: int = 0
    rejected_hypotheses: int = 0
    inconclusive_hypotheses: int = 0
    proposals_generated: int = 0
    proposals_approved: int = 0
    proposals_rejected: int = 0


class InvestigationResult(BaseDomainModel):
    """Complete Sentinel investigation result (matches orchestrator_result_schema.json)."""
    incident_id: str
    pipeline_status: Union[IncidentStatus, str]
    human_approval_notice: str = "AWAITING HUMAN APPROVAL — this fix has not been applied."
    llm_call_count: int = Field(default=0, ge=0)
    prompt_tokens: Optional[int] = None
    completion_tokens: Optional[int] = None
    total_tokens: Optional[int] = None
    stages: Dict[str, StageResult] = Field(default_factory=dict)
    summary: Optional[InvestigationResultSummary] = None
    error: Optional[str] = None
