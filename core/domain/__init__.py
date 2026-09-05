"""Domain package for Sentinel 2.0.

Exposes typed domain models, enums, and InvestigationState.
"""

from core.domain.models import (
    AnyEvidenceItem,
    ApprovalBundle,
    ApprovalDecision,
    ApprovalRecord,
    ApprovalStatus,
    BaseDomainModel,
    CheckResult,
    CheckType,
    CodeAgentEvidence,
    CodeEvidence,
    CodeEvidenceItem,
    CodeEvidenceType,
    CodeSourceType,
    EvidenceItem,
    EvidenceSourceType,
    FileChange,
    FixProposal,
    FixProposalBundle,
    Hypothesis,
    HypothesisBundle,
    IncidentStatus,
    InvestigationResult,
    InvestigationResultSummary,
    LogEvidence,
    LogEvidenceItem,
    LogEvidenceType,
    LogsAgentEvidence,
    MetricEvidence,
    MetricEvidenceItem,
    MetricEvidenceType,
    MetricsAgentEvidence,
    ProposalStatus,
    StageResult,
    StageStatus,
    VerificationBundle,
    VerificationCheck,
    VerificationResult,
    VerificationVerdict,
)
from core.domain.state import (
    InvestigationState,
    StageTransition,
)

__all__ = [
    # Enums
    "IncidentStatus",
    "StageStatus",
    "VerificationVerdict",
    "CheckResult",
    "CheckType",
    "ProposalStatus",
    "ApprovalDecision",
    "ApprovalStatus",
    "EvidenceSourceType",
    "LogEvidenceType",
    "MetricEvidenceType",
    "CodeEvidenceType",
    "CodeSourceType",
    # Base
    "BaseDomainModel",
    # Evidence
    "EvidenceItem",
    "LogEvidenceItem",
    "LogEvidence",
    "LogsAgentEvidence",
    "MetricEvidenceItem",
    "MetricEvidence",
    "MetricsAgentEvidence",
    "CodeEvidenceItem",
    "CodeEvidence",
    "CodeAgentEvidence",
    "AnyEvidenceItem",
    # Hypotheses
    "Hypothesis",
    "HypothesisBundle",
    # Verification
    "VerificationResult",
    "VerificationCheck",
    "VerificationBundle",
    # Fix Proposals
    "FileChange",
    "FixProposal",
    "FixProposalBundle",
    # Approvals
    "ApprovalRecord",
    "ApprovalBundle",
    # Results & Stages
    "StageResult",
    "InvestigationResultSummary",
    "InvestigationResult",
    # State
    "StageTransition",
    "InvestigationState",
]
