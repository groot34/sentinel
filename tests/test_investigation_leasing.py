"""Unit and integration tests for investigation execution leasing, crash recovery, and OCC safety (Mission 12A)."""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock, patch

import pytest
from starlette.testclient import TestClient

from api.app import create_app
from api.routes import _ACTIVE_INVESTIGATIONS, _ACTIVE_LOCK
from core.domain.models import IncidentStatus, StageStatus
from core.domain.state import InvestigationState, StageResult
from core.persistence import ConcurrencyError, PersistenceError, PersistenceRepository
from agents.orchestrator import IncidentOrchestrator


class MockOCCRepository(PersistenceRepository):
    """In-memory mock repository enforcing version-based OCC checks."""

    def __init__(self, states: Optional[Dict[str, InvestigationState]] = None) -> None:
        self._states: Dict[str, InvestigationState] = {}
        if states:
            for k, v in states.items():
                self._states[k] = InvestigationState.from_dict(v.to_dict())
        self.save_history: List[InvestigationState] = []

    def save(self, state: InvestigationState, events: Optional[List[Any]] = None) -> None:
        safe_id = state.incident_id
        current_in_db = self._states.get(safe_id)
        if current_in_db is None:
            # Insert with version 1
            state.version = 1
            self._states[safe_id] = InvestigationState.from_dict(state.to_dict())
        else:
            # Check OCC version
            if current_in_db.version != state.version:
                raise ConcurrencyError(
                    f"Optimistic concurrency conflict for investigation '{safe_id}'. "
                    f"State version is {state.version}, but database version is {current_in_db.version}."
                )
            state.version = current_in_db.version + 1
            self._states[safe_id] = InvestigationState.from_dict(state.to_dict())
        self.save_history.append(state)

    def load(self, investigation_id: str) -> Optional[InvestigationState]:
        stored = self._states.get(investigation_id)
        if stored is None:
            return None
        return InvestigationState.from_dict(stored.to_dict())

    def exists(self, investigation_id: str) -> bool:
        return investigation_id in self._states

    def delete(self, investigation_id: str) -> None:
        self._states.pop(investigation_id, None)

    def list(self) -> List[str]:
        return sorted(self._states.keys())


@pytest.fixture(autouse=True)
def clean_active_registry():
    with _ACTIVE_LOCK:
        _ACTIVE_INVESTIGATIONS.clear()
    yield
    with _ACTIVE_LOCK:
        _ACTIVE_INVESTIGATIONS.clear()


@pytest.fixture
def dummy_incidents_dir(tmp_path: Path) -> Path:
    inc_dir = tmp_path / "incidents"
    inc_dir.mkdir()
    (inc_dir / "inc_01_sample").mkdir()
    (inc_dir / "inc_02_sample").mkdir()
    return inc_dir


# ===========================================================================
# 1. Lease Model Unit Tests
# ===========================================================================

def test_lease_acquisition():
    """acquire_lease successfully grants lease when unowned or expired."""
    state = InvestigationState(incident_id="inc_01")
    assert not state.is_lease_active()

    assert state.acquire_lease(owner_id="worker-1", ttl_seconds=60) is True
    assert state.lease_owner == "worker-1"
    assert state.lease_expires_at is not None
    assert state.is_lease_active() is True


def test_active_lease_rejection():
    """acquire_lease rejects a second worker while lease is active."""
    state = InvestigationState(incident_id="inc_01")
    state.acquire_lease(owner_id="worker-1", ttl_seconds=120)

    # Same worker can re-acquire/extend
    assert state.acquire_lease(owner_id="worker-1", ttl_seconds=120) is True

    # Different worker is rejected
    assert state.acquire_lease(owner_id="worker-2", ttl_seconds=120) is False
    assert state.lease_owner == "worker-1"


def test_expired_lease_takeover():
    """acquire_lease allows takeover when lease expires_at is in the past."""
    state = InvestigationState(incident_id="inc_01")
    # Simulate an expired lease from 10 minutes ago
    past_dt = datetime.now(timezone.utc) - timedelta(minutes=10)
    state.lease_owner = "worker-crashed"
    state.lease_expires_at = past_dt.isoformat()

    assert state.is_lease_active() is False

    # Worker-2 can take over the expired lease
    assert state.acquire_lease(owner_id="worker-2", ttl_seconds=60) is True
    assert state.lease_owner == "worker-2"
    assert state.is_lease_active() is True


def test_lease_renewal():
    """renew_lease extends lease for the current owner."""
    state = InvestigationState(incident_id="inc_01")
    state.acquire_lease(owner_id="worker-1", ttl_seconds=30)
    first_exp = state.lease_expires_at

    # Worker-1 renews with longer TTL
    assert state.renew_lease(owner_id="worker-1", ttl_seconds=300) is True
    assert state.lease_expires_at > first_exp

    # Worker-2 cannot renew Worker-1's active lease
    assert state.renew_lease(owner_id="worker-2", ttl_seconds=300) is False
    assert state.lease_owner == "worker-1"


def test_lease_release():
    """release_lease clears lease fields."""
    state = InvestigationState(incident_id="inc_01")
    state.acquire_lease(owner_id="worker-1", ttl_seconds=60)
    assert state.is_lease_active() is True

    # Non-owner cannot release another owner's active lease
    state.release_lease(owner_id="worker-2")
    assert state.is_lease_active() is True
    assert state.lease_owner == "worker-1"

    # Owner releases
    state.release_lease(owner_id="worker-1")
    assert state.lease_owner is None
    assert state.lease_expires_at is None
    assert state.is_lease_active() is False


def test_terminal_states_release_lease():
    """complete(), mark_partial(), and fail() release active lease."""
    for transition_fn in ["complete", "mark_partial", "fail"]:
        state = InvestigationState(incident_id="inc_01", status=IncidentStatus.RUNNING)
        state.acquire_lease(owner_id="worker-1", ttl_seconds=60)
        assert state.is_lease_active() is True

        getattr(state, transition_fn)()
        assert state.lease_owner is None
        assert state.lease_expires_at is None
        assert state.is_lease_active() is False


# ===========================================================================
# 2. OCC Race & Concurrency Protection
# ===========================================================================

def test_two_worker_occ_race_conflict():
    """Two workers reading version N simultaneously: only first commit succeeds, second raises ConcurrencyError."""
    repo = MockOCCRepository()

    # Initial state saved with expired lease at version 1
    initial_state = InvestigationState(
        incident_id="inc_01",
        status=IncidentStatus.RUNNING,
        lease_owner="crashed-worker",
        lease_expires_at=(datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat(),
    )
    repo.save(initial_state)

    # Worker A loads version 1
    state_a = repo.load("inc_01")
    assert state_a.version == 1
    assert not state_a.is_lease_active()

    # Worker B loads version 1 concurrently
    state_b = repo.load("inc_01")
    assert state_b.version == 1
    assert not state_b.is_lease_active()

    # Worker A acquires lease and saves -> increments to version 2
    assert state_a.acquire_lease(owner_id="worker-A", ttl_seconds=60) is True
    repo.save(state_a)
    assert state_a.version == 2

    # Worker B attempts to acquire lease and save with stale version 1
    assert state_b.acquire_lease(owner_id="worker-B", ttl_seconds=60) is True
    with pytest.raises(ConcurrencyError) as exc_info:
        repo.save(state_b)

    assert "Optimistic concurrency conflict" in str(exc_info.value)
    # Ensure Worker A remains the persisted lease owner
    persisted = repo.load("inc_01")
    assert persisted.lease_owner == "worker-A"
    assert persisted.version == 2


# ===========================================================================
# 3. API Crash Recovery & Resume Tests
# ===========================================================================

def test_api_resume_running_active_lease_returns_409(dummy_incidents_dir: Path):
    """POST /investigations/{id}/resume on RUNNING with active lease returns 409 Conflict."""
    future_exp = (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat()
    running_state = InvestigationState(
        incident_id="inc_01_sample",
        status=IncidentStatus.RUNNING,
        lease_owner="active-worker-99",
        lease_expires_at=future_exp,
    )
    repo = MockOCCRepository(states={"inc_01_sample": running_state})
    app = create_app(repository=repo, incidents_root=dummy_incidents_dir)
    client = TestClient(app)

    response = client.post("/investigations/inc_01_sample/resume")
    assert response.status_code == 409
    data = response.json()
    assert data["error"] == "conflict"
    assert "active lease held by 'active-worker-99'" in data["detail"]


def test_api_resume_running_expired_lease_succeeds(dummy_incidents_dir: Path):
    """POST /investigations/{id}/resume on RUNNING with expired lease safely recovers."""
    past_exp = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
    crashed_state = InvestigationState(
        incident_id="inc_01_sample",
        status=IncidentStatus.RUNNING,
        current_stage="metrics",
        lease_owner="crashed-worker-1",
        lease_expires_at=past_exp,
    )
    crashed_state.stages["logs"] = StageResult(
        stage_name="logs",
        status=StageStatus.SUCCEEDED,
        llm_calls=1,
        prompt_tokens=100,
        completion_tokens=25,
        total_tokens=125,
        output={"raw_output": "logs_ok"},
    )
    crashed_state.stages["metrics"] = StageResult(
        stage_name="metrics",
        status=StageStatus.RUNNING,
        output=None,
    )

    repo = MockOCCRepository(states={"inc_01_sample": crashed_state})
    app = create_app(repository=repo, incidents_root=dummy_incidents_dir)
    client = TestClient(app)

    fake_result = {
        "incident_id": "inc_01_sample",
        "pipeline_status": "COMPLETED",
        "human_approval_notice": "AWAITING HUMAN APPROVAL",
        "llm_call_count": 2,
        "prompt_tokens": 200,
        "completion_tokens": 50,
        "total_tokens": 250,
        "stages": {
            "logs": {"status": "SUCCEEDED", "llm_calls": 1, "prompt_tokens": 100, "completion_tokens": 25, "total_tokens": 125, "cache_hit": False},
            "metrics": {"status": "SUCCEEDED", "llm_calls": 1, "prompt_tokens": 100, "completion_tokens": 25, "total_tokens": 125, "cache_hit": False},
        },
        "summary": {
            "confirmed_hypotheses": 1,
            "rejected_hypotheses": 0,
            "inconclusive_hypotheses": 0,
            "proposals_generated": 1,
            "proposals_approved": 0,
            "proposals_rejected": 1,
        },
        "error": None,
    }

    with patch("api.routes.IncidentOrchestrator") as MockOrch:
        instance = MockOrch.return_value
        instance.state = InvestigationState(incident_id="inc_01_sample", status=IncidentStatus.COMPLETED)
        instance.resume.return_value = fake_result

        response = client.post("/investigations/inc_01_sample/resume")

    assert response.status_code == 200
    data = response.json()
    assert data["investigation_id"] == "inc_01_sample"
    assert data["pipeline_status"] == "COMPLETED"


def test_api_resume_running_missing_lease_succeeds(dummy_incidents_dir: Path):
    """POST /investigations/{id}/resume on legacy RUNNING state without lease succeeds."""
    legacy_state = InvestigationState(
        incident_id="inc_01_sample",
        status=IncidentStatus.RUNNING,
        lease_owner=None,
        lease_expires_at=None,
    )
    repo = MockOCCRepository(states={"inc_01_sample": legacy_state})
    app = create_app(repository=repo, incidents_root=dummy_incidents_dir)
    client = TestClient(app)

    with patch("api.routes.IncidentOrchestrator") as MockOrch:
        instance = MockOrch.return_value
        instance.state = InvestigationState(incident_id="inc_01_sample", status=IncidentStatus.COMPLETED)
        instance.resume.return_value = {
            "incident_id": "inc_01_sample",
            "pipeline_status": "COMPLETED",
            "human_approval_notice": "AWAITING HUMAN APPROVAL",
            "stages": {},
            "summary": {},
            "error": None,
        }

        response = client.post("/investigations/inc_01_sample/resume")

    assert response.status_code == 200


def test_api_resume_failed_and_partial_states(dummy_incidents_dir: Path):
    """POST /investigations/{id}/resume on FAILED and PARTIAL states remains functional."""
    for status_val in [IncidentStatus.FAILED, IncidentStatus.PARTIAL]:
        state = InvestigationState(
            incident_id="inc_01_sample",
            status=status_val,
        )
        repo = MockOCCRepository(states={"inc_01_sample": state})
        app = create_app(repository=repo, incidents_root=dummy_incidents_dir)
        client = TestClient(app)

        with patch("api.routes.IncidentOrchestrator") as MockOrch:
            instance = MockOrch.return_value
            instance.state = InvestigationState(incident_id="inc_01_sample", status=IncidentStatus.COMPLETED)
            instance.resume.return_value = {
                "incident_id": "inc_01_sample",
                "pipeline_status": "COMPLETED",
                "human_approval_notice": "AWAITING HUMAN APPROVAL",
                "stages": {},
                "summary": {},
                "error": None,
            }

            response = client.post("/investigations/inc_01_sample/resume")
            assert response.status_code == 200


def test_api_resume_completed_state_rejected(dummy_incidents_dir: Path):
    """POST /investigations/{id}/resume on COMPLETED state returns 409."""
    state = InvestigationState(
        incident_id="inc_01_sample",
        status=IncidentStatus.COMPLETED,
    )
    repo = MockOCCRepository(states={"inc_01_sample": state})
    app = create_app(repository=repo, incidents_root=dummy_incidents_dir)
    client = TestClient(app)

    response = client.post("/investigations/inc_01_sample/resume")
    assert response.status_code == 409
    assert response.json()["error"] == "conflict"


def test_api_submit_approval_active_lease_returns_409():
    """POST /investigations/{id}/approval returns 409 if investigation is under active lease."""
    future_exp = (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat()
    state = InvestigationState(
        incident_id="inc_01",
        status=IncidentStatus.RUNNING,
        lease_owner="active-worker-1",
        lease_expires_at=future_exp,
    )
    state.stages["fix_proposals"] = StageResult(
        stage_name="fix_proposals",
        status=StageStatus.SUCCEEDED,
        output={"proposals": [{"proposal_id": "FIX-001"}]},
    )
    repo = MockOCCRepository(states={"inc_01": state})
    app = create_app(repository=repo)
    client = TestClient(app)

    response = client.post(
        "/investigations/inc_01/approval",
        json={
            "proposal_id": "FIX-001",
            "decision": "approved",
            "reviewer": "alice",
        },
    )
    assert response.status_code == 409
    assert response.json()["error"] == "conflict"
    assert "active lease" in response.json()["detail"]


# ===========================================================================
# 4. Orchestrator Crash Recovery & Stage Preserving Tests
# ===========================================================================

def test_orchestrator_crash_recovery_preserves_completed_stages(tmp_path: Path):
    """Orchestrator resume on crashed RUNNING state preserves completed stages and restarts incomplete stage."""
    inc_dir = tmp_path / "inc_01_sample"
    inc_dir.mkdir()
    (inc_dir / "logs.txt").write_text("test logs", encoding="utf-8")
    (inc_dir / "metrics.json").write_text("{}", encoding="utf-8")
    (inc_dir / "diff.patch").write_text("", encoding="utf-8")

    past_exp = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
    crashed_state = InvestigationState(
        incident_id="inc_01_sample",
        status=IncidentStatus.RUNNING,
        current_stage="metrics",
        lease_owner="crashed-worker",
        lease_expires_at=past_exp,
    )
    # Stage 1 (logs) was already succeeded before crash
    crashed_state.stages["logs"] = StageResult(
        stage_name="logs",
        status=StageStatus.SUCCEEDED,
        llm_calls=1,
        prompt_tokens=150,
        completion_tokens=50,
        total_tokens=200,
        output={"agent": "logs_agent", "evidence": []},
    )
    # Stage 2 (metrics) was running when crash happened
    crashed_state.stages["metrics"] = StageResult(
        stage_name="metrics",
        status=StageStatus.RUNNING,
        output=None,
    )

    repo = MockOCCRepository(states={"inc_01_sample": crashed_state})
    orchestrator = IncidentOrchestrator(
        repository=repo,
        worker_id="recovering-worker",
        non_interactive=True,
    )

    with patch.object(orchestrator, "_run_evidence_stage") as mock_evidence_stage, \
         patch.object(orchestrator, "_run_hypothesis_stage") as mock_hyp, \
         patch.object(orchestrator, "_run_verification_stage") as mock_ver, \
         patch.object(orchestrator, "_run_fix_proposal_stage") as mock_fix, \
         patch.object(orchestrator, "_run_approval_stage") as mock_appr:

        mock_evidence_stage.return_value = (
            {
                "agent": "metrics_agent",
                "evidence": [
                    {
                        "evidence_id": "EV-MET-001",
                        "source": "metrics",
                        "type": "spike",
                        "reference": "api_latency",
                        "description": "latency spike",
                    }
                ],
            },
            1,
            {"status": "SUCCEEDED", "llm_calls": 1, "prompt_tokens": 100, "completion_tokens": 25, "total_tokens": 125},
        )
        mock_hyp.return_value = (
            {
                "incident_id": "inc_01_sample",
                "hypotheses": [
                    {
                        "hypothesis_id": "HYP-001",
                        "claim": "DB bottleneck",
                        "evidence_ids": ["EV-MET-001"],
                        "supporting_reasoning": "high latency matches query timing",
                        "falsification_criteria": ["query is fast"],
                        "verification_plan": ["check query runtime"],
                    }
                ],
            },
            1,
            {"status": "SUCCEEDED", "llm_calls": 1, "prompt_tokens": 100, "completion_tokens": 25, "total_tokens": 125},
        )
        mock_ver.return_value = ({"results": []}, {"status": "SUCCEEDED"})
        mock_fix.return_value = ({"incident_id": "inc_01_sample", "proposals": []}, 0, {"status": "SUCCEEDED"})
        mock_appr.return_value = ({"approvals": []}, {"status": "SUCCEEDED"})

        result = orchestrator.resume(investigation_id="inc_01_sample", incident_dir=inc_dir)

    assert result["pipeline_status"] == "COMPLETED"

    # Verify logs stage was NOT re-run by _run_evidence_stage
    called_stages = [call.kwargs.get("stage_name") for call in mock_evidence_stage.call_args_list]
    assert "logs" not in called_stages
    assert "metrics" in called_stages
    assert "code" in called_stages

    # Verify final state in repo has lease released (since it completed)
    final_state = repo.load("inc_01_sample")
    assert final_state.status == IncidentStatus.COMPLETED
    assert final_state.lease_owner is None
    assert final_state.lease_expires_at is None
