"""Unit tests for Sentinel 2.0 Domain Models (core/domain/models.py).

Verifies schema compatibility, round-trip dictionary serialization, validation rules,
and enum invariants for all internal domain models.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict
import pytest
from pydantic import ValidationError

from core.domain.models import (
    ApprovalBundle,
    ApprovalDecision,
    ApprovalRecord,
    ApprovalStatus,
    CheckType,
    CodeAgentEvidence,
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
    LogEvidenceItem,
    LogEvidenceType,
    LogsAgentEvidence,
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

SCHEMAS_DIR = Path(__file__).parent.parent / "schemas"


# ============================================================================
# Evidence Models Tests
# ============================================================================

def test_evidence_item_round_trip() -> None:
    raw = {
        "evidence_id": "EV-LOG-001",
        "source_type": "logs",
        "timestamp": "2026-08-28T12:00:00Z",
        "description": "500 Internal Server Error spikes in auth service",
        "raw_snippet": "ERROR 2026-08-28T12:00:00Z [auth-svc] Connection pool timeout after 30000ms",
        "metadata": {"service": "auth-svc", "severity": "ERROR"},
    }
    model = EvidenceItem.from_dict(raw)
    assert model.evidence_id == "EV-LOG-001"
    assert model.source_type == "logs"
    assert model.metadata == {"service": "auth-svc", "severity": "ERROR"}

    dumped = model.to_dict(exclude_none=True)
    assert dumped == raw


def test_logs_agent_evidence_round_trip() -> None:
    raw = {
        "incident_id": "inc_01_n_plus_one_query",
        "agent": "logs_agent",
        "summary": "Errors show pool exhaustion after bulk queries.",
        "evidence": [
            {
                "evidence_id": "EV-LOG-001",
                "source": "logs",
                "reference": "logs/application.log:9",
                "timestamp": "2026-08-28T14:10:30Z",
                "type": "error",
                "excerpt": "ERROR [order-service] [db-pool] Pool exhausted: 20/20 active connections",
                "interpretation": "Connection pool is fully occupied.",
            }
        ],
    }
    model = LogsAgentEvidence.from_dict(raw)
    assert model.incident_id == "inc_01_n_plus_one_query"
    assert model.agent == "logs_agent"
    assert len(model.evidence) == 1
    assert model.evidence[0].evidence_id == "EV-LOG-001"
    assert model.evidence[0].type == "error"

    dumped = model.to_dict(exclude_none=True)
    assert dumped == raw


def test_metrics_agent_evidence_round_trip() -> None:
    raw = {
        "incident_id": "inc_01_n_plus_one_query",
        "agent": "metrics_agent",
        "summary": "Latency and connection count rose versus the early window.",
        "evidence": [
            {
                "evidence_id": "EV-MET-001",
                "source": "metrics",
                "reference": "metrics/metrics.csv:row 9",
                "timestamp": "2026-08-28T14:12:00Z",
                "metric": "latency_p95_ms",
                "value": 10000.0,
                "type": "spike",
                "interpretation": "p95 latency reached 10000ms.",
            }
        ],
    }
    model = MetricsAgentEvidence.from_dict(raw)
    assert model.incident_id == "inc_01_n_plus_one_query"
    assert len(model.evidence) == 1
    assert model.evidence[0].metric == "latency_p95_ms"
    assert model.evidence[0].value == 10000.0

    dumped = model.to_dict(exclude_none=True)
    assert dumped == raw


def test_code_agent_evidence_round_trip() -> None:
    raw = {
        "incident_id": "inc_01_n_plus_one_query",
        "agent": "code_agent",
        "summary": "Added per-item DB query inside serialization loop.",
        "evidence": [
            {
                "evidence_id": "EV-CODE-001",
                "source": "code",
                "reference": "service/app.py:40-42",
                "type": "suspicious_pattern",
                "excerpt": "for item in order.items:\n    address = db.query(item.id)",
                "interpretation": "DB call inside loop.",
            }
        ],
    }
    model = CodeAgentEvidence.from_dict(raw)
    assert model.incident_id == "inc_01_n_plus_one_query"
    assert len(model.evidence) == 1
    assert model.evidence[0].evidence_id == "EV-CODE-001"

    dumped = model.to_dict(exclude_none=True)
    assert dumped == raw


# ============================================================================
# Hypothesis Models Tests
# ============================================================================

def test_hypothesis_bundle_round_trip() -> None:
    raw = {
        "incident_id": "inc_01_n_plus_one_query",
        "hypotheses": [
            {
                "hypothesis_id": "HYP-001",
                "claim": "A database query inside the order-item loop caused excessive query volume and pool exhaustion.",
                "evidence_ids": ["EV-LOG-001", "EV-MET-002", "EV-CODE-003"],
                "supporting_reasoning": "Logs show pool exhaustion, metrics show connection spikes, code shows loop query.",
                "falsification_criteria": [
                    "If query count does not increase with item count, reject this hypothesis."
                ],
                "verification_plan": [
                    "Inspect the serializer query path using AST analysis."
                ],
            }
        ],
    }
    model = HypothesisBundle.from_dict(raw)
    assert model.incident_id == "inc_01_n_plus_one_query"
    assert len(model.hypotheses) == 1
    assert model.hypotheses[0].hypothesis_id == "HYP-001"

    dumped = model.to_dict(exclude_none=True)
    assert dumped == raw


# ============================================================================
# Verification Models Tests
# ============================================================================

def test_verification_result_round_trip() -> None:
    raw = {
        "verification_id": "VER-001",
        "hypothesis_id": "HYP-001",
        "status": "CONFIRMED",
        "check_type": "code_invariant",
        "check_code_or_query": "assert unclosed_connections_count > 0",
        "execution_output": "Check passed: 42 connections leaked.",
        "verified_evidence_ids": ["EV-LOG-001", "EV-CODE-002"],
        "reasoning": "Metric series confirms pool exhaustion precisely correlates with retry exception timestamps.",
    }
    model = VerificationResult.from_dict(raw)
    assert model.verification_id == "VER-001"
    assert model.status == "CONFIRMED"
    assert isinstance(model, VerificationCheck)

    dumped = model.to_dict(exclude_none=True)
    assert dumped == raw


def test_verification_bundle_aliases() -> None:
    bundle1 = VerificationBundle.from_dict({
        "incident_id": "inc_01",
        "results": [
            {
                "verification_id": "VER-001",
                "hypothesis_id": "HYP-001",
                "status": "CONFIRMED",
                "check_type": "code_invariant",
                "check_code_or_query": "assert True",
                "execution_output": "ok",
                "verified_evidence_ids": ["EV-LOG-001"],
                "reasoning": "verified",
            }
        ]
    })
    assert len(bundle1.verification_results) == 1
    assert bundle1.verification_results[0].verification_id == "VER-001"


# ============================================================================
# Fix Proposal Models Tests
# ============================================================================

def test_fix_proposal_bundle_round_trip() -> None:
    raw = {
        "incident_id": "inc_01_n_plus_one_query",
        "proposals": [
            {
                "proposal_id": "FIX-001",
                "hypothesis_id": "HYP-001",
                "incident_id": "inc_01_n_plus_one_query",
                "status": "PROPOSED",
                "human_approval_notice": "AWAITING HUMAN APPROVAL — this fix has not been applied.",
                "summary": "Eagerly fetch shipping addresses in batch before the serialization loop.",
                "rationale": "Eliminates N+1 queries by loading all addresses in a single query.",
                "changes": [
                    {
                        "file": "service/app.py",
                        "start_line": 40,
                        "end_line": 42,
                        "description": "Replace per-item query with pre-fetched dictionary lookup.",
                        "before": "address = db.query(item.id)",
                        "after": "address = address_map.get(item.id)",
                    }
                ],
                "patch": "--- a/service/app.py\n+++ b/service/app.py\n@@ -40,1 +40,1 @@",
                "expected_effect": "Database query count per bulk order request drops from 1+N to 2.",
                "risks": ["Slight memory increase if batch size is large."],
                "validation_plan": ["Run test_batch_serialization against mock DB."],
                "rollback_plan": "Revert commit to restore sequential loading.",
                "evidence_ids": ["EV-LOG-001", "EV-CODE-001"],
            }
        ],
    }
    model = FixProposalBundle.from_dict(raw)
    assert model.incident_id == "inc_01_n_plus_one_query"
    assert len(model.proposals) == 1
    assert model.proposals[0].proposal_id == "FIX-001"
    assert model.proposals[0].changes[0].file == "service/app.py"

    dumped = model.to_dict(exclude_none=True)
    assert dumped == raw


# ============================================================================
# Approval Models Tests
# ============================================================================

def test_approval_record_round_trip() -> None:
    raw = {
        "proposal_id": "FIX-001",
        "status": "APPROVED",
        "decision": "approved",
        "approved_by": "oncall-engineer@company.com",
        "timestamp": "2026-08-28T15:00:00Z",
        "notes": "Verified offline unit tests passed cleanly.",
    }
    model = ApprovalRecord.from_dict(raw)
    assert model.proposal_id == "FIX-001"
    assert model.status == "APPROVED"
    assert model.decision == "approved"

    dumped = model.to_dict(exclude_none=True)
    assert dumped == raw


def test_approval_bundle_round_trip() -> None:
    raw = {
        "incident_id": "inc_01",
        "approvals": [
            {
                "proposal_id": "FIX-001",
                "status": "APPROVED",
                "decision": "approved",
                "approved_by": "human",
                "timestamp": "2026-08-28T15:00:00Z",
                "notes": None,
            }
        ]
    }
    model = ApprovalBundle.from_dict(raw)
    assert len(model.approvals) == 1
    dumped = model.to_dict(exclude_none=False)
    assert dumped == raw


# ============================================================================
# Validation & Error Rejection Tests
# ============================================================================

def test_invalid_evidence_id_pattern_rejected() -> None:
    with pytest.raises(ValidationError):
        EvidenceItem.from_dict({
            "evidence_id": "invalid_id",  # Needs EV-...
            "source_type": "logs",
            "description": "bad id",
            "raw_snippet": "error",
        })


def test_invalid_hypothesis_id_pattern_rejected() -> None:
    with pytest.raises(ValidationError):
        Hypothesis.from_dict({
            "hypothesis_id": "HYP-1",  # Needs HYP-001 (3 digits)
            "claim": "claim",
            "evidence_ids": ["EV-01"],
            "supporting_reasoning": "reason",
            "falsification_criteria": ["crit"],
            "verification_plan": ["plan"],
        })


def test_missing_required_fields_rejected() -> None:
    with pytest.raises(ValidationError):
        # Missing 'claim' and 'verification_plan'
        Hypothesis.from_dict({
            "hypothesis_id": "HYP-001",
            "evidence_ids": ["EV-LOG-001"],
            "supporting_reasoning": "reasoning",
            "falsification_criteria": ["falsify"],
        })


def test_invalid_log_source_rejected() -> None:
    with pytest.raises(ValidationError):
        LogEvidenceItem.from_dict({
            "evidence_id": "EV-LOG-001",
            "source": "metrics",  # Invalid for LogEvidenceItem
            "reference": "logs/app.log:1",
            "type": "error",
            "excerpt": "log line",
        })


def test_invalid_metric_source_rejected() -> None:
    with pytest.raises(ValidationError):
        MetricEvidenceItem.from_dict({
            "evidence_id": "EV-MET-001",
            "source": "logs",  # Invalid for MetricEvidenceItem
            "reference": "metrics/metrics.csv:1",
            "metric": "cpu",
            "value": 99.0,
            "type": "spike",
        })


# ============================================================================
# Investigation Result & Orchestrator Models Tests
# ============================================================================

def test_stage_result_and_investigation_result_round_trip() -> None:
    raw = {
        "incident_id": "inc_01_n_plus_one_query",
        "pipeline_status": "COMPLETED",
        "human_approval_notice": "AWAITING HUMAN APPROVAL — this fix has not been applied.",
        "llm_call_count": 4,
        "prompt_tokens": 1200,
        "completion_tokens": 400,
        "total_tokens": 1600,
        "stages": {
            "logs": {
                "status": "SUCCEEDED",
                "output": {"summary": "logs ok"},
                "error": None,
                "llm_calls": 1,
                "cache_hit": False,
            },
            "metrics": {
                "status": "SUCCEEDED",
                "output": {"summary": "metrics ok"},
                "error": None,
                "llm_calls": 1,
                "cache_hit": False,
            },
        },
        "summary": {
            "confirmed_hypotheses": 1,
            "rejected_hypotheses": 0,
            "inconclusive_hypotheses": 0,
            "proposals_generated": 1,
            "proposals_approved": 1,
            "proposals_rejected": 0,
        },
        "error": None,
    }
    model = InvestigationResult.from_dict(raw)
    assert model.incident_id == "inc_01_n_plus_one_query"
    assert model.pipeline_status == "COMPLETED"
    assert model.stages["logs"].status == "SUCCEEDED"
    assert model.summary.confirmed_hypotheses == 1

    dumped = model.to_dict(exclude_none=False)
    assert dumped["incident_id"] == raw["incident_id"]
    assert dumped["pipeline_status"] == "COMPLETED"
    assert dumped["stages"]["logs"]["status"] == "SUCCEEDED"
