"""Tests for IncidentOrchestrator persistence and resume capabilities (Mission 03).

All agent and LLM calls are mocked; no real Groq API calls are executed.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict
from unittest.mock import MagicMock, call, patch

import pytest

from agents.fix_tools import HUMAN_APPROVAL_NOTICE
from agents.orchestrator import (
    PIPELINE_COMPLETED,
    PIPELINE_FAILED,
    PIPELINE_PARTIAL,
    STATUS_FAILED,
    STATUS_REUSED,
    STATUS_SUCCEEDED,
    IncidentOrchestrator,
    _find_resume_stage,
)
from core.domain.models import IncidentStatus, StageStatus
from core.domain.state import InvestigationState
from core.persistence.filesystem import FilesystemRepository
from core.persistence.repository import PersistenceError

REPO = Path(__file__).parent.parent
INC_01 = REPO / "incidents" / "inc_01_n_plus_one_query"


# ---------------------------------------------------------------------------
# Helpers & Mocks
# ---------------------------------------------------------------------------

def _mk_logs(incident_id: str = "inc_01_n_plus_one_query") -> Dict[str, Any]:
    return {
        "incident_id": incident_id,
        "agent": "logs_agent",
        "summary": "test logs",
        "evidence": [
            {
                "evidence_id": "EV-LOG-001",
                "source": "logs",
                "reference": "logs/application.log:1",
                "timestamp": "",
                "type": "error",
                "excerpt": "log excerpt 1",
                "interpretation": "interp 1",
            }
        ],
    }


def _mk_metrics(incident_id: str = "inc_01_n_plus_one_query") -> Dict[str, Any]:
    return {
        "incident_id": incident_id,
        "agent": "metrics_agent",
        "summary": "test metrics",
        "evidence": [
            {
                "evidence_id": "EV-MET-001",
                "source": "metrics",
                "reference": "metrics/metrics.csv:1",
                "timestamp": "",
                "type": "spike",
                "excerpt": "metric excerpt 1",
                "interpretation": "interp 1",
            }
        ],
    }


def _mk_code(incident_id: str = "inc_01_n_plus_one_query") -> Dict[str, Any]:
    return {
        "incident_id": incident_id,
        "agent": "code_agent",
        "summary": "test code",
        "evidence": [
            {
                "evidence_id": "EV-CODE-001",
                "source": "code",
                "reference": "service/app.py:1",
                "timestamp": "",
                "type": "suspicious_pattern",
                "excerpt": "code excerpt 1",
                "interpretation": "interp 1",
            }
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
                "checks": [
                    {
                        "check_id": "CHK-001",
                        "description": "check",
                        "result": "PASS",
                        "evidence": ["EV-LOG-001"],
                        "reference": "service/app.py:22",
                    }
                ],
                "reasoning": "All checks passed.",
                "confidence": 0.9,
            }
        ],
    }


def _mk_fix_proposals() -> Dict[str, Any]:
    return {
        "incident_id": "inc_01_n_plus_one_query",
        "proposals": [
            {
                "proposal_id": "FIX-001",
                "hypothesis_id": "HYP-001",
                "incident_id": "inc_01_n_plus_one_query",
                "status": "PROPOSED",
                "human_approval_notice": HUMAN_APPROVAL_NOTICE,
                "summary": "Fix 1",
                "rationale": "rationale",
                "changes": [
                    {
                        "file": "service/app.py",
                        "start_line": None,
                        "end_line": None,
                        "description": "desc",
                        "before": "old",
                        "after": "new",
                    }
                ],
                "patch": "--- a/service/app.py\n+++ b/service/app.py\n@@ -1 +1 @@\n-old\n+new",
                "expected_effect": "better",
                "risks": [],
                "validation_plan": ["run tests"],
                "rollback_plan": "revert",
                "evidence_ids": ["EV-LOG-001"],
            }
        ],
        "validation_errors": {},
        "skipped_hypotheses": [],
    }


def _mk_approvals() -> Dict[str, Any]:
    return {
        "incident_id": "inc_01_n_plus_one_query",
        "approval_records": [],
        "summary": {"total": 1, "approved": 0, "rejected": 1},
    }


def _patch_pipeline_agents(
    mock_logs=None,
    mock_metrics=None,
    mock_code=None,
    mock_hyp=None,
    mock_ver=None,
    mock_fix=None,
    mock_gate=None,
):
    """Context manager patching all specialist agents with sensible defaults."""
    p_logs = patch("agents.orchestrator.LogsAgent")
    p_metrics = patch("agents.orchestrator.MetricsAgent")
    p_code = patch("agents.orchestrator.CodeAgent")
    p_hyp = patch("agents.orchestrator.HypothesisEngine")
    p_ver = patch("agents.orchestrator.VerificationAgent")
    p_fix = patch("agents.orchestrator.FixProposalAgent")
    p_gate = patch("agents.orchestrator.ApprovalGate")

    class AgentMocks:
        def __enter__(self):
            self.logs = p_logs.__enter__()
            self.metrics = p_metrics.__enter__()
            self.code = p_code.__enter__()
            self.hyp = p_hyp.__enter__()
            self.ver = p_ver.__enter__()
            self.fix = p_fix.__enter__()
            self.gate = p_gate.__enter__()

            self.logs.return_value.extract_evidence.return_value = mock_logs or _mk_logs()
            self.metrics.return_value.extract_evidence.return_value = mock_metrics or _mk_metrics()
            self.code.return_value.extract_evidence.return_value = mock_code or _mk_code()
            self.hyp.return_value.generate_hypotheses.return_value = mock_hyp or _mk_hypotheses()
            self.ver.return_value.verify.return_value = mock_ver or _mk_verification()
            self.fix.return_value.propose_fix.return_value = mock_fix or _mk_fix_proposals()
            self.gate.return_value.review_all.return_value = mock_gate or _mk_approvals()
            return self

        def __exit__(self, exc_type, exc_val, exc_tb):
            p_gate.__exit__(exc_type, exc_val, exc_tb)
            p_fix.__exit__(exc_type, exc_val, exc_tb)
            p_ver.__exit__(exc_type, exc_val, exc_tb)
            p_hyp.__exit__(exc_type, exc_val, exc_tb)
            p_code.__exit__(exc_type, exc_val, exc_tb)
            p_metrics.__exit__(exc_type, exc_val, exc_tb)
            p_logs.__exit__(exc_type, exc_val, exc_tb)

    return AgentMocks()


# ---------------------------------------------------------------------------
# Step 14: Deterministic _find_resume_stage helper tests
# ---------------------------------------------------------------------------

def test_find_resume_stage_empty_state():
    state = InvestigationState(incident_id="inc_01")
    assert _find_resume_stage(state) == "logs"


def test_find_resume_stage_skips_succeeded_and_reused():
    state = InvestigationState(incident_id="inc_01")
    state.start_stage("logs")
    state.complete_stage("logs", output={"data": 1})
    state.start_stage("metrics")
    state.mark_cached("metrics", output={"data": 2})

    # Next missing stage is code
    assert _find_resume_stage(state) == "code"


def test_find_resume_stage_retries_failed_or_running():
    state = InvestigationState(incident_id="inc_01")
    state.start_stage("logs")
    state.complete_stage("logs", output={})
    state.start_stage("metrics")
    state.fail_stage("metrics", error="API error")

    assert _find_resume_stage(state) == "metrics"


def test_find_resume_stage_all_completed():
    state = InvestigationState(incident_id="inc_01")
    from agents.orchestrator import CANONICAL_STAGE_ORDER
    for st in CANONICAL_STAGE_ORDER:
        state.start_stage(st)
        state.complete_stage(st, output={})

    assert _find_resume_stage(state) is None


# ---------------------------------------------------------------------------
# Steps 11 & 12: Orchestrator persistence lifecycle hooks
# ---------------------------------------------------------------------------

def test_orchestrator_saves_initial_stage_completion_and_final_state(tmp_path: Path):
    """Orchestrator persists state at key lifecycle stages."""
    repo = FilesystemRepository(persistence_root=tmp_path)
    orch = IncidentOrchestrator(non_interactive=True, repository=repo)

    saved_statuses = []
    original_save = repo.save

    def tracking_save(st):
        saved_statuses.append(st.status)
        return original_save(st)

    with patch.object(repo, "save", side_effect=tracking_save):
        with _patch_pipeline_agents():
            res = orch.investigate(INC_01)

    assert len(saved_statuses) >= 9  # initial + 8 stages + final
    # Initial status was RUNNING
    assert saved_statuses[0] == IncidentStatus.RUNNING
    # Final status was COMPLETED
    assert saved_statuses[-1] == IncidentStatus.COMPLETED
    assert res["pipeline_status"] == PIPELINE_COMPLETED


def test_orchestrator_saves_stage_failure(tmp_path: Path):
    """When a stage fails, the failed stage status is persisted in the state."""
    repo = FilesystemRepository(persistence_root=tmp_path)
    orch = IncidentOrchestrator(non_interactive=True, repository=repo)

    with _patch_pipeline_agents() as mocks:
        mocks.logs.return_value.extract_evidence.side_effect = RuntimeError("Log parsing crashed")
        res = orch.investigate(INC_01)

    assert res["pipeline_status"] == PIPELINE_PARTIAL
    saved_state = repo.load("inc_01_n_plus_one_query")
    assert saved_state is not None
    assert saved_state.stages["logs"].status == StageStatus.FAILED
    assert "Log parsing crashed" in (saved_state.stages["logs"].error or "")


def test_persistence_failure_raises_immediately(tmp_path: Path):
    """If repository.save() fails, error is NOT swallowed and raises immediately."""
    repo = FilesystemRepository(persistence_root=tmp_path)
    orch = IncidentOrchestrator(non_interactive=True, repository=repo)

    with patch.object(repo, "save", side_effect=PersistenceError("Disk full")):
        with pytest.raises(PersistenceError, match="Disk full"):
            orch.investigate(INC_01)


# ---------------------------------------------------------------------------
# Steps 13-17: Resume API & Execution
# ---------------------------------------------------------------------------

def test_resume_without_repository_fails_clearly():
    orch = IncidentOrchestrator(non_interactive=True, repository=None)
    with pytest.raises(PersistenceError, match="Persistence repository is required"):
        orch.resume("inc_01_n_plus_one_query", INC_01)


def test_resume_missing_state_fails(tmp_path: Path):
    repo = FilesystemRepository(persistence_root=tmp_path)
    orch = IncidentOrchestrator(non_interactive=True, repository=repo)
    with pytest.raises(PersistenceError, match="No persisted state found"):
        orch.resume("inc_01_n_plus_one_query", INC_01)


def test_resume_refuses_completed_investigation(tmp_path: Path):
    repo = FilesystemRepository(persistence_root=tmp_path)
    state = InvestigationState(incident_id="inc_01_n_plus_one_query")
    state.complete()
    repo.save(state)

    orch = IncidentOrchestrator(non_interactive=True, repository=repo)
    with pytest.raises(PersistenceError, match="Cannot resume completed investigation"):
        orch.resume("inc_01_n_plus_one_query", INC_01)


def test_resume_skips_succeeded_stages_and_no_duplicate_llm_calls(tmp_path: Path):
    """Resuming skips SUCCEEDED stages and does NOT call their agents/LLMs."""
    repo = FilesystemRepository(persistence_root=tmp_path)
    state = InvestigationState(incident_id="inc_01_n_plus_one_query")

    # Complete stages logs, metrics, code
    state.start_stage("logs")
    state.complete_stage("logs", output=_mk_logs(), llm_calls=1)
    state.add_evidence(_mk_logs())

    state.start_stage("metrics")
    state.complete_stage("metrics", output=_mk_metrics(), llm_calls=1)
    state.add_evidence(_mk_metrics())

    state.start_stage("code")
    state.complete_stage("code", output=_mk_code(), llm_calls=1)
    state.add_evidence(_mk_code())

    # State halted before evidence_fusion
    repo.save(state)

    orch = IncidentOrchestrator(non_interactive=True, repository=repo)
    with _patch_pipeline_agents() as mocks:
        res = orch.resume("inc_01_n_plus_one_query", INC_01)

        # LogsAgent, MetricsAgent, CodeAgent should NEVER be invoked
        assert mocks.logs.return_value.extract_evidence.call_count == 0
        assert mocks.metrics.return_value.extract_evidence.call_count == 0
        assert mocks.code.return_value.extract_evidence.call_count == 0

        # Downstream stages should have run
        assert mocks.hyp.return_value.generate_hypotheses.call_count == 1
        assert mocks.ver.return_value.verify.call_count == 1
        assert mocks.fix.return_value.propose_fix.call_count == 1
        assert mocks.gate.return_value.review_all.call_count == 1

    assert res["pipeline_status"] == PIPELINE_COMPLETED
    assert res["stages"]["logs"]["status"] == STATUS_SUCCEEDED


def test_resume_skips_reused_cached_stages(tmp_path: Path):
    """Resuming skips stages marked REUSED / CACHED."""
    repo = FilesystemRepository(persistence_root=tmp_path)
    state = InvestigationState(incident_id="inc_01_n_plus_one_query")

    state.start_stage("logs")
    state.mark_cached("logs", output=_mk_logs())
    state.add_evidence(_mk_logs())

    repo.save(state)

    orch = IncidentOrchestrator(non_interactive=True, repository=repo)
    with _patch_pipeline_agents() as mocks:
        orch.resume("inc_01_n_plus_one_query", INC_01)
        assert mocks.logs.return_value.extract_evidence.call_count == 0
        assert mocks.metrics.return_value.extract_evidence.call_count == 1


def test_resume_retries_failed_stage(tmp_path: Path):
    """Resuming retries the failed stage and proceeds to completion."""
    repo = FilesystemRepository(persistence_root=tmp_path)
    state = InvestigationState(incident_id="inc_01_n_plus_one_query")

    # logs succeeded, metrics failed
    state.start_stage("logs")
    state.complete_stage("logs", output=_mk_logs())
    state.add_evidence(_mk_logs())

    state.start_stage("metrics")
    state.fail_stage("metrics", error="Temporary 503")
    state.fail(error="Metrics failed")
    repo.save(state)

    orch = IncidentOrchestrator(non_interactive=True, repository=repo)
    with _patch_pipeline_agents() as mocks:
        res = orch.resume("inc_01_n_plus_one_query", INC_01)

        # logs skipped
        assert mocks.logs.return_value.extract_evidence.call_count == 0
        # metrics retried
        assert mocks.metrics.return_value.extract_evidence.call_count == 1

    assert res["pipeline_status"] == PIPELINE_COMPLETED
    assert res["stages"]["metrics"]["status"] == STATUS_SUCCEEDED


def test_resume_retries_running_crashed_stage(tmp_path: Path):
    """Crash semantics: if process crashed while stage was RUNNING, retry from start."""
    repo = FilesystemRepository(persistence_root=tmp_path)
    state = InvestigationState(incident_id="inc_01_n_plus_one_query")

    state.start_stage("logs")
    state.complete_stage("logs", output=_mk_logs())
    state.add_evidence(_mk_logs())

    # Process crashed during metrics
    state.start_stage("metrics")
    assert state.current_stage == "metrics"
    repo.save(state)

    orch = IncidentOrchestrator(non_interactive=True, repository=repo)
    with _patch_pipeline_agents() as mocks:
        res = orch.resume("inc_01_n_plus_one_query", INC_01)
        # logs skipped
        assert mocks.logs.return_value.extract_evidence.call_count == 0
        # metrics retried from beginning
        assert mocks.metrics.return_value.extract_evidence.call_count == 1

    assert res["pipeline_status"] == PIPELINE_COMPLETED


def test_resume_partial_investigation(tmp_path: Path):
    """Resume an investigation in PARTIAL status (e.g. stopped after hypotheses)."""
    repo = FilesystemRepository(persistence_root=tmp_path)
    state = InvestigationState(incident_id="inc_01_n_plus_one_query")

    state.start_stage("logs")
    state.complete_stage("logs", output=_mk_logs())
    state.add_evidence(_mk_logs())

    state.start_stage("metrics")
    state.complete_stage("metrics", output=_mk_metrics())
    state.add_evidence(_mk_metrics())

    state.start_stage("code")
    state.complete_stage("code", output=_mk_code())
    state.add_evidence(_mk_code())

    state.start_stage("evidence_fusion")
    fused = {"incident_id": "inc_01_n_plus_one_query", "evidence": list(state.evidence)}
    state.complete_stage("evidence_fusion", output=fused)

    state.start_stage("hypotheses")
    state.complete_stage("hypotheses", output=_mk_hypotheses())
    state.add_hypotheses(_mk_hypotheses())

    # Stopped and marked PARTIAL
    state.mark_partial()
    repo.save(state)

    orch = IncidentOrchestrator(non_interactive=True, repository=repo)
    with _patch_pipeline_agents() as mocks:
        res = orch.resume("inc_01_n_plus_one_query", INC_01)
        # Stages 1-5 skipped
        assert mocks.logs.return_value.extract_evidence.call_count == 0
        assert mocks.hyp.return_value.generate_hypotheses.call_count == 0
        # Stages 6-8 run
        assert mocks.ver.return_value.verify.call_count == 1
        assert mocks.fix.return_value.propose_fix.call_count == 1

    assert res["pipeline_status"] == PIPELINE_COMPLETED


def test_state_remains_deduplicated_after_resume(tmp_path: Path):
    """ID-based state deduplication prevents items from duplicating on retry."""
    repo = FilesystemRepository(persistence_root=tmp_path)
    state = InvestigationState(incident_id="inc_01_n_plus_one_query")

    # Add evidence in logs stage
    state.start_stage("logs")
    state.complete_stage("logs", output=_mk_logs())
    state.add_evidence(_mk_logs())
    initial_ev_count = len(state.evidence)

    # Crash during code stage
    state.start_stage("code")
    repo.save(state)

    orch = IncidentOrchestrator(non_interactive=True, repository=repo)
    with _patch_pipeline_agents():
        orch.resume("inc_01_n_plus_one_query", INC_01)

    final_state = repo.load("inc_01_n_plus_one_query")
    assert final_state is not None
    # No duplicate EV-LOG-001
    log_evs = [e for e in final_state.evidence if e.evidence_id == "EV-LOG-001"]
    assert len(log_evs) == 1


def test_existing_external_orchestrator_result_unchanged(tmp_path: Path):
    """Result schema and structure returned by resume() match investigate()."""
    repo = FilesystemRepository(persistence_root=tmp_path)
    orch = IncidentOrchestrator(non_interactive=True, repository=repo)

    with _patch_pipeline_agents():
        res1 = orch.investigate(INC_01)

    expected_keys = {
        "incident_id",
        "pipeline_status",
        "human_approval_notice",
        "llm_call_count",
        "prompt_tokens",
        "completion_tokens",
        "total_tokens",
        "stages",
        "summary",
        "error",
    }
    assert expected_keys.issubset(set(res1.keys()))
    assert res1["human_approval_notice"] == HUMAN_APPROVAL_NOTICE
    assert res1["pipeline_status"] == PIPELINE_COMPLETED


def test_existing_stage_cache_behaviour_unchanged(tmp_path: Path):
    """Existing output_dir per-stage caching works alongside persistence repository."""
    cache_dir = tmp_path / "stage_cache"
    persist_dir = tmp_path / "persistence"

    repo = FilesystemRepository(persistence_root=persist_dir)
    orch = IncidentOrchestrator(
        non_interactive=True,
        output_dir=cache_dir,
        repository=repo,
    )

    with _patch_pipeline_agents():
        orch.investigate(INC_01)

    # Both stage cache files and state.json should exist
    assert (persist_dir / "inc_01_n_plus_one_query" / "state.json").exists()
    assert (cache_dir / "inc_01_n_plus_one_query" / "evidence_fusion.json").exists()
