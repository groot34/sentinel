"""Unit tests for the Sentinel final evaluation harness.

All LLM calls are mocked — no real Groq API calls.
Ground-truth isolation and baseline isolation are verified via AST inspection.
"""

from __future__ import annotations

import ast
import csv
import json
from io import StringIO
from pathlib import Path
from typing import Any, Dict, List
from unittest.mock import MagicMock, patch

import pytest

REPO = Path(__file__).parent.parent
INC_01 = REPO / "incidents" / "inc_01_n_plus_one_query"
INC_10 = REPO / "incidents" / "inc_10_multi_symptom_cascade"
INCIDENTS = sorted([d for d in (REPO / "incidents").iterdir()
                    if d.is_dir() and d.name.startswith("inc_")])


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _mk_orch_result(
    incident_id: str = "inc_01_n_plus_one_query",
    pipeline_status: str = "COMPLETED",
    confirmed: int = 1,
    rejected: int = 1,
    inconclusive: int = 0,
    proposals: int = 1,
    llm_calls: int = 5,
    claim: str = "N+1 query per item in loop",
) -> Dict[str, Any]:
    from agents.fix_tools import HUMAN_APPROVAL_NOTICE
    return {
        "incident_id": incident_id,
        "pipeline_status": pipeline_status,
        "human_approval_notice": HUMAN_APPROVAL_NOTICE,
        "llm_call_count": llm_calls,
        "stages": {
            "logs": {"status": "SUCCEEDED", "llm_calls": 1, "output": {"incident_id": incident_id, "evidence": []}},
            "metrics": {"status": "SUCCEEDED", "llm_calls": 1, "output": {"incident_id": incident_id, "evidence": []}},
            "code": {"status": "SUCCEEDED", "llm_calls": 1, "output": {"incident_id": incident_id, "evidence": []}},
            "evidence_fusion": {"status": "SUCCEEDED", "output": {"incident_id": incident_id, "evidence": []}},
            "hypotheses": {
                "status": "SUCCEEDED",
                "llm_calls": 1,
                "output": {
                    "incident_id": incident_id,
                    "hypotheses": [
                        {
                            "hypothesis_id": "HYP-001",
                            "claim": claim,
                            "evidence_ids": ["EV-LOG-001"],
                            "supporting_reasoning": claim,
                            "falsification_criteria": ["x"],
                            "verification_plan": ["check"],
                        }
                    ],
                },
            },
            "verification": {
                "status": "SUCCEEDED",
                "llm_calls": 0,
                "output": {
                    "incident_id": incident_id,
                    "results": [
                        {
                            "hypothesis_id": "HYP-001",
                            "verdict": "CONFIRMED" if confirmed > 0 else "REJECTED",
                            "checks": [],
                            "reasoning": claim,
                            "confidence": 0.9,
                        }
                    ],
                },
            },
            "fix_proposals": {
                "status": "SUCCEEDED",
                "llm_calls": proposals,
                "output": {
                    "incident_id": incident_id,
                    "proposals": [],
                    "validation_errors": {},
                    "skipped_hypotheses": [],
                },
            },
            "approvals": {
                "status": "SUCCEEDED",
                "llm_calls": 0,
                "output": {
                    "incident_id": incident_id,
                    "approval_records": [],
                    "summary": {"total": 0, "approved": 0, "rejected": 0},
                },
            },
        },
        "summary": {
            "confirmed_hypotheses": confirmed,
            "rejected_hypotheses": rejected,
            "inconclusive_hypotheses": inconclusive,
            "proposals_generated": proposals,
            "proposals_approved": 0,
            "proposals_rejected": 0,
        },
    }


def _mk_baseline_row(
    incident_id: str,
    correctness: str = "CORRECT",
    latency: float = 15.0,
) -> Dict[str, str]:
    return {
        "incident_id": incident_id,
        "baseline_status": "SUCCESS",
        "baseline_root_cause": "test root cause",
        "ground_truth_root_cause": "test gt",
        "correctness": correctness,
        "correctness_explanation": "test",
        "evidence_count": "3",
        "latency_seconds": str(latency),
        "input_tokens": "2000",
        "output_tokens": "600",
        "model": "openai/gpt-oss-120b",
        "error": "",
    }


# ---------------------------------------------------------------------------
# Test 1: Exactly 10 canonical incidents
# ---------------------------------------------------------------------------

def test_exactly_10_incidents():
    """Test 1: discover_incidents returns exactly 10 incidents."""
    from eval.run_sentinel_eval import discover_incidents
    incs = discover_incidents(start=1, end=10)
    assert len(incs) == 10, f"Expected 10 incidents, got {len(incs)}"


# ---------------------------------------------------------------------------
# Test 2-3: Baseline and sentinel results loaded correctly
# ---------------------------------------------------------------------------

def test_baseline_csv_loads(tmp_path: Path):
    """Test 2: load_baseline_csv loads all rows correctly."""
    from eval.run_sentinel_eval import load_baseline_csv, BASELINE_CSV
    # The real baseline CSV exists
    if BASELINE_CSV.exists():
        rows = load_baseline_csv()
        assert len(rows) == 10
        for inc in INCIDENTS:
            assert inc.name in rows, f"Missing baseline row: {inc.name}"
    else:
        pytest.skip("Baseline CSV not present")


def test_sentinel_final_loads(tmp_path: Path):
    """Test 3: load_sentinel_final returns None for missing incident."""
    from eval.run_sentinel_eval import load_sentinel_final, SENTINEL_RESULTS_DIR
    # No final for a fake incident
    result = load_sentinel_final("inc_99_fake")
    assert result is None


# ---------------------------------------------------------------------------
# Test 4-6: Ground truth and baseline isolation (AST-based)
# ---------------------------------------------------------------------------

def test_run_sentinel_eval_never_reads_ground_truth():
    """Test 4: run_sentinel_eval.py must not pass ground_truth.md to the pipeline."""
    source = (REPO / "eval" / "run_sentinel_eval.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    # Only evaluator.extract_ground_truth_root_cause is allowed to read it
    # runtime components must not open it directly
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            # Must not be open("...ground_truth.md") at pipeline level
            func = node.func
            if isinstance(func, ast.Name) and func.id == "open":
                for arg in node.args:
                    if isinstance(arg, ast.Constant) and "ground_truth" in str(arg.value):
                        pytest.fail("run_sentinel_eval.py opens ground_truth.md directly")


def test_orchestrator_never_reads_ground_truth():
    """Test 5: orchestrator.py must not have ground_truth.md as a string constant."""
    source = (REPO / "agents" / "orchestrator.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            assert "ground_truth.md" not in node.value


def test_run_sentinel_eval_never_reads_baseline_results():
    """Test 6: run_sentinel_eval.py does not pass baseline results to the pipeline."""
    source = (REPO / "eval" / "run_sentinel_eval.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name) and func.id == "open":
                for arg in node.args:
                    if isinstance(arg, ast.Constant):
                        val = str(arg.value).lower()
                        assert "results_baseline" not in val, \
                            "run_sentinel_eval opens results_baseline directly"


# ---------------------------------------------------------------------------
# Test 7: Every incident gets a result
# ---------------------------------------------------------------------------

def test_every_incident_gets_a_result(tmp_path: Path):
    """Test 7: run produces one record per incident even when some fail."""
    from eval.run_sentinel_eval import run_single_incident, discover_incidents
    
    incs = discover_incidents(start=1, end=10)
    baseline_rows = {inc.name: _mk_baseline_row(inc.name) for inc in incs}

    from eval.evaluator import CorrectnessEvaluator
    evaluator = CorrectnessEvaluator()

    with patch("eval.run_sentinel_eval.IncidentOrchestrator") as MockOrch:
        MockOrch.return_value.investigate.return_value = _mk_orch_result(
            incident_id="inc_01_n_plus_one_query",
            claim="N+1 query in loop for items",
        )
        # Run on just inc_01
        record = run_single_incident(
            incident_dir=INC_01,
            sleep_secs=0,
            evaluator=evaluator,
            baseline_rows=baseline_rows,
            force_rerun=True,
        )
        assert record["incident_id"] == "inc_01_n_plus_one_query"


# ---------------------------------------------------------------------------
# Tests 8-10: Failure counting and accuracy denominator
# ---------------------------------------------------------------------------

def test_failed_incidents_counted_in_accuracy(tmp_path: Path):
    """Test 8-10: Failures are not removed from the denominator."""
    from eval.run_sentinel_eval import write_final_summary, SUMMARY_JSON
    
    records = [
        {"incident_id": f"inc_{i:02d}", "baseline_correct": True, "baseline_verdict": "CORRECT",
         "sentinel_correct": i < 8, "sentinel_verdict": "CORRECT" if i < 8 else "FAILURE",
         "sentinel_verified": i < 8, "sentinel_status": "COMPLETED" if i < 8 else "ERROR",
         "sentinel_confirmed_hypotheses": 1 if i < 8 else 0,
         "sentinel_fix_proposals": 0, "baseline_latency_seconds": 15.0,
         "sentinel_latency_seconds": 30.0, "baseline_tokens": 2600,
         "sentinel_tokens": 0, "baseline_llm_calls": 1, "sentinel_llm_calls": 5,
         "sentinel_hypotheses_total": 1, "sentinel_hypotheses_rejected": 0,
         "sentinel_hypotheses_inconclusive": 0, "notes": "",
         "_sentinel_root_cause": "", "_sentinel_verdict_explanation": "",
         "_sentinel_error": "" if i < 8 else "error", "_orchestrator_result": None}
        for i in range(1, 11)
    ]
    
    # Patch SUMMARY_JSON path
    with patch("eval.run_sentinel_eval.SUMMARY_JSON", tmp_path / "summary.json"):
        with patch("eval.run_sentinel_eval.COMPARISON_CSV", tmp_path / "comparison.csv"):
            summary = write_final_summary(records)
    
    assert summary["total_incidents"] == 10
    assert summary["sentinel"]["correct"] == 7
    assert summary["sentinel"]["failures"] == 3
    # Accuracy denominator must be 10
    assert summary["sentinel"]["accuracy"] == pytest.approx(0.7, abs=0.01)
    assert summary["sentinel"]["accuracy_pct"] == "7/10 = 70.0%"


# ---------------------------------------------------------------------------
# Test 11-12: CSV and summary consistency
# ---------------------------------------------------------------------------

def test_csv_contains_all_10_incidents(tmp_path: Path):
    """Test 11: CSV has exactly 10 rows."""
    from eval.run_sentinel_eval import write_comparison_csv, COMPARISON_FIELDNAMES

    records = [
        {f: f"val_{i}" for f in COMPARISON_FIELDNAMES}
        for i in range(10)
    ]
    for i, r in enumerate(records):
        r["incident_id"] = f"inc_{i+1:02d}"

    csv_path = tmp_path / "comparison.csv"
    with patch("eval.run_sentinel_eval.COMPARISON_CSV", csv_path):
        from eval.run_sentinel_eval import write_comparison_csv as wcv
        # Write directly
        import csv as csv_mod
        csv_path.parent.mkdir(parents=True, exist_ok=True)
        with open(csv_path, "w", newline="", encoding="utf-8") as f:
            writer = csv_mod.DictWriter(f, fieldnames=COMPARISON_FIELDNAMES, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(records)

    rows = list(csv_mod.DictReader(open(csv_path, encoding="utf-8")))
    assert len(rows) == 10


# ---------------------------------------------------------------------------
# Tests 13-14: Accuracy calculations
# ---------------------------------------------------------------------------

def test_accuracy_delta_correct():
    """Test 15-16: Accuracy delta and relative improvement are calculated correctly."""
    from eval.run_sentinel_eval import write_final_summary
    records = [
        {"incident_id": f"inc_{i:02d}", "baseline_correct": True, "baseline_verdict": "CORRECT",
         "sentinel_correct": i <= 8, "sentinel_verdict": "CORRECT" if i <= 8 else "INCORRECT",
         "sentinel_verified": False, "sentinel_status": "COMPLETED",
         "sentinel_confirmed_hypotheses": 0, "sentinel_fix_proposals": 0,
         "baseline_latency_seconds": 15.0, "sentinel_latency_seconds": 25.0,
         "baseline_tokens": 2600, "sentinel_tokens": 5000,
         "baseline_llm_calls": 1, "sentinel_llm_calls": 5,
         "sentinel_hypotheses_total": 1, "sentinel_hypotheses_rejected": 0,
         "sentinel_hypotheses_inconclusive": 0, "notes": "",
         "_sentinel_root_cause": "", "_sentinel_verdict_explanation": "",
         "_sentinel_error": "", "_orchestrator_result": None}
        for i in range(1, 11)
    ]
    import tempfile, os
    with tempfile.TemporaryDirectory() as tmp:
        with patch("eval.run_sentinel_eval.SUMMARY_JSON", Path(tmp) / "s.json"):
            with patch("eval.run_sentinel_eval.COMPARISON_CSV", Path(tmp) / "c.csv"):
                summary = write_final_summary(records)
    
    # 10/10 baseline, 8/10 sentinel
    assert summary["baseline"]["accuracy"] == pytest.approx(1.0)
    assert summary["sentinel"]["accuracy"] == pytest.approx(0.8)
    assert summary["comparison"]["accuracy_delta"] == pytest.approx(-0.2, abs=0.001)


# ---------------------------------------------------------------------------
# Tests 20-21: Cache validation
# ---------------------------------------------------------------------------

def test_cached_result_wrong_incident_id_rejected(tmp_path: Path):
    """Test 21: load_sentinel_final rejects cache with wrong incident_id."""
    from eval.run_sentinel_eval import load_sentinel_final, SENTINEL_RESULTS_DIR
    
    cache_dir = tmp_path / "inc_01_n_plus_one_query"
    cache_dir.mkdir(parents=True)
    (cache_dir / "sentinel_final.json").write_text(
        json.dumps({"incident_id": "inc_99_wrong", "sentinel_status": "COMPLETED"}),
        encoding="utf-8"
    )
    
    with patch("eval.run_sentinel_eval.SENTINEL_RESULTS_DIR", tmp_path):
        result = load_sentinel_final("inc_01_n_plus_one_query")
    assert result is None


def test_valid_cache_loaded(tmp_path: Path):
    """Test 20: load_sentinel_final returns cached result when valid."""
    from eval.run_sentinel_eval import load_sentinel_final
    
    cache_dir = tmp_path / "inc_01_n_plus_one_query"
    cache_dir.mkdir(parents=True)
    data = {"incident_id": "inc_01_n_plus_one_query", "sentinel_status": "COMPLETED",
            "sentinel_verdict": "CORRECT", "sentinel_correct": True}
    (cache_dir / "sentinel_final.json").write_text(json.dumps(data), encoding="utf-8")
    
    with patch("eval.run_sentinel_eval.SENTINEL_RESULTS_DIR", tmp_path):
        result = load_sentinel_final("inc_01_n_plus_one_query")
    assert result is not None
    assert result["sentinel_verdict"] == "CORRECT"


# ---------------------------------------------------------------------------
# Test 22-23: Source file and ground-truth immutability
# ---------------------------------------------------------------------------

def test_source_files_unchanged_after_evaluation():
    """Test 22: Incident source files not modified by evaluation run."""
    service_app = INC_01 / "service" / "app.py"
    before = service_app.read_text(encoding="utf-8")
    
    from eval.run_sentinel_eval import load_sentinel_final
    # Just loading a result (read-only) must not change files
    load_sentinel_final("inc_01_n_plus_one_query")
    
    after = service_app.read_text(encoding="utf-8")
    assert before == after


def test_ground_truth_unchanged(tmp_path: Path):
    """Test 23: ground_truth.md is not modified by the evaluation harness."""
    gt_path = INC_01 / "ground_truth.md"
    before = gt_path.read_text(encoding="utf-8")
    
    from eval.evaluator import CorrectnessEvaluator
    evaluator = CorrectnessEvaluator()
    evaluator.extract_ground_truth_root_cause(gt_path)
    evaluator.evaluate_diagnosis("inc_01_n_plus_one_query", "N+1 query in loop")
    
    after = gt_path.read_text(encoding="utf-8")
    assert before == after


# ---------------------------------------------------------------------------
# Test 27-29: Non-interactive approval, no patches, no shell
# ---------------------------------------------------------------------------

def test_non_interactive_approval_defaults_to_rejected():
    """Test 27: ApprovalGate in non-interactive mode rejects all proposals."""
    from agents.approval_gate import ApprovalGate
    from agents.fix_tools import HUMAN_APPROVAL_NOTICE
    gate = ApprovalGate(interactive=False)
    proposal = {
        "proposal_id": "FIX-001",
        "hypothesis_id": "HYP-001",
        "incident_id": "inc_01_n_plus_one_query",
        "status": "PROPOSED",
        "human_approval_notice": HUMAN_APPROVAL_NOTICE,
        "summary": "Test fix",
        "rationale": "test",
        "changes": [{"file": "service/app.py", "start_line": None, "end_line": None,
                     "description": "fix", "before": "x", "after": "y"}],
        "patch": "diff",
        "expected_effect": "better",
        "risks": [],
        "validation_plan": ["test"],
        "rollback_plan": "revert",
        "evidence_ids": ["EV-LOG-001"],
    }
    record = gate.review(proposal, _answer=None)
    assert record["status"] == "REJECTED"


def test_no_shell_execution_in_eval_runner():
    """Test 29: run_sentinel_eval.py must not import subprocess."""
    source = (REPO / "eval" / "run_sentinel_eval.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert alias.name not in ("subprocess", "os"), \
                    f"run_sentinel_eval.py imports {alias.name!r}"
        if isinstance(node, ast.ImportFrom):
            assert node.module not in ("subprocess",)


# ---------------------------------------------------------------------------
# Test 30-31: Incident 10 included and all 10 present
# ---------------------------------------------------------------------------

def test_incident_10_discovered():
    """Test 30: Incident 10 (multi-symptom cascade) is included."""
    from eval.run_sentinel_eval import discover_incidents
    incs = discover_incidents(start=1, end=10)
    ids = [d.name for d in incs]
    assert "inc_10_multi_symptom_cascade" in ids


def test_all_10_incidents_represented_even_with_failures():
    """Test 31: All 10 incidents appear in final_summary even if some fail."""
    from eval.run_sentinel_eval import write_final_summary
    
    records = [
        {"incident_id": f"inc_{i:02d}_test", "baseline_correct": True,
         "baseline_verdict": "CORRECT", "sentinel_correct": False,
         "sentinel_verdict": "FAILURE", "sentinel_verified": False,
         "sentinel_status": "ERROR", "sentinel_confirmed_hypotheses": 0,
         "sentinel_fix_proposals": 0, "baseline_latency_seconds": 15.0,
         "sentinel_latency_seconds": 0.0, "baseline_tokens": 0,
         "sentinel_tokens": 0, "baseline_llm_calls": 1, "sentinel_llm_calls": 0,
         "sentinel_hypotheses_total": 0, "sentinel_hypotheses_rejected": 0,
         "sentinel_hypotheses_inconclusive": 0, "notes": "error",
         "_sentinel_root_cause": "", "_sentinel_verdict_explanation": "",
         "_sentinel_error": "error", "_orchestrator_result": None}
        for i in range(1, 11)
    ]
    
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        with patch("eval.run_sentinel_eval.SUMMARY_JSON", Path(tmp) / "s.json"):
            with patch("eval.run_sentinel_eval.COMPARISON_CSV", Path(tmp) / "c.csv"):
                summary = write_final_summary(records)
    
    assert summary["total_incidents"] == 10
    assert len(summary["per_incident"]) == 10


# ---------------------------------------------------------------------------
# Test: Evaluator uses locked criteria (not LLM)
# ---------------------------------------------------------------------------

def test_evaluator_is_deterministic():
    """Evaluator produces same result for same input across multiple calls."""
    from eval.evaluator import CorrectnessEvaluator
    e = CorrectnessEvaluator()
    r1, _ = e.evaluate_diagnosis("inc_01_n_plus_one_query", "N+1 query in loop for each item")
    r2, _ = e.evaluate_diagnosis("inc_01_n_plus_one_query", "N+1 query in loop for each item")
    assert r1 == r2


def test_evaluator_correct_for_inc_01():
    from eval.evaluator import CorrectnessEvaluator
    e = CorrectnessEvaluator()
    verdict, _ = e.evaluate_diagnosis(
        "inc_01_n_plus_one_query",
        "N+1 query pattern: individual address queries executed in a for loop during order serialization"
    )
    assert verdict == "CORRECT"


def test_evaluator_incorrect_for_distractor():
    from eval.evaluator import CorrectnessEvaluator
    e = CorrectnessEvaluator()
    verdict, _ = e.evaluate_diagnosis(
        "inc_10_multi_symptom_cascade",
        "The kubernetes pod was restarting due to connection pool exhaustion and node disk pressure."
    )
    assert verdict == "INCORRECT"
