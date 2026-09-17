"""Unit, functional, concurrency, and security tests for Sentinel 2.0 API Layer."""

from __future__ import annotations

import os
import threading
import time
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


class MockRepository(PersistenceRepository):
    """In-memory mock repository for deterministic API testing."""

    def __init__(self, states: Optional[Dict[str, InvestigationState]] = None) -> None:
        self._states: Dict[str, InvestigationState] = states or {}
        self.save_called: List[InvestigationState] = []

    def save(self, state: InvestigationState) -> None:
        self.save_called.append(state)
        self._states[state.incident_id] = state

    def load(self, investigation_id: str) -> Optional[InvestigationState]:
        return self._states.get(investigation_id)

    def exists(self, investigation_id: str) -> bool:
        return investigation_id in self._states

    def delete(self, investigation_id: str) -> None:
        self._states.pop(investigation_id, None)

    def list(self) -> List[str]:
        return sorted(self._states.keys())


@pytest.fixture(autouse=True)
def clean_active_registry():
    """Ensure active investigation registry is clean before and after every test."""
    with _ACTIVE_LOCK:
        _ACTIVE_INVESTIGATIONS.clear()
    yield
    with _ACTIVE_LOCK:
        _ACTIVE_INVESTIGATIONS.clear()


@pytest.fixture
def dummy_incidents_dir(tmp_path: Path) -> Path:
    """Create a temporary incidents root with sample incident directories."""
    inc_dir = tmp_path / "incidents"
    inc_dir.mkdir()
    (inc_dir / "inc_01_sample").mkdir()
    (inc_dir / "inc_01_sample" / "ground_truth.md").write_text("SECRET_GROUND_TRUTH", encoding="utf-8")
    (inc_dir / "inc_02_sample").mkdir()
    return inc_dir


def _can_symlink() -> bool:
    """Check if the current runtime environment supports creating symlinks."""
    import tempfile
    try:
        with tempfile.TemporaryDirectory() as td:
            src = Path(td) / "src"
            src.mkdir()
            dst = Path(td) / "dst"
            dst.symlink_to(src, target_is_directory=True)
            return True
    except (OSError, NotImplementedError):
        return False


# ===========================================================================
# 1. Health Endpoint Tests
# ===========================================================================

def test_health_endpoint():
    """GET /health returns 200 OK with expected liveness fields."""
    app = create_app(repository=MockRepository())
    client = TestClient(app)

    response = client.get("/health")
    assert response.status_code == 200
    data = response.json()
    assert data == {"status": "ok", "service": "sentinel", "version": "2.0"}


# ===========================================================================
# 2. Validation & Edge Case Tests
# ===========================================================================

def test_create_investigation_empty_id(dummy_incidents_dir: Path):
    """POST /investigations with empty incident_id returns 400."""
    app = create_app(repository=MockRepository(), incidents_root=dummy_incidents_dir)
    client = TestClient(app)

    response = client.post("/investigations", json={"incident_id": ""})
    assert response.status_code == 400
    assert response.json()["error"] == "invalid_incident_id"


def test_create_investigation_invalid_chars(dummy_incidents_dir: Path):
    """POST /investigations with invalid characters returns 400."""
    app = create_app(repository=MockRepository(), incidents_root=dummy_incidents_dir)
    client = TestClient(app)

    response = client.post("/investigations", json={"incident_id": "inc@invalid!"})
    assert response.status_code == 400
    assert response.json()["error"] == "invalid_incident_id"


def test_create_investigation_traversal_dots(dummy_incidents_dir: Path):
    """POST /investigations with .. in incident_id returns 400."""
    app = create_app(repository=MockRepository(), incidents_root=dummy_incidents_dir)
    client = TestClient(app)

    response = client.post("/investigations", json={"incident_id": "../inc_01"})
    assert response.status_code == 400
    assert response.json()["error"] == "invalid_incident_id"


def test_create_investigation_traversal_slash(dummy_incidents_dir: Path):
    """POST /investigations with slash in incident_id returns 400."""
    app = create_app(repository=MockRepository(), incidents_root=dummy_incidents_dir)
    client = TestClient(app)

    response = client.post("/investigations", json={"incident_id": "sub/inc_01"})
    assert response.status_code == 400
    assert response.json()["error"] == "invalid_incident_id"


def test_create_investigation_traversal_colon(dummy_incidents_dir: Path):
    """POST /investigations with colon in incident_id returns 400."""
    app = create_app(repository=MockRepository(), incidents_root=dummy_incidents_dir)
    client = TestClient(app)

    response = client.post("/investigations", json={"incident_id": "C:inc_01"})
    assert response.status_code == 400
    assert response.json()["error"] == "invalid_incident_id"


def test_create_investigation_malformed_json(dummy_incidents_dir: Path):
    """POST /investigations with malformed JSON body returns 400 invalid_json."""
    app = create_app(repository=MockRepository(), incidents_root=dummy_incidents_dir)
    client = TestClient(app)

    response = client.post(
        "/investigations",
        content="not-valid-json",
        headers={"Content-Type": "application/json"},
    )
    assert response.status_code == 400
    assert response.json()["error"] == "invalid_json"


def test_create_investigation_non_dict_json(dummy_incidents_dir: Path):
    """POST /investigations with non-dict JSON body returns 400 invalid_json."""
    app = create_app(repository=MockRepository(), incidents_root=dummy_incidents_dir)
    client = TestClient(app)

    response = client.post(
        "/investigations",
        content="[1, 2, 3]",
        headers={"Content-Type": "application/json"},
    )
    assert response.status_code == 400
    assert response.json()["error"] == "invalid_json"


def test_create_investigation_incident_not_found(dummy_incidents_dir: Path):
    """POST /investigations for missing incident directory returns 404."""
    app = create_app(repository=MockRepository(), incidents_root=dummy_incidents_dir)
    client = TestClient(app)

    response = client.post("/investigations", json={"incident_id": "inc_99_missing"})
    assert response.status_code == 404
    assert response.json()["error"] == "incident_not_found"


# ===========================================================================
# 3. Successful Investigation & Safe Projection Tests
# ===========================================================================

def test_create_investigation_success(dummy_incidents_dir: Path):
    """POST /investigations runs orchestrator and returns allowlisted response."""
    repo = MockRepository()
    app = create_app(repository=repo, incidents_root=dummy_incidents_dir)
    client = TestClient(app)

    fake_state = InvestigationState(incident_id="inc_01_sample", status=IncidentStatus.COMPLETED)
    fake_state.stages["logs"] = StageResult(
        stage_name="logs",
        status=StageStatus.SUCCEEDED,
        llm_calls=1,
        prompt_tokens=100,
        completion_tokens=25,
        total_tokens=125,
        output={"raw_output": "logs_stage_output"},
    )
    fake_state.stages["fix_proposals"] = StageResult(
        stage_name="fix_proposals",
        status=StageStatus.SUCCEEDED,
        llm_calls=1,
        prompt_tokens=200,
        completion_tokens=50,
        total_tokens=250,
        output={"proposals": [{"proposal_id": "P1", "patch": "--- diff patch text ---"}]},
    )

    fake_result = {
        "incident_id": "inc_01_sample",
        "pipeline_status": "COMPLETED",
        "human_approval_notice": "AWAITING HUMAN APPROVAL — this fix has not been applied.",
        "llm_call_count": 2,
        "prompt_tokens": 300,
        "completion_tokens": 75,
        "total_tokens": 375,
        "stages": {
            "logs": {
                "status": "SUCCEEDED",
                "llm_calls": 1,
                "prompt_tokens": 100,
                "completion_tokens": 25,
                "total_tokens": 125,
                "output": {"raw_output": "logs_stage_output"},
            },
            "fix_proposals": {
                "status": "SUCCEEDED",
                "llm_calls": 1,
                "prompt_tokens": 200,
                "completion_tokens": 50,
                "total_tokens": 250,
                "output": {"proposals": [{"proposal_id": "P1", "patch": "--- diff patch text ---"}]},
            },
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
        instance.state = fake_state
        instance.investigate.return_value = fake_result

        response = client.post("/investigations", json={"incident_id": "inc_01_sample"})

    assert response.status_code == 200
    data = response.json()
    assert data["investigation_id"] == "inc_01_sample"
    assert data["pipeline_status"] == "COMPLETED"
    assert data["status"] == "COMPLETED"
    assert data["llm_call_count"] == 2
    assert data["prompt_tokens"] == 300
    assert data["completion_tokens"] == 75
    assert data["total_tokens"] == 375
    assert data["summary"]["proposals_generated"] == 1
    assert data["summary"]["proposals_rejected"] == 1

    # Security verification: output and patch fields MUST NOT be present
    assert "output" not in data["stages"]["logs"]
    assert "output" not in data["stages"]["fix_proposals"]
    assert "patch" not in str(data)
    assert "SECRET_GROUND_TRUTH" not in str(data)


# ===========================================================================
# 4. Concurrency & Duplicate Rejection Tests
# ===========================================================================

def test_create_investigation_conflict_persisted(dummy_incidents_dir: Path):
    """POST /investigations returns 409 if repository already has persisted state."""
    existing = InvestigationState(incident_id="inc_01_sample", status=IncidentStatus.COMPLETED)
    repo = MockRepository(states={"inc_01_sample": existing})
    app = create_app(repository=repo, incidents_root=dummy_incidents_dir)
    client = TestClient(app)

    response = client.post("/investigations", json={"incident_id": "inc_01_sample"})
    assert response.status_code == 409
    assert response.json()["error"] == "conflict"


def test_create_investigation_simultaneous_same_id(dummy_incidents_dir: Path):
    """Two simultaneous requests for the same ID in one process: one succeeds, one gets 409."""
    repo = MockRepository()
    app = create_app(repository=repo, incidents_root=dummy_incidents_dir)
    client = TestClient(app)

    results = []

    def mock_investigate(path):
        time.sleep(0.15)
        return {
            "incident_id": "inc_01_sample",
            "pipeline_status": "COMPLETED",
            "human_approval_notice": "AWAITING HUMAN APPROVAL",
            "llm_call_count": 0,
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
            "stages": {},
            "summary": {},
            "error": None,
        }

    with patch("api.routes.IncidentOrchestrator") as MockOrch:
        instance = MockOrch.return_value
        instance.state = InvestigationState(incident_id="inc_01_sample", status=IncidentStatus.COMPLETED)
        instance.investigate.side_effect = mock_investigate

        def worker1():
            res = client.post("/investigations", json={"incident_id": "inc_01_sample"})
            results.append(res.status_code)

        def worker2():
            time.sleep(0.02)  # ensure worker1 acquires lock first
            res = client.post("/investigations", json={"incident_id": "inc_01_sample"})
            results.append(res.status_code)

        t1 = threading.Thread(target=worker1)
        t2 = threading.Thread(target=worker2)
        t1.start()
        t2.start()
        t1.join()
        t2.join()

    assert sorted(results) == [200, 409]


# ===========================================================================
# 5. Resilience & Error Cleanup Tests
# ===========================================================================

def test_active_registry_cleanup_on_orchestrator_error(dummy_incidents_dir: Path):
    """Active registry safely cleans up when orchestrator raises an exception."""
    repo = MockRepository()
    app = create_app(repository=repo, incidents_root=dummy_incidents_dir)
    client = TestClient(app, raise_server_exceptions=False)

    with patch("api.routes.IncidentOrchestrator") as MockOrch:
        instance = MockOrch.return_value
        instance.investigate.side_effect = RuntimeError("Fatal pipeline crash")

        response = client.post("/investigations", json={"incident_id": "inc_01_sample"})
        assert response.status_code == 500

    # Ensure registry was cleaned up in finally block
    with _ACTIVE_LOCK:
        assert "inc_01_sample" not in _ACTIVE_INVESTIGATIONS


def test_active_registry_cleanup_on_persistence_error(dummy_incidents_dir: Path):
    """Active registry safely cleans up when persistence raises an exception."""
    repo = MockRepository()
    app = create_app(repository=repo, incidents_root=dummy_incidents_dir)
    client = TestClient(app, raise_server_exceptions=False)

    with patch("api.routes.IncidentOrchestrator") as MockOrch:
        instance = MockOrch.return_value
        instance.investigate.side_effect = PersistenceError("Disk full")

        response = client.post("/investigations", json={"incident_id": "inc_01_sample"})
        assert response.status_code == 503
        assert response.json()["error"] == "persistence_unavailable"

    with _ACTIVE_LOCK:
        assert "inc_01_sample" not in _ACTIVE_INVESTIGATIONS


def test_subsequent_post_after_failed_running_state_returns_409(dummy_incidents_dir: Path):
    """Persisted RUNNING state left after an unhandled failure causes subsequent POST to return 409."""
    repo = MockRepository()
    app = create_app(repository=repo, incidents_root=dummy_incidents_dir)
    client = TestClient(app, raise_server_exceptions=False)

    # Simulate orchestrator persisting initial RUNNING state then crashing
    def crash_investigate(path):
        state = InvestigationState(incident_id="inc_01_sample", status=IncidentStatus.RUNNING)
        repo.save(state)
        raise RuntimeError("Orchestrator crash after initial persist")

    with patch("api.routes.IncidentOrchestrator") as MockOrch:
        instance = MockOrch.return_value
        instance.investigate.side_effect = crash_investigate
        res1 = client.post("/investigations", json={"incident_id": "inc_01_sample"})
        assert res1.status_code == 500

    # Active registry is empty
    with _ACTIVE_LOCK:
        assert "inc_01_sample" not in _ACTIVE_INVESTIGATIONS

    # Subsequent POST finds persisted RUNNING state and returns 409
    res2 = client.post("/investigations", json={"incident_id": "inc_01_sample"})
    assert res2.status_code == 409
    assert res2.json()["error"] == "conflict"


def test_create_investigation_concurrency_error_mapping(dummy_incidents_dir: Path):
    """ConcurrencyError from persistence maps to 409 Conflict."""
    repo = MockRepository()
    app = create_app(repository=repo, incidents_root=dummy_incidents_dir)
    client = TestClient(app, raise_server_exceptions=False)

    with patch("api.routes.IncidentOrchestrator") as MockOrch:
        instance = MockOrch.return_value
        instance.investigate.side_effect = ConcurrencyError("OCC update conflict")

        response = client.post("/investigations", json={"incident_id": "inc_01_sample"})
        assert response.status_code == 409
        assert response.json()["error"] == "conflict"


# ===========================================================================
# 6. Symlink Security Tests
# ===========================================================================

@pytest.mark.skipif(not _can_symlink(), reason="Symlinks not supported in current environment/permissions")
def test_create_investigation_rejects_symlink_inside_root(dummy_incidents_dir: Path):
    """Symlinked incident directory inside incidents root is rejected with 400."""
    sym_dir = dummy_incidents_dir / "inc_sym_internal"
    target_dir = dummy_incidents_dir / "inc_01_sample"
    sym_dir.symlink_to(target_dir, target_is_directory=True)

    app = create_app(repository=MockRepository(), incidents_root=dummy_incidents_dir)
    client = TestClient(app)

    response = client.post("/investigations", json={"incident_id": "inc_sym_internal"})
    assert response.status_code == 400
    assert response.json()["error"] == "invalid_incident_id"


@pytest.mark.skipif(not _can_symlink(), reason="Symlinks not supported in current environment/permissions")
def test_create_investigation_rejects_symlink_escaping_root(tmp_path: Path, dummy_incidents_dir: Path):
    """Symlinked incident directory pointing outside incidents root is rejected with 400."""
    outside_dir = tmp_path / "outside_incident"
    outside_dir.mkdir()
    sym_dir = dummy_incidents_dir / "inc_sym_escape"
    sym_dir.symlink_to(outside_dir, target_is_directory=True)

    app = create_app(repository=MockRepository(), incidents_root=dummy_incidents_dir)
    client = TestClient(app)

    response = client.post("/investigations", json={"incident_id": "inc_sym_escape"})
    assert response.status_code == 400
    assert response.json()["error"] == "invalid_incident_id"


# ===========================================================================
# 7. GET /investigations/{id} and GET /investigations Tests
# ===========================================================================

def test_get_investigation_success():
    """GET /investigations/{id} returns persisted state with OCC version."""
    state = InvestigationState(incident_id="inc_01_test", status=IncidentStatus.COMPLETED, version=7)
    state.stages["logs"] = StageResult(
        stage_name="logs",
        status=StageStatus.SUCCEEDED,
        llm_calls=1,
        prompt_tokens=100,
        completion_tokens=20,
        total_tokens=120,
        output={"raw_output": "sensitive"},
    )
    repo = MockRepository(states={"inc_01_test": state})
    app = create_app(repository=repo)
    client = TestClient(app)

    response = client.get("/investigations/inc_01_test")
    assert response.status_code == 200
    data = response.json()
    assert data["investigation_id"] == "inc_01_test"
    assert data["status"] == "COMPLETED"
    assert data["version"] == 7
    assert data["stages"]["logs"]["status"] == "SUCCEEDED"
    assert data["stages"]["logs"]["llm_calls"] == 1
    # Security: stage output MUST be omitted
    assert "output" not in data["stages"]["logs"]


def test_get_investigation_not_found():
    """GET /investigations/{id} returns 404 for non-existent ID."""
    app = create_app(repository=MockRepository())
    client = TestClient(app)

    response = client.get("/investigations/inc_99_missing")
    assert response.status_code == 404
    assert response.json()["error"] == "investigation_not_found"


def test_get_investigation_invalid_id():
    """GET /investigations/{id} returns 400 for malformed ID in path."""
    app = create_app(repository=MockRepository())
    client = TestClient(app)

    response = client.get("/investigations/invalid@id!")
    assert response.status_code == 400
    assert response.json()["error"] == "invalid_investigation_id"


def test_list_investigations_success():
    """GET /investigations returns sorted list of all investigation IDs."""
    s1 = InvestigationState(incident_id="inc_02_b", status=IncidentStatus.COMPLETED)
    s2 = InvestigationState(incident_id="inc_01_a", status=IncidentStatus.RUNNING)
    repo = MockRepository(states={"inc_02_b": s1, "inc_01_a": s2})
    app = create_app(repository=repo)
    client = TestClient(app)

    response = client.get("/investigations")
    assert response.status_code == 200
    data = response.json()
    assert data == {"investigations": ["inc_01_a", "inc_02_b"]}


# ===========================================================================
# 8. Error Sanitization Security Tests
# ===========================================================================

def test_security_error_sanitization(dummy_incidents_dir: Path):
    """Internal server errors sanitize file system paths and stack traces."""
    app = create_app(repository=MockRepository(), incidents_root=dummy_incidents_dir)
    client = TestClient(app, raise_server_exceptions=False)

    with patch("api.routes.IncidentOrchestrator") as MockOrch:
        instance = MockOrch.return_value
        instance.investigate.side_effect = RuntimeError("Fatal error in D:\\Assignemt\\secret\\code.py line 42")
        response = client.post("/investigations", json={"incident_id": "inc_01_sample"})
        assert response.status_code == 500
        data = response.json()
        assert data["error"] == "internal_error"
        assert "D:\\Assignemt" not in data["detail"]
        assert "secret" not in data["detail"]
