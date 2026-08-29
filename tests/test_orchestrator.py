"""Unit tests for agents/orchestrator.py.

All LLM calls are mocked — no real Groq API calls.
Source files are verified unchanged after every run.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path
from typing import Any, Dict, List
from unittest.mock import MagicMock, patch

import pytest

from agents.orchestrator import (
    PIPELINE_COMPLETED,
    PIPELINE_FAILED,
    PIPELINE_PARTIAL,
    STATUS_FAILED,
    STATUS_REUSED,
    STATUS_SKIPPED,
    STATUS_SUCCEEDED,
    IncidentOrchestrator,
    _fuse_evidence,
    _validate_evidence_fusion,
)
from agents.fix_tools import HUMAN_APPROVAL_NOTICE
from core.llm import GroqLLMClient, LLMAPIError, LLMResponse

REPO = Path(__file__).parent.parent
INC_01 = REPO / "incidents" / "inc_01_n_plus_one_query"
INC_04 = REPO / "incidents" / "inc_04_memory_leak"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _mk_logs(incident_id: str = "inc_01_n_plus_one_query", n: int = 3) -> Dict[str, Any]:
    return {
        "incident_id": incident_id,
        "agent": "logs_agent",
        "summary": "test logs",
        "evidence": [
            {"evidence_id": f"EV-LOG-{i:03d}", "source": "logs",
             "reference": f"logs/application.log:{i}", "timestamp": "",
             "type": "error", "excerpt": f"log excerpt {i}", "interpretation": f"interp {i}"}
            for i in range(1, n + 1)
        ],
    }


def _mk_metrics(incident_id: str = "inc_01_n_plus_one_query", n: int = 2) -> Dict[str, Any]:
    return {
        "incident_id": incident_id,
        "agent": "metrics_agent",
        "summary": "test metrics",
        "evidence": [
            {"evidence_id": f"EV-MET-{i:03d}", "source": "metrics",
             "reference": f"metrics/metrics.csv:{i}", "timestamp": "",
             "type": "spike", "excerpt": f"metric excerpt {i}", "interpretation": f"interp {i}"}
            for i in range(1, n + 1)
        ],
    }


def _mk_code(incident_id: str = "inc_01_n_plus_one_query", n: int = 2) -> Dict[str, Any]:
    return {
        "incident_id": incident_id,
        "agent": "code_agent",
        "summary": "test code",
        "evidence": [
            {"evidence_id": f"EV-CODE-{i:03d}", "source": "code",
             "reference": f"service/app.py:{i}", "timestamp": "",
             "type": "suspicious_pattern", "excerpt": f"code excerpt {i}", "interpretation": f"interp {i}"}
            for i in range(1, n + 1)
        ],
    }


def _mk_hypotheses(incident_id: str = "inc_01_n_plus_one_query") -> Dict[str, Any]:
    return {
        "incident_id": incident_id,
        "hypotheses": [
            {
                "hypothesis_id": "HYP-001",
                "claim": "N+1 query pattern.",
                "evidence_ids": ["EV-LOG-001", "EV-CODE-001"],
                "supporting_reasoning": "250 queries per request.",
                "falsification_criteria": ["constant query count"],
                "verification_plan": ["check query count per request"],
            }
        ],
    }


def _mk_verification(verdict: str = "CONFIRMED") -> Dict[str, Any]:
    return {
        "incident_id": "inc_01_n_plus_one_query",
        "results": [
            {
                "hypothesis_id": "HYP-001",
                "verdict": verdict,
                "checks": [{"check_id": "CHK-001", "description": "check", "result": "PASS",
                             "evidence": ["EV-LOG-001"], "reference": "service/app.py:22"}],
                "reasoning": "All checks passed.",
                "confidence": 0.9,
            }
        ],
    }


def _mk_fix_proposals(n: int = 1) -> Dict[str, Any]:
    proposals = []
    for i in range(1, n + 1):
        proposals.append({
            "proposal_id": f"FIX-{i:03d}",
            "hypothesis_id": "HYP-001",
            "incident_id": "inc_01_n_plus_one_query",
            "status": "PROPOSED",
            "human_approval_notice": HUMAN_APPROVAL_NOTICE,
            "summary": f"Fix {i}",
            "rationale": "rationale",
            "changes": [{"file": "service/app.py", "start_line": None, "end_line": None,
                         "description": "desc", "before": "old", "after": "new"}],
            "patch": "--- a/service/app.py\n+++ b/service/app.py\n@@ -1 +1 @@\n-old\n+new",
            "expected_effect": "better",
            "risks": [],
            "validation_plan": ["run tests"],
            "rollback_plan": "revert",
            "evidence_ids": ["EV-LOG-001"],
        })
    return {
        "incident_id": "inc_01_n_plus_one_query",
        "proposals": proposals,
        "validation_errors": {},
        "skipped_hypotheses": [],
    }


def _mk_llm_response(data: Dict[str, Any]) -> LLMResponse:
    resp = MagicMock(spec=LLMResponse)
    resp.content = json.dumps(data)
    resp.parsed_json = data
    resp.get_structured.return_value = data
    return resp


def _make_orchestrator(
    logs_result=None,
    metrics_result=None,
    code_result=None,
    hyp_result=None,
    ver_result=None,
    fix_result=None,
):
    """Return an IncidentOrchestrator with all agents patched."""
    orch = IncidentOrchestrator(non_interactive=True)

    with patch.object(orch, "_get_llm_client", return_value=MagicMock(spec=GroqLLMClient)):
        pass

    # We'll patch per-test using patch decorators below
    return orch


# ---------------------------------------------------------------------------
# Test 1: Correct stage ordering
# ---------------------------------------------------------------------------

def test_stage_order_recorded_in_result():
    """All 8 stages appear in result.stages in the correct keys."""
    orch = IncidentOrchestrator(non_interactive=True)
    with (
        patch("agents.orchestrator.LogsAgent") as MockLogs,
        patch("agents.orchestrator.MetricsAgent") as MockMetrics,
        patch("agents.orchestrator.CodeAgent") as MockCode,
        patch("agents.orchestrator.HypothesisEngine") as MockHyp,
        patch("agents.orchestrator.VerificationAgent") as MockVer,
        patch("agents.orchestrator.FixProposalAgent") as MockFix,
        patch("agents.orchestrator.ApprovalGate") as MockGate,
    ):
        MockLogs.return_value.extract_evidence.return_value = _mk_logs()
        MockMetrics.return_value.extract_evidence.return_value = _mk_metrics()
        MockCode.return_value.extract_evidence.return_value = _mk_code()
        MockHyp.return_value.generate_hypotheses.return_value = _mk_hypotheses()
        MockVer.return_value.verify.return_value = _mk_verification()
        MockFix.return_value.propose_fix.return_value = _mk_fix_proposals(0)
        MockGate.return_value.review_all.return_value = {
            "incident_id": "inc_01_n_plus_one_query",
            "approval_records": [],
            "summary": {"total": 0, "approved": 0, "rejected": 0},
        }
        result = orch.investigate(INC_01)

    stages = result["stages"]
    for key in ("logs", "metrics", "code", "evidence_fusion", "hypotheses", "verification", "fix_proposals", "approvals"):
        assert key in stages, f"Missing stage: {key}"


# ---------------------------------------------------------------------------
# Tests 2-3: Evidence agent calls
# ---------------------------------------------------------------------------

def test_logs_metrics_code_all_called():
    """All three evidence agents are called during a run."""
    orch = IncidentOrchestrator(non_interactive=True)
    call_log = []
    with (
        patch("agents.orchestrator.LogsAgent") as MockLogs,
        patch("agents.orchestrator.MetricsAgent") as MockMetrics,
        patch("agents.orchestrator.CodeAgent") as MockCode,
        patch("agents.orchestrator.HypothesisEngine") as MockHyp,
        patch("agents.orchestrator.VerificationAgent") as MockVer,
        patch("agents.orchestrator.FixProposalAgent") as MockFix,
        patch("agents.orchestrator.ApprovalGate") as MockGate,
    ):
        def log_call(name, result):
            def fn(*a, **kw):
                call_log.append(name)
                return result
            return fn

        MockLogs.return_value.extract_evidence.side_effect = log_call("logs", _mk_logs())
        MockMetrics.return_value.extract_evidence.side_effect = log_call("metrics", _mk_metrics())
        MockCode.return_value.extract_evidence.side_effect = log_call("code", _mk_code())
        MockHyp.return_value.generate_hypotheses.return_value = _mk_hypotheses()
        MockVer.return_value.verify.return_value = _mk_verification()
        MockFix.return_value.propose_fix.return_value = _mk_fix_proposals(0)
        MockGate.return_value.review_all.return_value = {
            "incident_id": "inc_01_n_plus_one_query",
            "approval_records": [],
            "summary": {"total": 0, "approved": 0, "rejected": 0},
        }
        orch.investigate(INC_01)

    assert "logs" in call_log
    assert "metrics" in call_log
    assert "code" in call_log
    # Order: logs first, then metrics, then code
    assert call_log.index("logs") < call_log.index("metrics") < call_log.index("code")


# ---------------------------------------------------------------------------
# Test 4: Evidence fusion
# ---------------------------------------------------------------------------

def test_evidence_fusion_combines_all_sources():
    fused = _fuse_evidence("inc_x", _mk_logs(n=2), _mk_metrics(n=2), _mk_code(n=2))
    ids = {ev["evidence_id"] for ev in fused["evidence"]}
    assert "EV-LOG-001" in ids
    assert "EV-MET-001" in ids
    assert "EV-CODE-001" in ids
    assert len(ids) == 6


def test_evidence_fusion_deduplicates():
    logs = _mk_logs(n=2)
    logs2 = _mk_logs(n=2)  # same IDs
    fused = _fuse_evidence("inc_x", logs, logs2, None)
    # Should deduplicate — only 2 unique IDs
    assert len(fused["evidence"]) == 2


def test_evidence_fusion_validation_passes_clean():
    fused = _fuse_evidence("inc_x", _mk_logs(n=1), _mk_metrics(n=1), _mk_code(n=1))
    errors = _validate_evidence_fusion(fused)
    assert errors == []


# ---------------------------------------------------------------------------
# Test 5: Hypothesis receives all three evidence sources
# ---------------------------------------------------------------------------

def test_hypothesis_receives_all_evidence_sources():
    orch = IncidentOrchestrator(non_interactive=True)
    captured = {}
    with (
        patch("agents.orchestrator.LogsAgent") as MockLogs,
        patch("agents.orchestrator.MetricsAgent") as MockMetrics,
        patch("agents.orchestrator.CodeAgent") as MockCode,
        patch("agents.orchestrator.HypothesisEngine") as MockHyp,
        patch("agents.orchestrator.VerificationAgent") as MockVer,
        patch("agents.orchestrator.FixProposalAgent") as MockFix,
        patch("agents.orchestrator.ApprovalGate") as MockGate,
    ):
        MockLogs.return_value.extract_evidence.return_value = _mk_logs()
        MockMetrics.return_value.extract_evidence.return_value = _mk_metrics()
        MockCode.return_value.extract_evidence.return_value = _mk_code()

        def capture_hyp_call(*a, **kw):
            captured.update(kw)
            return _mk_hypotheses()

        MockHyp.return_value.generate_hypotheses.side_effect = capture_hyp_call
        MockVer.return_value.verify.return_value = _mk_verification()
        MockFix.return_value.propose_fix.return_value = _mk_fix_proposals(0)
        MockGate.return_value.review_all.return_value = {
            "incident_id": "inc_01_n_plus_one_query",
            "approval_records": [],
            "summary": {"total": 0, "approved": 0, "rejected": 0},
        }
        orch.investigate(INC_01)

    assert "logs_evidence" in captured
    assert "metrics_evidence" in captured
    assert "code_evidence" in captured


# ---------------------------------------------------------------------------
# Test 6: Verification receives correct inputs
# ---------------------------------------------------------------------------

def test_verification_receives_hypotheses_and_evidence():
    orch = IncidentOrchestrator(non_interactive=True)
    captured = {}
    with (
        patch("agents.orchestrator.LogsAgent") as MockLogs,
        patch("agents.orchestrator.MetricsAgent") as MockMetrics,
        patch("agents.orchestrator.CodeAgent") as MockCode,
        patch("agents.orchestrator.HypothesisEngine") as MockHyp,
        patch("agents.orchestrator.VerificationAgent") as MockVer,
        patch("agents.orchestrator.FixProposalAgent") as MockFix,
        patch("agents.orchestrator.ApprovalGate") as MockGate,
    ):
        MockLogs.return_value.extract_evidence.return_value = _mk_logs()
        MockMetrics.return_value.extract_evidence.return_value = _mk_metrics()
        MockCode.return_value.extract_evidence.return_value = _mk_code()
        MockHyp.return_value.generate_hypotheses.return_value = _mk_hypotheses()

        def capture_ver(*a, **kw):
            captured.update(kw)
            return _mk_verification()

        MockVer.return_value.verify.side_effect = capture_ver
        MockFix.return_value.propose_fix.return_value = _mk_fix_proposals(0)
        MockGate.return_value.review_all.return_value = {
            "incident_id": "inc_01_n_plus_one_query",
            "approval_records": [],
            "summary": {"total": 0, "approved": 0, "rejected": 0},
        }
        orch.investigate(INC_01)

    assert "hypotheses" in captured
    assert "logs_evidence" in captured


# ---------------------------------------------------------------------------
# Test 7-8: Fix Proposal and Approval receive correct inputs
# ---------------------------------------------------------------------------

def test_fix_proposal_receives_verification():
    orch = IncidentOrchestrator(non_interactive=True)
    captured = {}
    with (
        patch("agents.orchestrator.LogsAgent") as MockLogs,
        patch("agents.orchestrator.MetricsAgent") as MockMetrics,
        patch("agents.orchestrator.CodeAgent") as MockCode,
        patch("agents.orchestrator.HypothesisEngine") as MockHyp,
        patch("agents.orchestrator.VerificationAgent") as MockVer,
        patch("agents.orchestrator.FixProposalAgent") as MockFix,
        patch("agents.orchestrator.ApprovalGate") as MockGate,
    ):
        MockLogs.return_value.extract_evidence.return_value = _mk_logs()
        MockMetrics.return_value.extract_evidence.return_value = _mk_metrics()
        MockCode.return_value.extract_evidence.return_value = _mk_code()
        MockHyp.return_value.generate_hypotheses.return_value = _mk_hypotheses()
        MockVer.return_value.verify.return_value = _mk_verification()

        def capture_fix(*a, **kw):
            captured.update(kw)
            return _mk_fix_proposals(0)

        MockFix.return_value.propose_fix.side_effect = capture_fix
        MockGate.return_value.review_all.return_value = {
            "incident_id": "inc_01_n_plus_one_query",
            "approval_records": [],
            "summary": {"total": 0, "approved": 0, "rejected": 0},
        }
        orch.investigate(INC_01)

    assert "verification_results" in captured
    assert "hypotheses" in captured


def test_approval_receives_proposals():
    orch = IncidentOrchestrator(non_interactive=True)
    approval_arg = {}
    with (
        patch("agents.orchestrator.LogsAgent") as MockLogs,
        patch("agents.orchestrator.MetricsAgent") as MockMetrics,
        patch("agents.orchestrator.CodeAgent") as MockCode,
        patch("agents.orchestrator.HypothesisEngine") as MockHyp,
        patch("agents.orchestrator.VerificationAgent") as MockVer,
        patch("agents.orchestrator.FixProposalAgent") as MockFix,
        patch("agents.orchestrator.ApprovalGate") as MockGate,
    ):
        MockLogs.return_value.extract_evidence.return_value = _mk_logs()
        MockMetrics.return_value.extract_evidence.return_value = _mk_metrics()
        MockCode.return_value.extract_evidence.return_value = _mk_code()
        MockHyp.return_value.generate_hypotheses.return_value = _mk_hypotheses()
        MockVer.return_value.verify.return_value = _mk_verification()
        MockFix.return_value.propose_fix.return_value = _mk_fix_proposals(1)

        def capture_gate(bundle, *a, **kw):
            approval_arg["bundle"] = bundle
            return {
                "incident_id": "inc_01_n_plus_one_query",
                "approval_records": [],
                "summary": {"total": 1, "approved": 0, "rejected": 1},
            }

        MockGate.return_value.review_all.side_effect = capture_gate
        orch.investigate(INC_01)

    assert "proposals" in approval_arg.get("bundle", {})


# ---------------------------------------------------------------------------
# Test 9: Final result contains all stages
# ---------------------------------------------------------------------------

def test_final_result_contains_all_stages():
    orch = IncidentOrchestrator(non_interactive=True)
    with (
        patch("agents.orchestrator.LogsAgent") as MockLogs,
        patch("agents.orchestrator.MetricsAgent") as MockMetrics,
        patch("agents.orchestrator.CodeAgent") as MockCode,
        patch("agents.orchestrator.HypothesisEngine") as MockHyp,
        patch("agents.orchestrator.VerificationAgent") as MockVer,
        patch("agents.orchestrator.FixProposalAgent") as MockFix,
        patch("agents.orchestrator.ApprovalGate") as MockGate,
    ):
        MockLogs.return_value.extract_evidence.return_value = _mk_logs()
        MockMetrics.return_value.extract_evidence.return_value = _mk_metrics()
        MockCode.return_value.extract_evidence.return_value = _mk_code()
        MockHyp.return_value.generate_hypotheses.return_value = _mk_hypotheses()
        MockVer.return_value.verify.return_value = _mk_verification()
        MockFix.return_value.propose_fix.return_value = _mk_fix_proposals(0)
        MockGate.return_value.review_all.return_value = {
            "incident_id": "inc_01_n_plus_one_query",
            "approval_records": [],
            "summary": {"total": 0, "approved": 0, "rejected": 0},
        }
        result = orch.investigate(INC_01)

    assert result["incident_id"] == "inc_01_n_plus_one_query"
    assert "pipeline_status" in result
    assert "llm_call_count" in result
    assert "human_approval_notice" in result
    assert result["human_approval_notice"] == HUMAN_APPROVAL_NOTICE


# ---------------------------------------------------------------------------
# Test 10: Stage failure is recorded
# ---------------------------------------------------------------------------

def test_logs_stage_failure_is_recorded():
    orch = IncidentOrchestrator(non_interactive=True)
    with (
        patch("agents.orchestrator.LogsAgent") as MockLogs,
        patch("agents.orchestrator.MetricsAgent") as MockMetrics,
        patch("agents.orchestrator.CodeAgent") as MockCode,
        patch("agents.orchestrator.HypothesisEngine") as MockHyp,
        patch("agents.orchestrator.VerificationAgent") as MockVer,
        patch("agents.orchestrator.FixProposalAgent") as MockFix,
        patch("agents.orchestrator.ApprovalGate") as MockGate,
    ):
        MockLogs.return_value.extract_evidence.side_effect = RuntimeError("log agent exploded")
        MockMetrics.return_value.extract_evidence.return_value = _mk_metrics()
        MockCode.return_value.extract_evidence.return_value = _mk_code()
        MockHyp.return_value.generate_hypotheses.return_value = _mk_hypotheses()
        MockVer.return_value.verify.return_value = _mk_verification()
        MockFix.return_value.propose_fix.return_value = _mk_fix_proposals(0)
        MockGate.return_value.review_all.return_value = {
            "incident_id": "inc_01_n_plus_one_query",
            "approval_records": [],
            "summary": {"total": 0, "approved": 0, "rejected": 0},
        }
        result = orch.investigate(INC_01)

    assert result["stages"]["logs"]["status"] == STATUS_FAILED
    assert "exploded" in result["stages"]["logs"]["error"]


# ---------------------------------------------------------------------------
# Test 11: Missing evidence does not silently succeed
# ---------------------------------------------------------------------------

def test_all_evidence_fail_stops_pipeline():
    orch = IncidentOrchestrator(non_interactive=True)
    with (
        patch("agents.orchestrator.LogsAgent") as MockLogs,
        patch("agents.orchestrator.MetricsAgent") as MockMetrics,
        patch("agents.orchestrator.CodeAgent") as MockCode,
        patch("agents.orchestrator.HypothesisEngine") as MockHyp,
    ):
        MockLogs.return_value.extract_evidence.side_effect = RuntimeError("fail")
        MockMetrics.return_value.extract_evidence.side_effect = RuntimeError("fail")
        MockCode.return_value.extract_evidence.side_effect = RuntimeError("fail")
        # Should stop before hypothesis since no evidence
        MockHyp.return_value.generate_hypotheses.return_value = _mk_hypotheses()
        result = orch.investigate(INC_01)

    # All evidence stages failed → no evidence → hypotheses skipped
    assert result["stages"]["logs"]["status"] == STATUS_FAILED
    assert result["stages"]["hypotheses"]["status"] in (STATUS_SKIPPED, STATUS_FAILED)
    MockHyp.return_value.generate_hypotheses.assert_not_called()


# ---------------------------------------------------------------------------
# Tests 12-13: Ground truth and baseline isolation
# ---------------------------------------------------------------------------

def test_orchestrator_never_reads_ground_truth():
    module_path = REPO / "agents" / "orchestrator.py"
    source = module_path.read_text(encoding="utf-8")
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            assert "ground_truth.md" not in node.value, \
                "orchestrator.py must not reference ground_truth.md path"


def test_orchestrator_never_reads_baseline():
    """orchestrator.py must not import from baseline module or open baseline result files."""
    module_path = REPO / "agents" / "orchestrator.py"
    source = module_path.read_text(encoding="utf-8")
    tree = ast.parse(source)
    # No imports from baseline module
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            assert node.module != "baseline" and not (node.module or "").startswith("baseline."), \
                "orchestrator.py must not import from baseline module"
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert not alias.name.startswith("baseline"), \
                    "orchestrator.py must not import baseline module"
    # No open/read calls referencing baseline result files by path
    # (The _RB/_BS variables exist as guards — we check they are never passed to open())
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name) and func.id == "open":
                for arg in node.args:
                    if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                        assert "results_baseline" not in arg.value.lower()
                        assert "baseline_summary" not in arg.value.lower()


# ---------------------------------------------------------------------------
# Test 14: No direct Groq import
# ---------------------------------------------------------------------------

def test_no_direct_groq_import():
    module_path = REPO / "agents" / "orchestrator.py"
    source = module_path.read_text(encoding="utf-8")
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert not alias.name.startswith("groq"), \
                    "orchestrator.py must not import 'groq' directly"
        if isinstance(node, ast.ImportFrom):
            assert node.module != "groq" and not (node.module or "").startswith("groq."), \
                "orchestrator.py must not import from 'groq' directly"


# ---------------------------------------------------------------------------
# Test 15: Orchestrator makes zero direct LLM calls
# ---------------------------------------------------------------------------

def test_orchestrator_makes_zero_direct_llm_calls():
    """IncidentOrchestrator.investigate() must never call generate_structured directly."""
    orch = IncidentOrchestrator(non_interactive=True)
    mock_client = MagicMock(spec=GroqLLMClient)
    orch._llm_client = mock_client
    with (
        patch("agents.orchestrator.LogsAgent") as MockLogs,
        patch("agents.orchestrator.MetricsAgent") as MockMetrics,
        patch("agents.orchestrator.CodeAgent") as MockCode,
        patch("agents.orchestrator.HypothesisEngine") as MockHyp,
        patch("agents.orchestrator.VerificationAgent") as MockVer,
        patch("agents.orchestrator.FixProposalAgent") as MockFix,
        patch("agents.orchestrator.ApprovalGate") as MockGate,
    ):
        MockLogs.return_value.extract_evidence.return_value = _mk_logs()
        MockMetrics.return_value.extract_evidence.return_value = _mk_metrics()
        MockCode.return_value.extract_evidence.return_value = _mk_code()
        MockHyp.return_value.generate_hypotheses.return_value = _mk_hypotheses()
        MockVer.return_value.verify.return_value = _mk_verification()
        MockFix.return_value.propose_fix.return_value = _mk_fix_proposals(0)
        MockGate.return_value.review_all.return_value = {
            "incident_id": "inc_01_n_plus_one_query",
            "approval_records": [],
            "summary": {"total": 0, "approved": 0, "rejected": 0},
        }
        orch.investigate(INC_01)

    mock_client.generate_structured.assert_not_called()
    mock_client.generate.assert_not_called()


# ---------------------------------------------------------------------------
# Test 17: Evidence IDs remain intact through the pipeline
# ---------------------------------------------------------------------------

def test_evidence_ids_preserved_through_fusion():
    logs = _mk_logs(n=2)
    metrics = _mk_metrics(n=2)
    code = _mk_code(n=2)
    fused = _fuse_evidence("test_inc", logs, metrics, code)
    all_ids = {ev["evidence_id"] for ev in fused["evidence"]}
    for i in range(1, 3):
        assert f"EV-LOG-{i:03d}" in all_ids
        assert f"EV-MET-{i:03d}" in all_ids
        assert f"EV-CODE-{i:03d}" in all_ids


# ---------------------------------------------------------------------------
# Test 19: Rejected hypotheses do not produce confirmed fixes
# ---------------------------------------------------------------------------

def test_rejected_hypothesis_fix_proposal_not_generated():
    orch = IncidentOrchestrator(non_interactive=True)
    with (
        patch("agents.orchestrator.LogsAgent") as MockLogs,
        patch("agents.orchestrator.MetricsAgent") as MockMetrics,
        patch("agents.orchestrator.CodeAgent") as MockCode,
        patch("agents.orchestrator.HypothesisEngine") as MockHyp,
        patch("agents.orchestrator.VerificationAgent") as MockVer,
        patch("agents.orchestrator.FixProposalAgent") as MockFix,
        patch("agents.orchestrator.ApprovalGate") as MockGate,
    ):
        MockLogs.return_value.extract_evidence.return_value = _mk_logs()
        MockMetrics.return_value.extract_evidence.return_value = _mk_metrics()
        MockCode.return_value.extract_evidence.return_value = _mk_code()
        MockHyp.return_value.generate_hypotheses.return_value = _mk_hypotheses()
        # All hypotheses REJECTED
        MockVer.return_value.verify.return_value = _mk_verification(verdict="REJECTED")
        # FixProposalAgent should return 0 proposals for REJECTED hyps
        MockFix.return_value.propose_fix.return_value = {
            "incident_id": "inc_01_n_plus_one_query",
            "proposals": [],
            "validation_errors": {},
            "skipped_hypotheses": [{"hypothesis_id": "HYP-001", "verdict": "REJECTED"}],
        }
        MockGate.return_value.review_all.return_value = {
            "incident_id": "inc_01_n_plus_one_query",
            "approval_records": [],
            "summary": {"total": 0, "approved": 0, "rejected": 0},
        }
        result = orch.investigate(INC_01)

    fix_output = result["stages"]["fix_proposals"]["output"] or {}
    assert len(fix_output.get("proposals") or []) == 0


# ---------------------------------------------------------------------------
# Test 21: Approval defaults to rejection
# ---------------------------------------------------------------------------

def test_approval_defaults_to_rejected_in_non_interactive():
    orch = IncidentOrchestrator(non_interactive=True)
    with (
        patch("agents.orchestrator.LogsAgent") as MockLogs,
        patch("agents.orchestrator.MetricsAgent") as MockMetrics,
        patch("agents.orchestrator.CodeAgent") as MockCode,
        patch("agents.orchestrator.HypothesisEngine") as MockHyp,
        patch("agents.orchestrator.VerificationAgent") as MockVer,
        patch("agents.orchestrator.FixProposalAgent") as MockFix,
        patch("agents.orchestrator.ApprovalGate") as MockGate,
    ):
        MockLogs.return_value.extract_evidence.return_value = _mk_logs()
        MockMetrics.return_value.extract_evidence.return_value = _mk_metrics()
        MockCode.return_value.extract_evidence.return_value = _mk_code()
        MockHyp.return_value.generate_hypotheses.return_value = _mk_hypotheses()
        MockVer.return_value.verify.return_value = _mk_verification()
        MockFix.return_value.propose_fix.return_value = _mk_fix_proposals(1)
        # Gate constructed with interactive=False
        MockGate.return_value.review_all.return_value = {
            "incident_id": "inc_01_n_plus_one_query",
            "approval_records": [{"proposal_id": "FIX-001", "status": "REJECTED",
                                   "decision": "rejected", "approved_by": "human",
                                   "timestamp": "2026-01-01T00:00:00+00:00"}],
            "summary": {"total": 1, "approved": 0, "rejected": 1},
        }
        result = orch.investigate(INC_01)
        # Verify gate was constructed with interactive=False (non_interactive=True → interactive=False)
        MockGate.assert_called_with(interactive=False)

    assert result["summary"]["proposals_approved"] == 0


# ---------------------------------------------------------------------------
# Test 22: Approval does not modify source files
# ---------------------------------------------------------------------------

def test_approval_does_not_modify_source_files():
    service_app = INC_01 / "service" / "app.py"
    before = service_app.read_text(encoding="utf-8")
    orch = IncidentOrchestrator(non_interactive=True)
    with (
        patch("agents.orchestrator.LogsAgent") as MockLogs,
        patch("agents.orchestrator.MetricsAgent") as MockMetrics,
        patch("agents.orchestrator.CodeAgent") as MockCode,
        patch("agents.orchestrator.HypothesisEngine") as MockHyp,
        patch("agents.orchestrator.VerificationAgent") as MockVer,
        patch("agents.orchestrator.FixProposalAgent") as MockFix,
        patch("agents.orchestrator.ApprovalGate") as MockGate,
    ):
        MockLogs.return_value.extract_evidence.return_value = _mk_logs()
        MockMetrics.return_value.extract_evidence.return_value = _mk_metrics()
        MockCode.return_value.extract_evidence.return_value = _mk_code()
        MockHyp.return_value.generate_hypotheses.return_value = _mk_hypotheses()
        MockVer.return_value.verify.return_value = _mk_verification()
        MockFix.return_value.propose_fix.return_value = _mk_fix_proposals(1)
        MockGate.return_value.review_all.return_value = {
            "incident_id": "inc_01_n_plus_one_query",
            "approval_records": [{"proposal_id": "FIX-001", "status": "APPROVED",
                                   "decision": "approved", "approved_by": "human",
                                   "timestamp": "2026-01-01T00:00:00+00:00"}],
            "summary": {"total": 1, "approved": 1, "rejected": 0},
        }
        orch.investigate(INC_01)

    after = service_app.read_text(encoding="utf-8")
    assert before == after, "Source file was modified by orchestrator!"


# ---------------------------------------------------------------------------
# Test 24: No shell execution in orchestrator
# ---------------------------------------------------------------------------

def test_no_shell_execution_in_orchestrator():
    module_path = REPO / "agents" / "orchestrator.py"
    source = module_path.read_text(encoding="utf-8")
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert alias.name not in ("subprocess", "os"), \
                    f"orchestrator.py imports {alias.name!r} which enables shell execution"
        if isinstance(node, ast.ImportFrom):
            assert node.module not in ("subprocess",), \
                "orchestrator.py must not import subprocess"


# ---------------------------------------------------------------------------
# Test 25: Cache reuse works
# ---------------------------------------------------------------------------

def test_cache_reuse(tmp_path: Path):
    cache_dir = tmp_path / "cache"
    orch = IncidentOrchestrator(non_interactive=True, output_dir=cache_dir)

    logs_data = _mk_logs()
    (cache_dir / "inc_01_n_plus_one_query").mkdir(parents=True)
    (cache_dir / "inc_01_n_plus_one_query" / "logs.json").write_text(
        json.dumps(logs_data), encoding="utf-8"
    )

    with (
        patch("agents.orchestrator.LogsAgent") as MockLogs,
        patch("agents.orchestrator.MetricsAgent") as MockMetrics,
        patch("agents.orchestrator.CodeAgent") as MockCode,
        patch("agents.orchestrator.HypothesisEngine") as MockHyp,
        patch("agents.orchestrator.VerificationAgent") as MockVer,
        patch("agents.orchestrator.FixProposalAgent") as MockFix,
        patch("agents.orchestrator.ApprovalGate") as MockGate,
    ):
        MockMetrics.return_value.extract_evidence.return_value = _mk_metrics()
        MockCode.return_value.extract_evidence.return_value = _mk_code()
        MockHyp.return_value.generate_hypotheses.return_value = _mk_hypotheses()
        MockVer.return_value.verify.return_value = _mk_verification()
        MockFix.return_value.propose_fix.return_value = _mk_fix_proposals(0)
        MockGate.return_value.review_all.return_value = {
            "incident_id": "inc_01_n_plus_one_query",
            "approval_records": [],
            "summary": {"total": 0, "approved": 0, "rejected": 0},
        }
        result = orch.investigate(INC_01)
        # LogsAgent should NOT have been called (cache hit)
        MockLogs.return_value.extract_evidence.assert_not_called()

    assert result["stages"]["logs"]["status"] == STATUS_REUSED


# ---------------------------------------------------------------------------
# Test 26: Invalid cache rejected
# ---------------------------------------------------------------------------

def test_invalid_cache_rejected(tmp_path: Path):
    """Cache belonging to a different incident is rejected and agent re-runs."""
    cache_dir = tmp_path / "cache"
    orch = IncidentOrchestrator(non_interactive=True, output_dir=cache_dir)

    # Write cache with WRONG incident_id
    wrong_data = _mk_logs(incident_id="inc_99_wrong")
    (cache_dir / "inc_01_n_plus_one_query").mkdir(parents=True)
    (cache_dir / "inc_01_n_plus_one_query" / "logs.json").write_text(
        json.dumps(wrong_data), encoding="utf-8"
    )

    with (
        patch("agents.orchestrator.LogsAgent") as MockLogs,
        patch("agents.orchestrator.MetricsAgent") as MockMetrics,
        patch("agents.orchestrator.CodeAgent") as MockCode,
        patch("agents.orchestrator.HypothesisEngine") as MockHyp,
        patch("agents.orchestrator.VerificationAgent") as MockVer,
        patch("agents.orchestrator.FixProposalAgent") as MockFix,
        patch("agents.orchestrator.ApprovalGate") as MockGate,
    ):
        MockLogs.return_value.extract_evidence.return_value = _mk_logs()
        MockMetrics.return_value.extract_evidence.return_value = _mk_metrics()
        MockCode.return_value.extract_evidence.return_value = _mk_code()
        MockHyp.return_value.generate_hypotheses.return_value = _mk_hypotheses()
        MockVer.return_value.verify.return_value = _mk_verification()
        MockFix.return_value.propose_fix.return_value = _mk_fix_proposals(0)
        MockGate.return_value.review_all.return_value = {
            "incident_id": "inc_01_n_plus_one_query",
            "approval_records": [],
            "summary": {"total": 0, "approved": 0, "rejected": 0},
        }
        orch.investigate(INC_01)
        # Agent must have been called since cache was invalid
        MockLogs.return_value.extract_evidence.assert_called_once()


# ---------------------------------------------------------------------------
# Test 27: Missing incident directory
# ---------------------------------------------------------------------------

def test_missing_incident_directory_raises():
    orch = IncidentOrchestrator(non_interactive=True)
    with pytest.raises(FileNotFoundError):
        orch.investigate(Path("/nonexistent/incident_xyz_does_not_exist"))


# ---------------------------------------------------------------------------
# Test 28: Agent exception handled
# ---------------------------------------------------------------------------

def test_hypothesis_exception_recorded():
    orch = IncidentOrchestrator(non_interactive=True)
    with (
        patch("agents.orchestrator.LogsAgent") as MockLogs,
        patch("agents.orchestrator.MetricsAgent") as MockMetrics,
        patch("agents.orchestrator.CodeAgent") as MockCode,
        patch("agents.orchestrator.HypothesisEngine") as MockHyp,
    ):
        MockLogs.return_value.extract_evidence.return_value = _mk_logs()
        MockMetrics.return_value.extract_evidence.return_value = _mk_metrics()
        MockCode.return_value.extract_evidence.return_value = _mk_code()
        MockHyp.return_value.generate_hypotheses.side_effect = LLMAPIError("Groq exploded")
        result = orch.investigate(INC_01)

    assert result["stages"]["hypotheses"]["status"] == STATUS_FAILED
    assert "Groq exploded" in result["stages"]["hypotheses"]["error"]


# ---------------------------------------------------------------------------
# Test 30: Final schema validation
# ---------------------------------------------------------------------------

def test_final_result_validates_against_schema():
    from agents.orchestrator import _load_result_schema
    import jsonschema
    orch = IncidentOrchestrator(non_interactive=True)
    with (
        patch("agents.orchestrator.LogsAgent") as MockLogs,
        patch("agents.orchestrator.MetricsAgent") as MockMetrics,
        patch("agents.orchestrator.CodeAgent") as MockCode,
        patch("agents.orchestrator.HypothesisEngine") as MockHyp,
        patch("agents.orchestrator.VerificationAgent") as MockVer,
        patch("agents.orchestrator.FixProposalAgent") as MockFix,
        patch("agents.orchestrator.ApprovalGate") as MockGate,
    ):
        MockLogs.return_value.extract_evidence.return_value = _mk_logs()
        MockMetrics.return_value.extract_evidence.return_value = _mk_metrics()
        MockCode.return_value.extract_evidence.return_value = _mk_code()
        MockHyp.return_value.generate_hypotheses.return_value = _mk_hypotheses()
        MockVer.return_value.verify.return_value = _mk_verification()
        MockFix.return_value.propose_fix.return_value = _mk_fix_proposals(0)
        MockGate.return_value.review_all.return_value = {
            "incident_id": "inc_01_n_plus_one_query",
            "approval_records": [],
            "summary": {"total": 0, "approved": 0, "rejected": 0},
        }
        result = orch.investigate(INC_01)

    schema = _load_result_schema()
    # Should not raise
    jsonschema.validate(instance=result, schema=schema)


# ---------------------------------------------------------------------------
# Tests 31-37: Existing test suites still pass (meta-tests — just import check)
# ---------------------------------------------------------------------------

def test_existing_modules_importable():
    """Smoke test that all existing modules still import cleanly."""
    import agents.logs_agent
    import agents.metrics_agent
    import agents.code_agent
    import agents.hypothesis_engine
    import agents.verification_agent
    import agents.fix_proposal_agent
    import agents.approval_gate


# ---------------------------------------------------------------------------
# Additional: human_approval_notice always in result
# ---------------------------------------------------------------------------

def test_human_approval_notice_in_result():
    orch = IncidentOrchestrator(non_interactive=True)
    with (
        patch("agents.orchestrator.LogsAgent") as MockLogs,
        patch("agents.orchestrator.MetricsAgent") as MockMetrics,
        patch("agents.orchestrator.CodeAgent") as MockCode,
        patch("agents.orchestrator.HypothesisEngine") as MockHyp,
        patch("agents.orchestrator.VerificationAgent") as MockVer,
        patch("agents.orchestrator.FixProposalAgent") as MockFix,
        patch("agents.orchestrator.ApprovalGate") as MockGate,
    ):
        MockLogs.return_value.extract_evidence.return_value = _mk_logs()
        MockMetrics.return_value.extract_evidence.return_value = _mk_metrics()
        MockCode.return_value.extract_evidence.return_value = _mk_code()
        MockHyp.return_value.generate_hypotheses.return_value = _mk_hypotheses()
        MockVer.return_value.verify.return_value = _mk_verification()
        MockFix.return_value.propose_fix.return_value = _mk_fix_proposals(0)
        MockGate.return_value.review_all.return_value = {
            "incident_id": "inc_01_n_plus_one_query",
            "approval_records": [],
            "summary": {"total": 0, "approved": 0, "rejected": 0},
        }
        result = orch.investigate(INC_01)

    assert result["human_approval_notice"] == HUMAN_APPROVAL_NOTICE
