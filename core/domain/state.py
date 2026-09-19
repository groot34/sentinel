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
    version: int = Field(default=1, ge=1)
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
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
        total_tokens: int = 0,
        completed_at: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> StageTransition:
        """Mark an active stage as SUCCEEDED."""
        if self.current_stage != stage_name:
            raise ValueError(
                f"Cannot complete stage '{stage_name}': current active stage is '{self.current_stage}'"
            )

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
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=total_tokens,
        )
        self.llm_call_count += max(0, llm_calls)
        self.current_stage = None
        return transition

    def fail_stage(
        self,
        stage_name: str,
        error: str,
        output: Any = None,
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
            output=output,
            error=error,
            llm_calls=0,
            prompt_tokens=0,
            completion_tokens=0,
            total_tokens=0,
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
            llm_calls=0,
            prompt_tokens=0,
            completion_tokens=0,
            total_tokens=0,
        )
        if self.current_stage == stage_name:
            self.current_stage = None
        return transition

    def mark_cached(
        self,
        stage_name: str,
        output: Any = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> StageTransition:
        """Record a cached stage reuse."""
        now = _utcnow_iso()
        transition = self._get_active_transition(stage_name)
        if transition is not None:
            transition.status = StageStatus.CACHED
            transition.completed_at = now
            if metadata:
                transition.metadata.update(metadata)
        else:
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
            prompt_tokens=0,
            completion_tokens=0,
            total_tokens=0,
            cache_hit=True,
        )
        if self.current_stage == stage_name:
            self.current_stage = None
        return transition

    def _get_active_transition(self, stage_name: str) -> Optional[StageTransition]:
        """Find the active running transition for stage_name."""
        for t in reversed(self.stage_history):
            if t.stage == stage_name and t.status == StageStatus.RUNNING:
                return t
        return None

    # -----------------------------------------------------------------------
    # Evidence & Artefact Accumulation Methods (Deterministic Deduplication)
    # -----------------------------------------------------------------------

    def _dict_to_evidence_item(self, ev_dict: Dict[str, Any]) -> Optional[Union[LogEvidenceItem, MetricEvidenceItem, CodeEvidenceItem, EvidenceItem]]:
        """Convert an evidence dictionary to a strongly typed evidence item."""
        if not isinstance(ev_dict, dict):
            return None
        ev_id = ev_dict.get("evidence_id", "")
        if ev_id.startswith("EV-LOG-"):
            return LogEvidenceItem.from_dict(ev_dict)
        elif ev_id.startswith("EV-MET-"):
            return MetricEvidenceItem.from_dict(ev_dict)
        elif ev_id.startswith("EV-CODE-"):
            return CodeEvidenceItem.from_dict(ev_dict)
        elif "source_type" in ev_dict:
            return EvidenceItem.from_dict(ev_dict)
        elif "evidence_id" in ev_dict:
            return EvidenceItem.from_dict({
                "evidence_id": ev_dict["evidence_id"],
                "source_type": ev_dict.get("source", "logs"),
                "description": ev_dict.get("description", ev_dict.get("excerpt", "")),
                "raw_snippet": ev_dict.get("raw_snippet", ev_dict.get("excerpt", "")),
                "timestamp": ev_dict.get("timestamp"),
                "metadata": ev_dict.get("metadata"),
            })
        return None

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
        """Add evidence item(s) or bundles to state with deterministic ID deduplication."""
        if isinstance(evidence, list):
            for item in evidence:
                self.add_evidence(item)
            return

        items_to_add: List[Union[LogEvidenceItem, MetricEvidenceItem, CodeEvidenceItem, EvidenceItem]] = []

        if isinstance(evidence, (LogsAgentEvidence, MetricsAgentEvidence, CodeAgentEvidence)):
            items_to_add = list(evidence.evidence)
        elif isinstance(evidence, (LogEvidenceItem, MetricEvidenceItem, CodeEvidenceItem, EvidenceItem)):
            items_to_add = [evidence]
        elif isinstance(evidence, dict):
            if "agent" in evidence and "evidence" in evidence:
                agent_name = evidence.get("agent")
                if agent_name == "logs_agent":
                    try:
                        bundle = LogsAgentEvidence.from_dict(evidence)
                        items_to_add = list(bundle.evidence)
                    except Exception:
                        items_to_add = [self._dict_to_evidence_item(ev) for ev in evidence.get("evidence", []) if isinstance(ev, dict)]
                elif agent_name == "metrics_agent":
                    try:
                        bundle = MetricsAgentEvidence.from_dict(evidence)
                        items_to_add = list(bundle.evidence)
                    except Exception:
                        items_to_add = [self._dict_to_evidence_item(ev) for ev in evidence.get("evidence", []) if isinstance(ev, dict)]
                elif agent_name == "code_agent":
                    try:
                        bundle = CodeAgentEvidence.from_dict(evidence)
                        items_to_add = list(bundle.evidence)
                    except Exception:
                        items_to_add = [self._dict_to_evidence_item(ev) for ev in evidence.get("evidence", []) if isinstance(ev, dict)]
                else:
                    items_to_add = [self._dict_to_evidence_item(ev) for ev in evidence.get("evidence", []) if isinstance(ev, dict)]
            elif "evidence" in evidence and isinstance(evidence["evidence"], list):
                items_to_add = [self._dict_to_evidence_item(ev) for ev in evidence["evidence"] if isinstance(ev, dict)]
            else:
                single_item = self._dict_to_evidence_item(evidence)
                if single_item is not None:
                    items_to_add = [single_item]

        seen_ids = {e.evidence_id for e in self.evidence if hasattr(e, "evidence_id")}
        for item in items_to_add:
            if item is not None and hasattr(item, "evidence_id") and item.evidence_id not in seen_ids:
                self.evidence.append(item)
                seen_ids.add(item.evidence_id)

    def add_hypotheses(
        self,
        hypotheses: Union[Hypothesis, HypothesisBundle, Dict[str, Any], List[Any]],
    ) -> None:
        """Add hypothesis item(s) or bundle to the state with deterministic ID deduplication."""
        if isinstance(hypotheses, list):
            for item in hypotheses:
                self.add_hypotheses(item)
            return

        items_to_add: List[Hypothesis] = []
        if isinstance(hypotheses, HypothesisBundle):
            items_to_add = list(hypotheses.hypotheses)
        elif isinstance(hypotheses, Hypothesis):
            items_to_add = [hypotheses]
        elif isinstance(hypotheses, dict):
            if "hypotheses" in hypotheses and isinstance(hypotheses["hypotheses"], list):
                bundle = HypothesisBundle.from_dict(hypotheses)
                items_to_add = list(bundle.hypotheses)
            else:
                items_to_add = [Hypothesis.from_dict(hypotheses)]

        seen_ids = {h.hypothesis_id for h in self.hypotheses if hasattr(h, "hypothesis_id")}
        for item in items_to_add:
            if item is not None and hasattr(item, "hypothesis_id") and item.hypothesis_id not in seen_ids:
                self.hypotheses.append(item)
                seen_ids.add(item.hypothesis_id)

    def add_verification_results(
        self,
        results: Union[VerificationResult, VerificationBundle, Dict[str, Any], List[Any]],
    ) -> None:
        """Add verification result(s) or bundle to state with deterministic deduplication."""
        if isinstance(results, list):
            for item in results:
                self.add_verification_results(item)
            return

        items_to_add: List[VerificationResult] = []

        if isinstance(results, VerificationBundle):
            items_to_add = list(results.verification_results)
        elif isinstance(results, VerificationResult):
            items_to_add = [results]
        elif isinstance(results, dict):
            raw_list = results.get("verification_results") or results.get("verifications") or results.get("results")
            if isinstance(raw_list, list):
                for r in raw_list:
                    if isinstance(r, dict):
                        if "verification_id" in r:
                            items_to_add.append(VerificationResult.from_dict(r))
                        elif "hypothesis_id" in r and "verdict" in r:
                            # Aggregate verification agent output shape
                            hyp_id = r.get("hypothesis_id", "HYP-001")
                            verdict = r.get("verdict", "CONFIRMED")
                            reasoning = r.get("reasoning", "")
                            checks = r.get("checks") or []
                            if checks:
                                for i, chk in enumerate(checks, start=1):
                                    chk_id = chk.get("check_id") or f"CHK-{i:03d}"
                                    v_id = f"VER-{hyp_id.replace('-', '')}-{chk_id.replace('-', '')}"
                                    items_to_add.append(VerificationResult(
                                        verification_id=v_id,
                                        hypothesis_id=hyp_id,
                                        status=verdict,
                                        check_type=chk.get("check_type", "code_invariant"),
                                        check_code_or_query=chk.get("description", "verification check"),
                                        execution_output=chk.get("detail") or f"Result: {chk.get('result', 'PASS')}",
                                        verified_evidence_ids=chk.get("evidence") or [],
                                        reasoning=reasoning,
                                    ))
                            else:
                                v_id = f"VER-{hyp_id.replace('-', '')}-001"
                                items_to_add.append(VerificationResult(
                                    verification_id=v_id,
                                    hypothesis_id=hyp_id,
                                    status=verdict,
                                    check_type="code_invariant",
                                    check_code_or_query="verification check",
                                    execution_output=f"Verdict: {verdict}",
                                    verified_evidence_ids=[],
                                    reasoning=reasoning,
                                ))
            else:
                if "verification_id" in results:
                    items_to_add = [VerificationResult.from_dict(results)]

        seen_ids = {
            getattr(v, "verification_id", None) or getattr(v, "hypothesis_id", None)
            for v in self.verification_results
        }
        for item in items_to_add:
            item_id = getattr(item, "verification_id", None) or getattr(item, "hypothesis_id", None)
            if item is not None and (item_id is None or item_id not in seen_ids):
                self.verification_results.append(item)
                if item_id is not None:
                    seen_ids.add(item_id)

    def add_proposals(
        self,
        proposals: Union[FixProposal, FixProposalBundle, Dict[str, Any], List[Any]],
    ) -> None:
        """Add fix proposal(s) or bundle to state with deterministic ID deduplication."""
        if isinstance(proposals, list):
            for item in proposals:
                self.add_proposals(item)
            return

        items_to_add: List[FixProposal] = []
        if isinstance(proposals, FixProposalBundle):
            items_to_add = list(proposals.proposals)
        elif isinstance(proposals, FixProposal):
            items_to_add = [proposals]
        elif isinstance(proposals, dict):
            if "proposals" in proposals and isinstance(proposals["proposals"], list):
                bundle = FixProposalBundle.from_dict(proposals)
                items_to_add = list(bundle.proposals)
            else:
                items_to_add = [FixProposal.from_dict(proposals)]

        seen_ids = {p.proposal_id for p in self.proposals if hasattr(p, "proposal_id")}
        for item in items_to_add:
            if item is not None and hasattr(item, "proposal_id") and item.proposal_id not in seen_ids:
                self.proposals.append(item)
                seen_ids.add(item.proposal_id)

    def record_approval(
        self,
        approval: Union[ApprovalRecord, ApprovalBundle, Dict[str, Any], List[Any]],
    ) -> None:
        """Record human approval gate decision(s) with deterministic ID deduplication."""
        if isinstance(approval, list):
            for item in approval:
                self.record_approval(item)
            return

        items_to_add: List[ApprovalRecord] = []
        if isinstance(approval, ApprovalBundle):
            items_to_add = list(approval.approvals)
        elif isinstance(approval, ApprovalRecord):
            items_to_add = [approval]
        elif isinstance(approval, dict):
            raw_list = approval.get("approvals") or approval.get("approval_records")
            if isinstance(raw_list, list):
                for item in raw_list:
                    if isinstance(item, dict):
                        items_to_add.append(ApprovalRecord.from_dict(item))
            elif "proposal_id" in approval:
                items_to_add = [ApprovalRecord.from_dict(approval)]

        seen_ids = {a.proposal_id for a in self.approvals if hasattr(a, "proposal_id")}
        for item in items_to_add:
            if item is not None and hasattr(item, "proposal_id") and item.proposal_id not in seen_ids:
                self.approvals.append(item)
                seen_ids.add(item.proposal_id)

    def update_approval(
        self,
        record: Union[ApprovalRecord, Dict[str, Any]],
    ) -> None:
        """Update an existing approval record for a proposal or add it if not present."""
        if isinstance(record, dict):
            record = ApprovalRecord.from_dict(record)
        if not isinstance(record, ApprovalRecord):
            return

        for idx, a in enumerate(self.approvals):
            if a.proposal_id == record.proposal_id:
                self.approvals[idx] = record
                return
        self.approvals.append(record)

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
