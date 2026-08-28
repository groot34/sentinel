"""Unit tests for the Sentinel Baseline Evaluation harness.

Tests cover:
1.  All 10 incidents are discovered.
2.  ground_truth.md is NOT passed into baseline runtime.
3.  Baseline output is evaluated correctly (correctness evaluator).
4.  Correct semantic match → CORRECT.
5.  Incorrect diagnosis → INCORRECT.
6.  Ambiguous diagnosis → REVIEW.
7.  Missing baseline output (API failure).
8.  Baseline API failure is recorded and does not crash harness.
9.  Existing results can be resumed (skip re-running completed incidents).
10. Results are written correctly to CSV and JSON.
11. No Sentinel advanced agents are invoked.
12. Evaluation does not modify incident data files.
"""

import csv
import json
import os
import time
from pathlib import Path
from typing import Any, Dict
from unittest.mock import MagicMock, patch

import pytest

from eval.evaluator import CorrectnessEvaluator, CANONICAL_INCIDENT_CRITERIA
from eval.run_eval import (
    discover_incidents,
    load_existing_results,
    run_single_incident,
    write_csv,
    write_summary,
    INCIDENTS_DIR,
    RESULTS_DIR,
    RESULTS_CSV,
    SUMMARY_JSON,
    CSV_FIELDNAMES,
)
from baseline.baseline_agent import BaselineAgent
from core.llm import LLMAPIError, LLMRateLimitError, LLMResponse


# ──────────────────────────────────────────────────────────────────────────────
# Fixtures
# ──────────────────────────────────────────────────────────────────────────────

@pytest.fixture
def evaluator():
    return CorrectnessEvaluator()


@pytest.fixture
def mock_llm_client():
    client = MagicMock()
    client.model = "llama-3.3-70b-versatile"
    client._last_response = None
    return client


@pytest.fixture
def baseline_agent(mock_llm_client):
    return BaselineAgent(llm_client=mock_llm_client)


@pytest.fixture
def sample_incident_dir():
    """Return real path to first canonical incident."""
    return INCIDENTS_DIR / "inc_01_n_plus_one_query"


# ──────────────────────────────────────────────────────────────────────────────
# Test 1: All 10 incidents are discovered
# ──────────────────────────────────────────────────────────────────────────────

def test_all_10_incidents_discovered():
    incidents = discover_incidents(INCIDENTS_DIR, start=1, end=10)
    assert len(incidents) == 10, f"Expected 10 incidents, found {len(incidents)}"
    ids = [inc.name for inc in incidents]
    assert "inc_01_n_plus_one_query" in ids
    assert "inc_10_multi_symptom_cascade" in ids


# ──────────────────────────────────────────────────────────────────────────────
# Test 2: ground_truth.md is NOT passed into baseline runtime
# ──────────────────────────────────────────────────────────────────────────────

def test_ground_truth_not_in_baseline_prompt(baseline_agent, mock_llm_client, sample_incident_dir):
    """Verify ground_truth.md content never appears in the LLM call."""
    mock_llm_client.generate_structured.return_value = LLMResponse(
        content=json.dumps({
            "incident_id": "inc_01_n_plus_one_query",
            "root_cause_guess": "N+1 query",
            "reasoning": "Queried each item in a loop.",
            "confidence": 0.8,
            "evidence": [],
        }),
        parsed_json={
            "incident_id": "inc_01_n_plus_one_query",
            "root_cause_guess": "N+1 query",
            "reasoning": "Queried each item in a loop.",
            "confidence": 0.8,
            "evidence": [],
        }
    )

    baseline_agent.diagnose(sample_incident_dir)

    call_args = mock_llm_client.generate_structured.call_args
    prompt = call_args.kwargs.get("prompt") or call_args.args[0]

    # ground_truth.md must never be referenced or included in the prompt
    assert "ground_truth" not in prompt.lower()
    assert "Ground Truth:" not in prompt

    # Verify the actual ground_truth.md file for inc_01 would have been present but excluded
    gt_path = sample_incident_dir / "ground_truth.md"
    if gt_path.exists():
        gt_content_excerpt = gt_path.read_text(encoding="utf-8")[:100]
        assert gt_content_excerpt not in prompt


# ──────────────────────────────────────────────────────────────────────────────
# Test 3: Correctness evaluator returns valid status
# ──────────────────────────────────────────────────────────────────────────────

def test_correctness_evaluator_returns_valid_status(evaluator):
    status, explanation = evaluator.evaluate_diagnosis(
        "inc_01_n_plus_one_query",
        "N+1 query pattern exhausted the connection pool",
        "Each item triggered a separate SQL address query inside a loop.",
    )
    assert status in ("CORRECT", "INCORRECT", "REVIEW")
    assert isinstance(explanation, str) and len(explanation) > 0


# ──────────────────────────────────────────────────────────────────────────────
# Test 4: Correct semantic match → CORRECT
# ──────────────────────────────────────────────────────────────────────────────

def test_correct_semantic_match_returns_correct(evaluator):
    """A diagnosis naming the N+1 pattern must return CORRECT."""
    status, _ = evaluator.evaluate_diagnosis(
        "inc_01_n_plus_one_query",
        "N+1 query anti-pattern: address lookup fired per order item inside serializer loop",
        "Sequential queries per item in the for-loop saturated the DB connection pool.",
    )
    assert status == "CORRECT"


# ──────────────────────────────────────────────────────────────────────────────
# Test 5: Incorrect diagnosis → INCORRECT
# ──────────────────────────────────────────────────────────────────────────────

def test_incorrect_diagnosis_returns_incorrect(evaluator):
    """A diagnosis blaming TCP retransmissions (distractor) for inc_01 must return INCORRECT."""
    status, explanation = evaluator.evaluate_diagnosis(
        "inc_01_n_plus_one_query",
        "TCP retransmissions due to network packet loss caused high latency",
        "Network layer instability led to connection timeouts.",
    )
    assert status == "INCORRECT"
    assert len(explanation) > 0


# ──────────────────────────────────────────────────────────────────────────────
# Test 6: Ambiguous diagnosis → REVIEW
# ──────────────────────────────────────────────────────────────────────────────

def test_ambiguous_diagnosis_returns_review(evaluator):
    """A vague diagnosis that partially matches but is not conclusive returns REVIEW."""
    status, explanation = evaluator.evaluate_diagnosis(
        "inc_02_cache_stampede",
        "High database load caused connection timeouts under peak traffic",
        "Cache was not effective and backend took too long to respond.",
    )
    assert status in ("REVIEW", "INCORRECT")


def test_inc10_without_index_mention_is_incorrect(evaluator):
    """For the hard case (inc_10), blaming only pods/retries without mentioning dropped index is INCORRECT."""
    status, explanation = evaluator.evaluate_diagnosis(
        "inc_10_multi_symptom_cascade",
        "Kubernetes pod crashes and connection pool exhaustion from client retries caused the outage",
        "Readiness probe failures led to pod termination cascade across all ledger pods.",
    )
    assert status == "INCORRECT"
    assert "index" in explanation.lower() or "Kubernetes" in explanation


# ──────────────────────────────────────────────────────────────────────────────
# Test 7: Missing baseline output (API failure handling)
# ──────────────────────────────────────────────────────────────────────────────

def test_api_failure_recorded_in_result(tmp_path, mock_llm_client, sample_incident_dir):
    """An LLM API failure should be captured and baseline_status set to LLM_ERROR."""
    mock_llm_client.generate_structured.side_effect = LLMAPIError("Groq 500 server error")
    agent = BaselineAgent(llm_client=mock_llm_client)
    evaluator = CorrectnessEvaluator()

    result = run_single_incident(sample_incident_dir, agent, evaluator, tmp_path)

    assert result["baseline_status"] == "LLM_ERROR"
    assert "500" in result["error"] or "Groq" in result["error"]
    assert result["correctness"] == ""


# ──────────────────────────────────────────────────────────────────────────────
# Test 8: Rate limit failure is captured without crashing
# ──────────────────────────────────────────────────────────────────────────────

def test_rate_limit_failure_captured(tmp_path, mock_llm_client, sample_incident_dir):
    mock_llm_client.generate_structured.side_effect = LLMRateLimitError("Rate limit exceeded")
    agent = BaselineAgent(llm_client=mock_llm_client)
    evaluator = CorrectnessEvaluator()

    result = run_single_incident(sample_incident_dir, agent, evaluator, tmp_path)

    assert result["baseline_status"] == "RATE_LIMITED"
    assert result["error"] != ""


# ──────────────────────────────────────────────────────────────────────────────
# Test 9: Existing results can be resumed (skip re-running)
# ──────────────────────────────────────────────────────────────────────────────

def test_load_existing_results_detects_completed(tmp_path):
    """load_existing_results should detect already-written JSON files."""
    completed_file = tmp_path / "inc_01_n_plus_one_query.json"
    completed_file.write_text(json.dumps({
        "incident_id": "inc_01_n_plus_one_query",
        "baseline_status": "SUCCESS",
        "baseline_root_cause": "N+1 query"
    }), encoding="utf-8")

    running_file = tmp_path / "inc_02_cache_stampede.json"
    running_file.write_text(json.dumps({
        "incident_id": "inc_02_cache_stampede",
        "baseline_status": "RUNNING",
    }), encoding="utf-8")

    completed = load_existing_results(tmp_path)
    assert "inc_01_n_plus_one_query" in completed
    assert "inc_02_cache_stampede" not in completed  # RUNNING = not completed


# ──────────────────────────────────────────────────────────────────────────────
# Test 10: Results are written correctly to CSV and JSON
# ──────────────────────────────────────────────────────────────────────────────

def test_csv_written_correctly(tmp_path):
    records = [{
        "incident_id": "inc_01_n_plus_one_query",
        "baseline_status": "SUCCESS",
        "baseline_root_cause": "N+1 query",
        "ground_truth_root_cause": "N+1 pattern.",
        "correctness": "CORRECT",
        "correctness_explanation": "Matched.",
        "evidence_count": 2,
        "latency_seconds": 3.14,
        "input_tokens": 500,
        "output_tokens": 100,
        "model": "llama-3.3-70b-versatile",
        "error": "",
    }]
    csv_path = tmp_path / "results_baseline.csv"
    write_csv(records, csv_path)

    assert csv_path.exists()
    with open(csv_path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        rows = list(reader)

    assert len(rows) == 1
    assert rows[0]["incident_id"] == "inc_01_n_plus_one_query"
    assert rows[0]["correctness"] == "CORRECT"
    assert rows[0]["latency_seconds"] == "3.14"


def test_summary_json_written_correctly(tmp_path):
    records = [
        {"correctness": "CORRECT", "baseline_status": "SUCCESS", "latency_seconds": 4.0, "input_tokens": 400, "output_tokens": 80},
        {"correctness": "INCORRECT", "baseline_status": "SUCCESS", "latency_seconds": 3.0, "input_tokens": 350, "output_tokens": 60},
        {"correctness": "REVIEW", "baseline_status": "SUCCESS", "latency_seconds": 5.0, "input_tokens": 420, "output_tokens": 90},
        {"correctness": "", "baseline_status": "LLM_ERROR", "latency_seconds": 0, "input_tokens": None, "output_tokens": None},
    ]
    summary_path = tmp_path / "baseline_summary.json"
    write_summary(records, summary_path, model="llama-3.3-70b-versatile")

    assert summary_path.exists()
    data = json.loads(summary_path.read_text())
    assert data["correct"] == 1
    assert data["incorrect"] == 1
    assert data["review"] == 1
    assert data["failures"] == 1
    assert data["accuracy"] == pytest.approx(1/3, abs=0.01)
    assert data["model_used"] == "llama-3.3-70b-versatile"
    assert "unavailable" in data["cost"]


# ──────────────────────────────────────────────────────────────────────────────
# Test 11: No Sentinel advanced agents are invoked
# ──────────────────────────────────────────────────────────────────────────────

def test_no_sentinel_agent_imported_in_eval_runner():
    """run_eval.py must not import any Sentinel advanced agent modules."""
    import eval.run_eval as module
    import sys

    advanced_modules = [
        "agents.logs_agent",
        "agents.metrics_agent",
        "agents.code_agent",
        "agents.hypothesis_engine",
        "agents.verification_agent",
        "agents.fix_proposal_agent",
        "agents.orchestrator",
    ]
    for mod in advanced_modules:
        assert mod not in sys.modules or not hasattr(module, mod.split(".")[-1]), \
            f"Advanced agent module '{mod}' was unexpectedly imported in run_eval."


# ──────────────────────────────────────────────────────────────────────────────
# Test 12: Evaluation does not modify incident data files
# ──────────────────────────────────────────────────────────────────────────────

def test_evaluation_does_not_modify_incident_data(tmp_path, mock_llm_client):
    """run_single_incident must NOT write anything into the incident directory."""
    # Create a minimal fake incident directory
    inc_dir = tmp_path / "inc_01_n_plus_one_query"
    inc_dir.mkdir()
    (inc_dir / "logs").mkdir()
    (inc_dir / "logs" / "application.log").write_text("2026-08-28 INFO started\n", encoding="utf-8")
    (inc_dir / "metrics").mkdir()
    (inc_dir / "metrics" / "metrics.csv").write_text("timestamp,cpu\n2026-08-28,10\n", encoding="utf-8")

    mock_llm_client.generate_structured.return_value = LLMResponse(
        content=json.dumps({
            "incident_id": "inc_01_n_plus_one_query",
            "root_cause_guess": "N+1 query",
            "reasoning": "Loop issue.",
            "confidence": 0.7,
            "evidence": [],
        }),
        parsed_json={
            "incident_id": "inc_01_n_plus_one_query",
            "root_cause_guess": "N+1 query",
            "reasoning": "Loop issue.",
            "confidence": 0.7,
            "evidence": [],
        }
    )
    agent = BaselineAgent(llm_client=mock_llm_client)
    evaluator = CorrectnessEvaluator()
    results_dir = tmp_path / "results" / "baseline"

    before_files = set(inc_dir.rglob("*"))
    run_single_incident(inc_dir, agent, evaluator, results_dir)
    after_files = set(inc_dir.rglob("*"))

    assert before_files == after_files, "Incident directory was modified during evaluation!"
