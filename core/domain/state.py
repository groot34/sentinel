"""Investigation State and Lifecycle Management for Sentinel 2.0.

Represents the isolated lifecycle of one investigation, tracking stage transitions,
accumulated domain models, and overall investigation status.
Does NOT store raw incident bundles, log files, ground_truth.md, or baseline outputs.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Union

from pydantic import Field, field_validator

from core.domain.models import (
    ApprovalBundle,
    ApprovalRecord,
    BaseDomainModel,
    CodeAgentEvidence,
    CodeEvidenceItem,
    EvidenceItem,
    FixProposal,
    FixProposalBundle,
    Hypothesis,
    HypothesisBundle,
    IncidentStatus,
    LogEvidenceItem,
    LogsAgentEvidence,
    MetricEvidenceItem,
    MetricsAgentEvidence,
    StageResult,
    StageStatus,
    VerificationBundle,
    VerificationResult,
)


def _utcnow_iso() -> str:
    """Return current UTC timestamp in ISO-8601 format."""
    return datetime.now(timezone.utc).isoformat()


# ============================================================================
# Stage Transition Model
# ============================================================================

class StageTransition(BaseDomainModel):
    """Represents a discrete stage transition in the investigation lifecycle."""
    stage: str
    status: Union[StageStatus, str]
    started_at: str = Field(default_factory=_utcnow_iso)
    completed_at: Optional[str] = None
    error: Optional[str] = None
    metadata: Dict[str, Any] = Field(default_factory=dict)


# ============================================================================
# Investigation State Model
# ============================================================================

class InvestigationState(BaseDomainModel):
    """Central domain state container for a single incident investigation."""
    incident_id: str
    status: Union[IncidentStatus, str] = IncidentStatus.RUNNING
    current_stage: Optional[str] = None
    stage_history: List[StageTransition] = Field(default_factory=list)
    stages: Dict[str, StageResult] = Field(default_factory=dict)
    evidence: List[Union[LogEvidenceItem, MetricEvidenceItem, CodeEvidenceItem, EvidenceItem]] = Field(default_factory=list)
    hypotheses: List[Hypothesis] = Field(default_factory=list)
    verification_results: List[VerificationResult] = Field(default_factory=list)
    proposals: List[FixProposal] = Field(default_factory=list)
    approvals: List[ApprovalRecord] = Field(default_factory=list)
    started_at: str = Field(default_factory=_utcnow_iso)
    completed_at: Optional[str] = None
    error: Optional[str] = None
    llm_call_count: int = Field(default=0, ge=0)
    metadata: Dict[str, Any] = Field(default_factory=dict)

    @field_validator("incident_id")
    @classmethod
    def validate_incident_id(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("incident_id cannot be empty")
        return v.strip()

    # -----------------------------------------------------------------------
    # Stage Lifecycle Methods
    # -----------------------------------------------------------------------

    def start_stage(self, stage_name: str, started_at: Optional[str] = None) -> StageTransition:
        """Begin a new stage, recording the transition and setting current_stage."""
        if not stage_name or not stage_name.strip():
            raise ValueError("stage_name cannot be empty")

        if self.status in (IncidentStatus.COMPLETED, IncidentStatus.FAILED):
            raise ValueError(f"Cannot start stage '{stage_name}' on finished investigation in status '{self.status}'")

        if self.current_stage is not None:
            raise ValueError(f"Cannot start stage '{stage_name}': stage '{self.current_stage}' is currently active")

        transition = StageTransition(
            stage=stage_name,
            status=StageStatus.RUNNING,
            started_at=started_at or _utcnow_iso(),
        )
        self.current_stage = stage_name
        self.stage_history.append(transition)
        return transition

    def complete_stage(
        self,
        stage_name: str,
        output: Any = None,
        llm_calls: int = 0,
        completed_at: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> StageTransition:
        """Mark an active stage as SUCCEEDED."""
        if self.current_stage != stage_name:
            raise ValueError(
                f"Cannot complete stage '{stage_name}': current active stage is '{self.current_stage}'"
            )

        # Update last transition for this stage
        transition = self._get_active_transition(stage_name)
        now = completed_at or _utcnow_iso()
        if transition is not None:
            transition.status = StageStatus.SUCCEEDED
            transition.completed_at = now
            if metadata:
                transition.metadata.update(metadata)
        else:
            transition = StageTransition(
                stage=stage_name,
                status=StageStatus.SUCCEEDED,
                started_at=now,
                completed_at=now,
                metadata=metadata or {},
            )
            self.stage_history.append(transition)

        self.stages[stage_name] = StageResult(
            status=StageStatus.SUCCEEDED,
            output=output,
            llm_calls=llm_calls,
        )
        self.llm_call_count += max(0, llm_calls)
        self.current_stage = None
        return transition

    def fail_stage(
        self,
        stage_name: str,
        error: str,
        completed_at: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> StageTransition:
        """Mark an active stage as FAILED."""
        if self.current_stage != stage_name and self.current_stage is not None:
            raise ValueError(
                f"Cannot fail stage '{stage_name}': current active stage is '{self.current_stage}'"
            )

        now = completed_at or _utcnow_iso()
        transition = self._get_active_transition(stage_name)
        if transition is not None:
            transition.status = StageStatus.FAILED
            transition.completed_at = now
            transition.error = error
            if metadata:
                transition.metadata.update(metadata)
        else:
            transition = StageTransition(
                stage=stage_name,
                status=StageStatus.FAILED,
                started_at=now,
                completed_at=now,
                error=error,
                metadata=metadata or {},
            )
            self.stage_history.append(transition)

        self.stages[stage_name] = StageResult(
            status=StageStatus.FAILED,
            error=error,
        )
        self.current_stage = None
        return transition

    def skip_stage(
        self,
        stage_name: str,
        reason: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> StageTransition:
        """Record a skipped stage."""
        now = _utcnow_iso()
        meta = dict(metadata or {})
        if reason:
            meta["reason"] = reason

        transition = StageTransition(
            stage=stage_name,
            status=StageStatus.SKIPPED,
            started_at=now,
            completed_at=now,
            metadata=meta,
        )
        self.stage_history.append(transition)
        self.stages[stage_name] = StageResult(
            status=StageStatus.SKIPPED,
            error=reason,
        )
        return transition

    def mark_cached(
        self,
        stage_name: str,
        output: Any = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> StageTransition:
        """Record a cached stage reuse."""
        now = _utcnow_iso()
        transition = StageTransition(
            stage=stage_name,
            status=StageStatus.CACHED,
            started_at=now,
            completed_at=now,
            metadata=metadata or {},
        )
        self.stage_history.append(transition)
        self.stages[stage_name] = StageResult(
            status=StageStatus.REUSED,
            output=output,
            llm_calls=0,
            cache_hit=True,
        )
        return transition

    def _get_active_transition(self, stage_name: str) -> Optional[StageTransition]:
        """Find the active running transition for stage_name."""
        for t in reversed(self.stage_history):
            if t.stage == stage_name and t.status == StageStatus.RUNNING:
                return t
        return None

    # -----------------------------------------------------------------------
    # Evidence & Artefact Accumulation Methods
    # -----------------------------------------------------------------------

    def add_evidence(
        self,
        evidence: Union[
            EvidenceItem,
            LogEvidenceItem,
            MetricEvidenceItem,
            CodeEvidenceItem,
            LogsAgentEvidence,
            MetricsAgentEvidence,
            CodeAgentEvidence,
            Dict[str, Any],
            List[Any],
        ],
    ) -> None:
        """Add evidence item(s) or agent evidence bundles to the investigation state."""
        if isinstance(evidence, list):
            for item in evidence:
                self.add_evidence(item)
            return

        if isinstance(evidence, (LogsAgentEvidence, MetricsAgentEvidence, CodeAgentEvidence)):
            for item in evidence.evidence:
                self.evidence.append(item)
            return

        if isinstance(evidence, (LogEvidenceItem, MetricEvidenceItem, CodeEvidenceItem, EvidenceItem)):
            self.evidence.append(evidence)
            return

        if isinstance(evidence, dict):
            # Check if it's an agent bundle
            if "agent" in evidence and "evidence" in evidence:
                agent_name = evidence.get("agent")
                if agent_name == "logs_agent":
                    bundle = LogsAgentEvidence.from_dict(evidence)
                    for item in bundle.evidence:
                        self.evidence.append(item)
                    return
                elif agent_name == "metrics_agent":
                    bundle = MetricsAgentEvidence.from_dict(evidence)
                    for item in bundle.evidence:
                        self.evidence.append(item)
                    return
                elif agent_name == "code_agent":
                    bundle = CodeAgentEvidence.from_dict(evidence)
                    for item in bundle.evidence:
                        self.evidence.append(item)
                    return

            # Check individual item type by ID pattern or source field
            ev_id = evidence.get("evidence_id", "")
            if ev_id.startswith("EV-LOG-"):
                self.evidence.append(LogEvidenceItem.from_dict(evidence))
            elif ev_id.startswith("EV-MET-"):
                self.evidence.append(MetricEvidenceItem.from_dict(evidence))
            elif ev_id.startswith("EV-CODE-"):
                self.evidence.append(CodeEvidenceItem.from_dict(evidence))
            elif "source_type" in evidence:
                self.evidence.append(EvidenceItem.from_dict(evidence))
            else:
                # Generic fallback if required fields present
                if "evidence_id" in evidence:
                    self.evidence.append(EvidenceItem.from_dict({
                        "evidence_id": evidence["evidence_id"],
                        "source_type": evidence.get("source", "logs"),
                        "description": evidence.get("description", evidence.get("excerpt", "")),
                        "raw_snippet": evidence.get("raw_snippet", evidence.get("excerpt", "")),
                        "timestamp": evidence.get("timestamp"),
                        "metadata": evidence.get("metadata"),
                    }))

    def add_hypotheses(
        self,
        hypotheses: Union[Hypothesis, HypothesisBundle, Dict[str, Any], List[Any]],
    ) -> None:
        """Add hypothesis item(s) or bundle to the state."""
        if isinstance(hypotheses, list):
            for item in hypotheses:
                self.add_hypotheses(item)
            return

        if isinstance(hypotheses, HypothesisBundle):
            self.hypotheses.extend(hypotheses.hypotheses)
            return

        if isinstance(hypotheses, Hypothesis):
            self.hypotheses.append(hypotheses)
            return

        if isinstance(hypotheses, dict):
            if "hypotheses" in hypotheses and isinstance(hypotheses["hypotheses"], list):
                bundle = HypothesisBundle.from_dict(hypotheses)
                self.hypotheses.extend(bundle.hypotheses)
            else:
                self.hypotheses.append(Hypothesis.from_dict(hypotheses))

    def add_verification_results(
        self,
        results: Union[VerificationResult, VerificationBundle, Dict[str, Any], List[Any]],
    ) -> None:
        """Add verification result(s) or bundle to the state."""
        if isinstance(results, list):
            for item in results:
                self.add_verification_results(item)
            return

        if isinstance(results, VerificationBundle):
            self.verification_results.extend(results.verification_results)
            return

        if isinstance(results, VerificationResult):
            self.verification_results.append(results)
            return

        if isinstance(results, dict):
            if "verification_results" in results or "verifications" in results or "results" in results:
                bundle = VerificationBundle.from_dict(results)
                self.verification_results.extend(bundle.verification_results)
            else:
                self.verification_results.append(VerificationResult.from_dict(results))

    def add_proposals(
        self,
        proposals: Union[FixProposal, FixProposalBundle, Dict[str, Any], List[Any]],
    ) -> None:
        """Add fix proposal(s) or bundle to the state."""
        if isinstance(proposals, list):
            for item in proposals:
                self.add_proposals(item)
            return

        if isinstance(proposals, FixProposalBundle):
            self.proposals.extend(proposals.proposals)
            return

        if isinstance(proposals, FixProposal):
            self.proposals.append(proposals)
            return

        if isinstance(proposals, dict):
            if "proposals" in proposals and isinstance(proposals["proposals"], list):
                bundle = FixProposalBundle.from_dict(proposals)
                self.proposals.extend(bundle.proposals)
            else:
                self.proposals.append(FixProposal.from_dict(proposals))

    def record_approval(
        self,
        approval: Union[ApprovalRecord, ApprovalBundle, Dict[str, Any], List[Any]],
    ) -> None:
        """Record human approval gate decision(s)."""
        if isinstance(approval, list):
            for item in approval:
                self.record_approval(item)
            return

        if isinstance(approval, ApprovalBundle):
            self.approvals.extend(approval.approvals)
            return

        if isinstance(approval, ApprovalRecord):
            self.approvals.append(approval)
            return

        if isinstance(approval, dict):
            if "approvals" in approval and isinstance(approval["approvals"], list):
                bundle = ApprovalBundle.from_dict(approval)
                self.approvals.extend(bundle.approvals)
            else:
                self.approvals.append(ApprovalRecord.from_dict(approval))

    # -----------------------------------------------------------------------
    # Final Investigation Lifecycle Methods
    # -----------------------------------------------------------------------

    def complete(self, completed_at: Optional[str] = None) -> None:
        """Transition investigation state to COMPLETED."""
        if self.status in (IncidentStatus.COMPLETED, IncidentStatus.FAILED):
            raise ValueError(f"Cannot complete investigation already in terminal state '{self.status}'")
        self.status = IncidentStatus.COMPLETED
        self.completed_at = completed_at or _utcnow_iso()
        self.current_stage = None

    def mark_partial(self, completed_at: Optional[str] = None) -> None:
        """Transition investigation state to PARTIAL (degraded or partial run)."""
        if self.status in (IncidentStatus.COMPLETED, IncidentStatus.FAILED):
            raise ValueError(f"Cannot mark partial on investigation already in terminal state '{self.status}'")
        self.status = IncidentStatus.PARTIAL
        self.completed_at = completed_at or _utcnow_iso()
        self.current_stage = None

    def fail(self, error: Optional[str] = None, completed_at: Optional[str] = None) -> None:
        """Transition investigation state to FAILED."""
        self.status = IncidentStatus.FAILED
        self.completed_at = completed_at or _utcnow_iso()
        self.current_stage = None
        if error:
            self.error = error
