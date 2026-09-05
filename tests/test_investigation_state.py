"""Unit tests for Sentinel 2.0 Investigation State (core/domain/state.py).

Verifies lifecycle state transitions, data accumulation, validation rules,
error handling, and safety invariants (ground truth & baseline output isolation).
"""

from __future__ import annotations

import json
from typing import Any, Dict
import pytest
from pydantic import ValidationError

from core.domain.models import (
    ApprovalRecord,
    CheckType,
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
    StageStatus,
    VerificationBundle,
    VerificationResult,
    VerificationVerdict,
)
from core.domain.state import InvestigationState, StageTransition


# ============================================================================
# Initial State & Validation Tests
# ============================================================================

def test_initial_state_defaults() -> None:
    state = InvestigationState(incident_id="inc_01_n_plus_one_query")
    assert state.incident_id == "inc_01_n_plus_one_query"
    assert state.status == IncidentStatus.RUNNING
    assert state.current_stage is None
    assert len(state.stage_history) == 0
    assert len(state.evidence) == 0
    assert len(state.hypotheses) == 0
    assert len(state.verification_results) == 0
    assert len(state.proposals) == 0
    assert len(state.approvals) == 0
    assert state.completed_at is None
    assert state.error is None
    assert state.llm_call_count == 0


def test_empty_incident_id_rejected() -> None:
    with pytest.raises(ValidationError):
        InvestigationState(incident_id="")


# ============================================================================
# Stage Lifecycle Tests
# ============================================================================

def test_stage_start_complete_lifecycle() -> None:
    state = InvestigationState(incident_id="inc_01")
    
    # 1. Start stage
    t1 = state.start_stage("logs")
    assert state.current_stage == "logs"
    assert t1.stage == "logs"
    assert t1.status == StageStatus.RUNNING
    assert len(state.stage_history) == 1

    # 2. Complete stage
    t2 = state.complete_stage("logs", output={"logs_found": 5}, llm_calls=1)
    assert state.current_stage is None
    assert t2.status == StageStatus.SUCCEEDED
    assert t2.completed_at is not None
    assert state.llm_call_count == 1
    assert "logs" in state.stages
    assert state.stages["logs"].status == StageStatus.SUCCEEDED
    assert state.stages["logs"].output == {"logs_found": 5}


def test_stage_fail_lifecycle() -> None:
    state = InvestigationState(incident_id="inc_01")
    state.start_stage("metrics")
    
    t = state.fail_stage("metrics", error="CSV parsing error")
    assert state.current_stage is None
    assert t.status == StageStatus.FAILED
    assert t.error == "CSV parsing error"
    assert state.stages["metrics"].status == StageStatus.FAILED
    assert state.stages["metrics"].error == "CSV parsing error"


def test_stage_skip_and_cache_lifecycle() -> None:
    state = InvestigationState(incident_id="inc_01")
    
    # Skip
    t_skip = state.skip_stage("code", reason="No diff present in bundle")
    assert t_skip.status == StageStatus.SKIPPED
    assert state.stages["code"].status == StageStatus.SKIPPED

    # Cached
    t_cache = state.mark_cached("hypotheses", output={"hypotheses": []})
    assert t_cache.status == StageStatus.CACHED
    assert state.stages["hypotheses"].status == StageStatus.REUSED
    assert state.stages["hypotheses"].cache_hit is True


def test_invalid_stage_transitions_rejected() -> None:
    state = InvestigationState(incident_id="inc_01")

    # 1. Cannot start empty stage name
    with pytest.raises(ValueError):
        state.start_stage("")

    # 2. Cannot complete stage that was never started
    with pytest.raises(ValueError):
        state.complete_stage("logs")

    # 3. Cannot start another stage while one is running
    state.start_stage("logs")
    with pytest.raises(ValueError):
        state.start_stage("metrics")

    # 4. Cannot complete wrong stage name
    with pytest.raises(ValueError):
        state.complete_stage("metrics")

    # 5. Completing logs clears active stage
    state.complete_stage("logs")

    # 6. Cannot start new stage after investigation is marked completed
    state.complete()
    with pytest.raises(ValueError):
        state.start_stage("hypotheses")


# ============================================================================
# Evidence & Artefact Accumulation Tests
# ============================================================================

def test_add_evidence_items_and_bundles() -> None:
    state = InvestigationState(incident_id="inc_01")

    # Add single typed items
    log_item = LogEvidenceItem(
        evidence_id="EV-LOG-001",
        source="logs",
        reference="logs/app.log:1",
        type="error",
        excerpt="Connection timeout",
    )
    metric_item = MetricEvidenceItem(
        evidence_id="EV-MET-001",
        source="metrics",
        reference="metrics/m.csv:row 2",
        metric="latency_p95",
        value=5000.0,
        type="spike",
    )
    code_item = CodeEvidenceItem(
        evidence_id="EV-CODE-001",
        source="code",
        reference="app.py:10",
        type="suspicious_pattern",
        excerpt="db.query() inside loop",
    )

    state.add_evidence(log_item)
    state.add_evidence(metric_item)
    state.add_evidence(code_item)
    assert len(state.evidence) == 3

    # Add via agent bundle dicts
    state.add_evidence({
        "incident_id": "inc_01",
        "agent": "logs_agent",
        "evidence": [
            {
                "evidence_id": "EV-LOG-002",
                "source": "logs",
                "reference": "logs/app.log:2",
                "type": "error",
                "excerpt": "Retry attempt failed",
            }
        ]
    })
    assert len(state.evidence) == 4
    assert state.evidence[3].evidence_id == "EV-LOG-002"


def test_add_hypotheses_and_bundles() -> None:
    state = InvestigationState(incident_id="inc_01")

    hyp = Hypothesis(
        hypothesis_id="HYP-001",
        claim="Connection pool leak in retry loop.",
        evidence_ids=["EV-LOG-001", "EV-MET-001"],
        supporting_reasoning="Errors correlate with pool exhaustion.",
        falsification_criteria=["Pool size remains stable under load."],
        verification_plan=["Check unreleased connection metrics."],
    )
    state.add_hypotheses(hyp)
    assert len(state.hypotheses) == 1

    # Add via bundle dict
    state.add_hypotheses({
        "incident_id": "inc_01",
        "hypotheses": [
            {
                "hypothesis_id": "HYP-002",
                "claim": "Upstream service timeout.",
                "evidence_ids": ["EV-LOG-002"],
                "supporting_reasoning": "Upstream error 504.",
                "falsification_criteria": ["Upstream responded < 50ms."],
                "verification_plan": ["Inspect upstream latency logs."],
            }
        ]
    })
    assert len(state.hypotheses) == 2


def test_add_verification_results_and_bundles() -> None:
    state = InvestigationState(incident_id="inc_01")

    ver = VerificationResult(
        verification_id="VER-001",
        hypothesis_id="HYP-001",
        status=VerificationVerdict.CONFIRMED,
        check_type=CheckType.CODE_INVARIANT,
        check_code_or_query="assert leaks > 0",
        execution_output="Passed: 10 leaked connections found.",
        verified_evidence_ids=["EV-LOG-001"],
        reasoning="Confirmed connection leak.",
    )
    state.add_verification_results(ver)
    assert len(state.verification_results) == 1

    # Add via bundle dict
    state.add_verification_results({
        "incident_id": "inc_01",
        "verification_results": [
            {
                "verification_id": "VER-002",
                "hypothesis_id": "HYP-002",
                "status": "REJECTED",
                "check_type": "log_sequence",
                "check_code_or_query": "check sequence",
                "execution_output": "Failed: upstream was healthy.",
                "verified_evidence_ids": [],
                "reasoning": "Upstream was reachable with 200 OK.",
            }
        ]
    })
    assert len(state.verification_results) == 2


def test_add_proposals_and_bundles() -> None:
    state = InvestigationState(incident_id="inc_01")

    prop = FixProposal(
        proposal_id="FIX-001",
        hypothesis_id="HYP-001",
        incident_id="inc_01",
        status="PROPOSED",
        summary="Use connection context manager.",
        rationale="Ensures connections are returned to the pool.",
        changes=[{
            "file": "app.py",
            "description": "wrap in with db:",
            "before": "conn = db.get()",
            "after": "with db.get() as conn:",
        }],
        expected_effect="Connection leaks drop to 0.",
        validation_plan=["Run unit test."],
        rollback_plan="Revert patch.",
        evidence_ids=["EV-LOG-001"],
    )
    state.add_proposals(prop)
    assert len(state.proposals) == 1


def test_record_approvals() -> None:
    state = InvestigationState(incident_id="inc_01")

    appr = ApprovalRecord(
        proposal_id="FIX-001",
        status="APPROVED",
        decision="approved",
        approved_by="tech-lead",
        timestamp="2026-08-28T16:00:00Z",
        notes="Looks good",
    )
    state.record_approval(appr)
    assert len(state.approvals) == 1
    assert state.approvals[0].decision == "approved"


# ============================================================================
# Final Investigation State Updates (complete, mark_partial, fail)
# ============================================================================

def test_complete_updates_state() -> None:
    state = InvestigationState(incident_id="inc_01")
    state.complete(completed_at="2026-08-28T18:00:00Z")
    assert state.status == IncidentStatus.COMPLETED
    assert state.completed_at == "2026-08-28T18:00:00Z"

    # Cannot complete twice
    with pytest.raises(ValueError):
        state.complete()


def test_mark_partial_updates_state() -> None:
    state = InvestigationState(incident_id="inc_01")
    state.mark_partial()
    assert state.status == IncidentStatus.PARTIAL
    assert state.completed_at is not None


def test_fail_updates_state() -> None:
    state = InvestigationState(incident_id="inc_01")
    state.fail(error="Orchestrator unhandled fatal exception")
    assert state.status == IncidentStatus.FAILED
    assert state.error == "Orchestrator unhandled fatal exception"
    assert state.completed_at is not None


# ============================================================================
# Safety & Isolation Invariant Tests
# ============================================================================

def test_state_serialization_does_not_expose_ground_truth() -> None:
    state = InvestigationState(incident_id="inc_01")
    state.add_evidence(LogEvidenceItem(
        evidence_id="EV-LOG-001",
        source="logs",
        reference="logs/app.log:1",
        type="error",
        excerpt="Connection timeout",
    ))
    
    dumped = state.to_dict()
    dumped_str = json.dumps(dumped)

    # Invariants: ground_truth.md and baseline output paths must never be present
    assert "ground_truth.md" not in dumped_str
    assert "baseline" not in dumped_str.lower()
    assert "root_cause_guess" not in dumped_str


def test_state_round_trip_serialization() -> None:
    state = InvestigationState(incident_id="inc_01")
    state.start_stage("logs")
    state.complete_stage("logs", output={"found": 1}, llm_calls=1)
    state.add_evidence(LogEvidenceItem(
        evidence_id="EV-LOG-001",
        source="logs",
        reference="logs/app.log:1",
        type="error",
        excerpt="Connection timeout",
    ))
    state.complete()

    data = state.to_dict()
    restored = InvestigationState.from_dict(data)

    assert restored.incident_id == state.incident_id
    assert restored.status == IncidentStatus.COMPLETED
    assert len(restored.stage_history) == 1
    assert restored.stage_history[0].status == StageStatus.SUCCEEDED
    assert len(restored.evidence) == 1
    assert restored.evidence[0].evidence_id == "EV-LOG-001"
