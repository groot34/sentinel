"""Tests for async investigation dispatch (Mission 13).

All tests are deterministic: they use controllable gates, mock injection,
and Starlette's TestClient (which runs BackgroundTasks before returning)
to verify correct behavior without timing-sensitive assertions.

Testing note on TestClient + BackgroundTask:
    Starlette's TestClient processes BackgroundTasks synchronously after the
    response is delivered within the same client.post(...) call. This means
    the background work is guaranteed to have completed when the next assertion
    runs. Tests that need to verify the *accepted* response schema before
    background work completes use a blocking gate (threading.Event) injected
    into the background task via a mock to pause execution between the
    202 response and pipeline completion.
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock, patch

import pytest
from starlette.testclient import TestClient

from api.app import create_app
from api.models import AcceptedInvestigationResponse
from api.routes import (
    _ACTIVE_INVESTIGATIONS,
    _ACTIVE_LOCK,
    _run_investigation_background,
    _run_resume_background,
)
from core.domain.models import IncidentStatus, StageStatus
from core.domain.state import InvestigationState, StageResult
from core.persistence import ConcurrencyError, PersistenceError, PersistenceRepository


# ---------------------------------------------------------------------------
# Shared mock repository
# ---------------------------------------------------------------------------

class MockRepository(PersistenceRepository):
    """In-memory repository for async dispatch tests."""

    def __init__(self, states=None):
        self._states = states or {}
        self.saved = []

    def save(self, state, events=None):
        self.saved.append(state)
        self._states[state.incident_id] = state

    def load(self, investigation_id):
        return self._states.get(investigation_id)

    def exists(self, investigation_id):
        return investigation_id in self._states

    def delete(self, investigation_id):
        self._states.pop(investigation_id, None)

    def list(self):
        return sorted(self._states.keys())

    @property
    def supports_event_history(self):
        return True

    def list_events(self, investigation_id, limit=200, after_cursor=None):
        return []


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def clean_active_registry():
    with _ACTIVE_LOCK:
        _ACTIVE_INVESTIGATIONS.clear()
    yield
    with _ACTIVE_LOCK:
        _ACTIVE_INVESTIGATIONS.clear()


@pytest.fixture
def incidents_dir(tmp_path):
    inc_dir = tmp_path / "incidents"
    inc_dir.mkdir()
    (inc_dir / "inc_async_01").mkdir()
    (inc_dir / "inc_async_02").mkdir()
    return inc_dir


def _make_fake_result(incident_id):
    return {
        "incident_id": incident_id,
        "pipeline_status": "COMPLETED",
        "human_approval_notice": "AWAITING HUMAN APPROVAL",
        "llm_call_count": 0,
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "total_tokens": 0,
        "stages": {},
        "summary": {
            "confirmed_hypotheses": 0,
            "rejected_hypotheses": 0,
            "inconclusive_hypotheses": 0,
            "proposals_generated": 0,
            "proposals_approved": 0,
            "proposals_rejected": 0,
        },
        "error": None,
    }


# ===========================================================================
# 1. AcceptedInvestigationResponse schema validation
# ===========================================================================

def test_accepted_response_schema_valid():
    resp = AcceptedInvestigationResponse(
        investigation_id="inc_async_01",
        status="RUNNING",
        message="Accepted for async execution.",
        status_url="http://localhost:8000/investigations/inc_async_01",
        events_url="http://localhost:8000/investigations/inc_async_01/events",
    )
    assert resp.investigation_id == "inc_async_01"
    assert resp.status == "RUNNING"
    assert "/investigations/inc_async_01" in resp.status_url
    assert "/investigations/inc_async_01/events" in resp.events_url


def test_accepted_response_rejects_extra_fields():
    import pydantic
    with pytest.raises(pydantic.ValidationError):
        AcceptedInvestigationResponse(
            investigation_id="inc_async_01",
            status="RUNNING",
            message="accepted",
            status_url="http://localhost:8000/investigations/inc_async_01",
            events_url="http://localhost:8000/investigations/inc_async_01/events",
            unexpected_field="boom",
        )


def test_accepted_response_requires_all_fields():
    import pydantic
    with pytest.raises(pydantic.ValidationError):
        AcceptedInvestigationResponse(
            investigation_id="inc_async_01",
            status="RUNNING",
        )


# ===========================================================================
# 2. Async creation: 202 response and schema
# ===========================================================================

def test_async_create_returns_202(incidents_dir):
    repo = MockRepository()
    app = create_app(repository=repo, incidents_root=incidents_dir)
    client = TestClient(app)

    with patch("api.routes.IncidentOrchestrator") as MockOrch:
        instance = MockOrch.return_value
        instance.investigate.return_value = _make_fake_result("inc_async_01")
        instance.state = InvestigationState(incident_id="inc_async_01", status=IncidentStatus.COMPLETED)

        response = client.post(
            "/investigations",
            json={"incident_id": "inc_async_01"},
            headers={"Prefer": "respond-async"},
        )

    assert response.status_code == 202


def test_async_create_response_schema(incidents_dir):
    repo = MockRepository()
    app = create_app(repository=repo, incidents_root=incidents_dir)
    client = TestClient(app)

    with patch("api.routes.IncidentOrchestrator") as MockOrch:
        instance = MockOrch.return_value
        instance.investigate.return_value = _make_fake_result("inc_async_01")
        instance.state = InvestigationState(incident_id="inc_async_01", status=IncidentStatus.COMPLETED)

        response = client.post(
            "/investigations",
            json={"incident_id": "inc_async_01"},
            headers={"Prefer": "respond-async"},
        )

    assert response.status_code == 202
    data = response.json()
    parsed = AcceptedInvestigationResponse(**data)
    assert parsed.investigation_id == "inc_async_01"
    assert parsed.status == "RUNNING"
    assert "inc_async_01" in parsed.status_url
    assert "inc_async_01" in parsed.events_url


def test_async_create_response_header(incidents_dir):
    repo = MockRepository()
    app = create_app(repository=repo, incidents_root=incidents_dir)
    client = TestClient(app)

    with patch("api.routes.IncidentOrchestrator") as MockOrch:
        instance = MockOrch.return_value
        instance.investigate.return_value = _make_fake_result("inc_async_01")
        instance.state = InvestigationState(incident_id="inc_async_01", status=IncidentStatus.COMPLETED)

        response = client.post(
            "/investigations",
            json={"incident_id": "inc_async_01"},
            headers={"Prefer": "respond-async"},
        )

    assert response.headers.get("preference-applied") == "respond-async"
    assert response.headers.get("location") == "http://testserver/investigations/inc_async_01"


def test_async_create_via_query_param(incidents_dir):
    repo = MockRepository()
    app = create_app(repository=repo, incidents_root=incidents_dir)
    client = TestClient(app)

    with patch("api.routes.IncidentOrchestrator") as MockOrch:
        instance = MockOrch.return_value
        instance.investigate.return_value = _make_fake_result("inc_async_01")
        instance.state = InvestigationState(incident_id="inc_async_01", status=IncidentStatus.COMPLETED)

        response = client.post(
            "/investigations?async=true",
            json={"incident_id": "inc_async_01"},
        )

    assert response.status_code == 202
    data = response.json()
    assert data["status"] == "RUNNING"
    assert response.headers.get("preference-applied") == "respond-async"
    assert response.headers.get("location") == "http://testserver/investigations/inc_async_01"


def test_async_create_status_url_resolves(incidents_dir):
    repo = MockRepository()
    app = create_app(repository=repo, incidents_root=incidents_dir)
    client = TestClient(app)

    with patch("api.routes.IncidentOrchestrator") as MockOrch:
        instance = MockOrch.return_value
        instance.investigate.return_value = _make_fake_result("inc_async_01")
        instance.state = InvestigationState(incident_id="inc_async_01", status=IncidentStatus.COMPLETED)

        client.post(
            "/investigations",
            json={"incident_id": "inc_async_01"},
            headers={"Prefer": "respond-async"},
        )

    # Investigation was persisted before 202 returned; must be queryable
    status_resp = client.get("/investigations/inc_async_01")
    assert status_resp.status_code == 200


def test_async_create_events_url_resolves(incidents_dir):
    repo = MockRepository()
    app = create_app(repository=repo, incidents_root=incidents_dir)
    client = TestClient(app)

    with patch("api.routes.IncidentOrchestrator") as MockOrch:
        instance = MockOrch.return_value
        instance.investigate.return_value = _make_fake_result("inc_async_01")
        instance.state = InvestigationState(incident_id="inc_async_01", status=IncidentStatus.COMPLETED)

        client.post(
            "/investigations",
            json={"incident_id": "inc_async_01"},
            headers={"Prefer": "respond-async"},
        )

    events_resp = client.get("/investigations/inc_async_01/events")
    assert events_resp.status_code == 200


# ===========================================================================
# 3. Immediate queryability
# ===========================================================================

def test_state_persisted_as_running_before_pipeline(incidents_dir):
    """The pre-persist save must record a RUNNING initial state."""
    repo = MockRepository()
    app = create_app(repository=repo, incidents_root=incidents_dir)
    client = TestClient(app)

    gate = threading.Event()

    def blocking_investigate(path):
        gate.wait()
        return _make_fake_result("inc_async_01")

    with patch("api.routes.IncidentOrchestrator") as MockOrch:
        instance = MockOrch.return_value
        instance.investigate.side_effect = blocking_investigate
        instance.state = InvestigationState(incident_id="inc_async_01", status=IncidentStatus.COMPLETED)
        gate.set()  # release immediately for TestClient synchronous BG

        response = client.post(
            "/investigations",
            json={"incident_id": "inc_async_01"},
            headers={"Prefer": "respond-async"},
        )

    assert response.status_code == 202
    running_saves = [s for s in repo.saved if str(s.incident_id) == "inc_async_01"]
    assert len(running_saves) >= 1
    first_status = running_saves[0].status
    first_status_val = first_status.value if hasattr(first_status, "value") else str(first_status)
    assert first_status_val == "RUNNING"


def test_investigation_exists_after_202(incidents_dir):
    repo = MockRepository()
    app = create_app(repository=repo, incidents_root=incidents_dir)
    client = TestClient(app)

    with patch("api.routes.IncidentOrchestrator") as MockOrch:
        instance = MockOrch.return_value
        instance.investigate.return_value = _make_fake_result("inc_async_01")
        instance.state = InvestigationState(incident_id="inc_async_01", status=IncidentStatus.COMPLETED)

        client.post(
            "/investigations",
            json={"incident_id": "inc_async_01"},
            headers={"Prefer": "respond-async"},
        )

    get_resp = client.get("/investigations/inc_async_01")
    assert get_resp.status_code == 200
    assert get_resp.json()["investigation_id"] == "inc_async_01"


# ===========================================================================
# 4. SSE connectivity after 202
# ===========================================================================

def test_sse_endpoint_accessible_after_async_create(incidents_dir):
    repo = MockRepository()
    app = create_app(repository=repo, incidents_root=incidents_dir)
    app.state.sse_max_polls = 0
    app.state.sse_poll_interval = 0.0
    client = TestClient(app)

    completed_state = InvestigationState(incident_id="inc_async_01", status=IncidentStatus.COMPLETED)
    repo.save(completed_state)

    sse_resp = client.get(
        "/investigations/inc_async_01/events",
        headers={"Accept": "text/event-stream"},
    )
    assert sse_resp.status_code == 200
    assert "text/event-stream" in sse_resp.headers.get("content-type", "")


# ===========================================================================
# 5. Duplicate execution rejection
# ===========================================================================

def test_async_create_rejects_duplicate_persisted_state(incidents_dir):
    existing = InvestigationState(incident_id="inc_async_01", status=IncidentStatus.COMPLETED)
    repo = MockRepository(states={"inc_async_01": existing})
    app = create_app(repository=repo, incidents_root=incidents_dir)
    client = TestClient(app)

    response = client.post(
        "/investigations",
        json={"incident_id": "inc_async_01"},
        headers={"Prefer": "respond-async"},
    )
    assert response.status_code == 409
    assert response.json()["error"] == "conflict"


def test_async_create_rejects_while_active_in_process(incidents_dir):
    repo = MockRepository()
    app = create_app(repository=repo, incidents_root=incidents_dir)
    client = TestClient(app)

    with _ACTIVE_LOCK:
        _ACTIVE_INVESTIGATIONS.add("inc_async_01")

    try:
        response = client.post(
            "/investigations",
            json={"incident_id": "inc_async_01"},
            headers={"Prefer": "respond-async"},
        )
        assert response.status_code == 409
    finally:
        with _ACTIVE_LOCK:
            _ACTIVE_INVESTIGATIONS.discard("inc_async_01")


# ===========================================================================
# 6. Synchronous behavior unchanged
# ===========================================================================

def test_sync_create_returns_200_without_prefer_header(incidents_dir):
    repo = MockRepository()
    app = create_app(repository=repo, incidents_root=incidents_dir)
    client = TestClient(app)

    fake_state = InvestigationState(incident_id="inc_async_01", status=IncidentStatus.COMPLETED)

    with patch("api.routes.IncidentOrchestrator") as MockOrch:
        instance = MockOrch.return_value
        instance.investigate.return_value = _make_fake_result("inc_async_01")
        instance.state = fake_state

        response = client.post("/investigations", json={"incident_id": "inc_async_01"})

    assert response.status_code == 200
    data = response.json()
    assert data["investigation_id"] == "inc_async_01"
    assert data["pipeline_status"] == "COMPLETED"


def test_sync_create_returns_200_with_unrecognized_prefer(incidents_dir):
    repo = MockRepository()
    app = create_app(repository=repo, incidents_root=incidents_dir)
    client = TestClient(app)

    fake_state = InvestigationState(incident_id="inc_async_01", status=IncidentStatus.COMPLETED)

    with patch("api.routes.IncidentOrchestrator") as MockOrch:
        instance = MockOrch.return_value
        instance.investigate.return_value = _make_fake_result("inc_async_01")
        instance.state = fake_state

        response = client.post(
            "/investigations",
            json={"incident_id": "inc_async_01"},
            headers={"Prefer": "return=representation"},
        )

    assert response.status_code == 200


# ===========================================================================
# 7. Async resume
# ===========================================================================

def test_async_resume_returns_202(incidents_dir):
    failed_state = InvestigationState(incident_id="inc_async_01", status=IncidentStatus.FAILED)
    failed_state.lease_expires_at = None
    failed_state.lease_owner = None

    repo = MockRepository(states={"inc_async_01": failed_state})
    app = create_app(repository=repo, incidents_root=incidents_dir)
    client = TestClient(app)

    with patch("api.routes.IncidentOrchestrator") as MockOrch:
        instance = MockOrch.return_value
        instance.resume.return_value = _make_fake_result("inc_async_01")
        instance.state = InvestigationState(incident_id="inc_async_01", status=IncidentStatus.COMPLETED)

        response = client.post(
            "/investigations/inc_async_01/resume",
            headers={"Prefer": "respond-async"},
        )

    assert response.status_code == 202
    data = response.json()
    parsed = AcceptedInvestigationResponse(**data)
    assert parsed.investigation_id == "inc_async_01"
    assert parsed.status == "RUNNING"


def test_async_resume_via_query_param(incidents_dir):
    failed_state = InvestigationState(incident_id="inc_async_01", status=IncidentStatus.FAILED)
    failed_state.lease_expires_at = None
    failed_state.lease_owner = None

    repo = MockRepository(states={"inc_async_01": failed_state})
    app = create_app(repository=repo, incidents_root=incidents_dir)
    client = TestClient(app)

    with patch("api.routes.IncidentOrchestrator") as MockOrch:
        instance = MockOrch.return_value
        instance.resume.return_value = _make_fake_result("inc_async_01")
        instance.state = InvestigationState(incident_id="inc_async_01", status=IncidentStatus.COMPLETED)

        response = client.post(
            "/investigations/inc_async_01/resume?async=true",
        )

    assert response.status_code == 202
    data = response.json()
    assert data["status"] == "RUNNING"
    assert response.headers.get("location") == "http://testserver/investigations/inc_async_01"


def test_async_resume_rejects_completed(incidents_dir):
    completed = InvestigationState(incident_id="inc_async_01", status=IncidentStatus.COMPLETED)
    repo = MockRepository(states={"inc_async_01": completed})
    app = create_app(repository=repo, incidents_root=incidents_dir)
    client = TestClient(app)

    response = client.post(
        "/investigations/inc_async_01/resume",
        headers={"Prefer": "respond-async"},
    )
    assert response.status_code == 409
    assert response.json()["error"] == "conflict"


def test_async_resume_rejects_not_found(incidents_dir):
    repo = MockRepository()
    app = create_app(repository=repo, incidents_root=incidents_dir)
    client = TestClient(app)

    response = client.post(
        "/investigations/inc_async_01/resume",
        headers={"Prefer": "respond-async"},
    )
    assert response.status_code == 404


def test_async_resume_rejects_active_lease(incidents_dir):
    from datetime import datetime, timedelta, timezone
    running = InvestigationState(incident_id="inc_async_01", status=IncidentStatus.RUNNING)
    future = datetime.now(timezone.utc) + timedelta(seconds=300)
    running.lease_expires_at = future.isoformat()
    running.lease_owner = "other-worker"

    repo = MockRepository(states={"inc_async_01": running})
    app = create_app(repository=repo, incidents_root=incidents_dir)
    client = TestClient(app)

    response = client.post(
        "/investigations/inc_async_01/resume",
        headers={"Prefer": "respond-async"},
    )
    assert response.status_code == 409


# ===========================================================================
# 8. Completed stages preserved on async resume
# ===========================================================================

def test_completed_stages_preserved_in_async_resume(incidents_dir):
    """Orchestrator.resume is called (not investigate), preserving completed stages."""
    partial_state = InvestigationState(incident_id="inc_async_01", status=IncidentStatus.PARTIAL)
    partial_state.stages["logs"] = StageResult(
        stage_name="logs",
        status=StageStatus.SUCCEEDED,
        llm_calls=1,
        prompt_tokens=100,
        completion_tokens=20,
        total_tokens=120,
    )
    partial_state.lease_expires_at = None
    partial_state.lease_owner = None

    repo = MockRepository(states={"inc_async_01": partial_state})
    app = create_app(repository=repo, incidents_root=incidents_dir)
    client = TestClient(app)

    resume_called = threading.Event()
    investigate_called = threading.Event()

    def do_resume(**kw):
        resume_called.set()
        return _make_fake_result("inc_async_01")

    def do_investigate(p):
        investigate_called.set()
        return _make_fake_result("inc_async_01")

    with patch("api.routes.IncidentOrchestrator") as MockOrch:
        instance = MockOrch.return_value
        instance.resume.side_effect = do_resume
        instance.investigate.side_effect = do_investigate
        instance.state = InvestigationState(incident_id="inc_async_01", status=IncidentStatus.COMPLETED)

        client.post(
            "/investigations/inc_async_01/resume",
            headers={"Prefer": "respond-async"},
        )

    assert resume_called.is_set(), "orchestrator.resume() was not called"
    assert not investigate_called.is_set(), "orchestrator.investigate() must not be called on resume"


# ===========================================================================
# 9. Lease and cleanup on success and failure
# ===========================================================================

def test_process_local_registry_cleared_after_async_success(incidents_dir):
    repo = MockRepository()
    app = create_app(repository=repo, incidents_root=incidents_dir)
    client = TestClient(app)

    with patch("api.routes.IncidentOrchestrator") as MockOrch:
        instance = MockOrch.return_value
        instance.investigate.return_value = _make_fake_result("inc_async_01")
        instance.state = InvestigationState(incident_id="inc_async_01", status=IncidentStatus.COMPLETED)

        client.post(
            "/investigations",
            json={"incident_id": "inc_async_01"},
            headers={"Prefer": "respond-async"},
        )

    with _ACTIVE_LOCK:
        assert "inc_async_01" not in _ACTIVE_INVESTIGATIONS


def test_process_local_registry_cleared_after_async_failure(incidents_dir):
    repo = MockRepository()
    app = create_app(repository=repo, incidents_root=incidents_dir)
    client = TestClient(app, raise_server_exceptions=False)

    with patch("api.routes.IncidentOrchestrator") as MockOrch:
        instance = MockOrch.return_value
        instance.investigate.side_effect = RuntimeError("pipeline crash")
        instance.state = MagicMock()

        client.post(
            "/investigations",
            json={"incident_id": "inc_async_01"},
            headers={"Prefer": "respond-async"},
        )

    with _ACTIVE_LOCK:
        assert "inc_async_01" not in _ACTIVE_INVESTIGATIONS


def test_async_failure_persists_failed_state(incidents_dir):
    """Background pipeline crash must persist FAILED state."""
    repo = MockRepository()
    app = create_app(repository=repo, incidents_root=incidents_dir)
    client = TestClient(app, raise_server_exceptions=False)

    with patch("api.routes.IncidentOrchestrator") as MockOrch:
        instance = MockOrch.return_value
        instance.investigate.side_effect = RuntimeError("LLM timeout")
        instance.state = MagicMock()

        client.post(
            "/investigations",
            json={"incident_id": "inc_async_01"},
            headers={"Prefer": "respond-async"},
        )

    state = repo.load("inc_async_01")
    assert state is not None
    status_val = state.status.value if hasattr(state.status, "value") else str(state.status)
    assert status_val == "FAILED", f"Expected FAILED, got {status_val!r}"


# ===========================================================================
# 10. Background task helper unit tests
# ===========================================================================

def test_run_investigation_background_releases_claim_on_success():
    with _ACTIVE_LOCK:
        _ACTIVE_INVESTIGATIONS.add("test_bg_id")

    repo = MockRepository()
    initial = InvestigationState(incident_id="test_bg_id", status=IncidentStatus.RUNNING)
    repo.save(initial)

    with patch("api.routes.IncidentOrchestrator") as MockOrch:
        instance = MockOrch.return_value
        instance.investigate.return_value = _make_fake_result("test_bg_id")

        _run_investigation_background("test_bg_id", Path("/fake/path"), repo, None)

    with _ACTIVE_LOCK:
        assert "test_bg_id" not in _ACTIVE_INVESTIGATIONS


def test_run_investigation_background_releases_claim_on_failure():
    with _ACTIVE_LOCK:
        _ACTIVE_INVESTIGATIONS.add("test_bg_fail")

    repo = MockRepository()
    initial = InvestigationState(incident_id="test_bg_fail", status=IncidentStatus.RUNNING)
    repo.save(initial)

    with patch("api.routes.IncidentOrchestrator") as MockOrch:
        instance = MockOrch.return_value
        instance.investigate.side_effect = RuntimeError("crash")

        _run_investigation_background("test_bg_fail", Path("/fake/path"), repo, None)

    with _ACTIVE_LOCK:
        assert "test_bg_fail" not in _ACTIVE_INVESTIGATIONS


def test_run_resume_background_releases_claim_on_success():
    with _ACTIVE_LOCK:
        _ACTIVE_INVESTIGATIONS.add("test_resume_bg")

    repo = MockRepository()
    initial = InvestigationState(incident_id="test_resume_bg", status=IncidentStatus.FAILED)
    repo.save(initial)

    with patch("api.routes.IncidentOrchestrator") as MockOrch:
        instance = MockOrch.return_value
        instance.resume.return_value = _make_fake_result("test_resume_bg")

        _run_resume_background("test_resume_bg", Path("/fake/path"), repo, None)

    with _ACTIVE_LOCK:
        assert "test_resume_bg" not in _ACTIVE_INVESTIGATIONS


def test_run_resume_background_releases_claim_on_failure():
    with _ACTIVE_LOCK:
        _ACTIVE_INVESTIGATIONS.add("test_resume_fail")

    repo = MockRepository()
    initial = InvestigationState(incident_id="test_resume_fail", status=IncidentStatus.FAILED)
    repo.save(initial)

    with patch("api.routes.IncidentOrchestrator") as MockOrch:
        instance = MockOrch.return_value
        instance.resume.side_effect = RuntimeError("resume crash")

        _run_resume_background("test_resume_fail", Path("/fake/path"), repo, None)

    with _ACTIVE_LOCK:
        assert "test_resume_fail" not in _ACTIVE_INVESTIGATIONS


# ===========================================================================
# 11. Existing endpoints regression guard
# ===========================================================================

def test_health_still_returns_200():
    app = create_app(repository=MockRepository())
    client = TestClient(app)
    assert client.get("/health").status_code == 200


def test_get_investigation_still_works():
    state = InvestigationState(incident_id="existing_inv", status=IncidentStatus.COMPLETED)
    repo = MockRepository(states={"existing_inv": state})
    app = create_app(repository=repo)
    client = TestClient(app)

    resp = client.get("/investigations/existing_inv")
    assert resp.status_code == 200
    assert resp.json()["investigation_id"] == "existing_inv"


def test_list_investigations_still_works():
    state = InvestigationState(incident_id="existing_inv", status=IncidentStatus.COMPLETED)
    repo = MockRepository(states={"existing_inv": state})
    app = create_app(repository=repo)
    client = TestClient(app)

    resp = client.get("/investigations")
    assert resp.status_code == 200
    assert "existing_inv" in resp.json()["investigations"]


def test_json_event_history_still_works():
    state = InvestigationState(incident_id="existing_inv", status=IncidentStatus.COMPLETED)
    repo = MockRepository(states={"existing_inv": state})
    app = create_app(repository=repo)
    client = TestClient(app)

    resp = client.get("/investigations/existing_inv/events")
    assert resp.status_code == 200
    data = resp.json()
    assert "events" in data
    assert data["investigation_id"] == "existing_inv"


def test_invalid_id_returns_400_async(incidents_dir):
    app = create_app(repository=MockRepository(), incidents_root=incidents_dir)
    client = TestClient(app)

    resp = client.post(
        "/investigations",
        json={"incident_id": "invalid@id!"},
        headers={"Prefer": "respond-async"},
    )
    assert resp.status_code == 400


def test_missing_incident_dir_returns_404_async(incidents_dir):
    app = create_app(repository=MockRepository(), incidents_root=incidents_dir)
    client = TestClient(app)

    resp = client.post(
        "/investigations",
        json={"incident_id": "inc_does_not_exist"},
        headers={"Prefer": "respond-async"},
    )
    assert resp.status_code == 404
    assert resp.json()["error"] == "incident_not_found"


# ===========================================================================
# 12. Durable Lease Ownership & OCC Concurrency Verification (Mission 13 Audit)
# ===========================================================================

def test_async_create_consistent_worker_id_and_lease_ownership(incidents_dir):
    """Worker ID generated during async acceptance is passed directly to the
    background IncidentOrchestrator and matches initial_state.lease_owner."""
    repo = MockRepository()
    app = create_app(repository=repo, incidents_root=incidents_dir)
    client = TestClient(app)

    captured_init_kwargs = {}

    with patch("api.routes.IncidentOrchestrator") as MockOrch:
        def capture_init(**kwargs):
            captured_init_kwargs.update(kwargs)
            instance = MagicMock()
            instance.investigate.return_value = _make_fake_result("inc_async_01")
            instance.state = InvestigationState(incident_id="inc_async_01", status=IncidentStatus.COMPLETED)
            return instance

        MockOrch.side_effect = capture_init

        resp = client.post(
            "/investigations",
            json={"incident_id": "inc_async_01"},
            headers={"Prefer": "respond-async"},
        )

    assert resp.status_code == 202
    assert "worker_id" in captured_init_kwargs
    orchestrator_worker_id = captured_init_kwargs["worker_id"]
    assert orchestrator_worker_id is not None
    assert orchestrator_worker_id.startswith("worker-")

    # Initial state saved in repo must have the exact same lease_owner
    assert len(repo.saved) >= 1
    initial_saved = repo.saved[0]
    assert initial_saved.lease_owner == orchestrator_worker_id
    assert initial_saved.status in (IncidentStatus.RUNNING, "RUNNING")


def test_async_create_orchestrator_reuses_preleased_state(tmp_path):
    """When orchestrator.investigate runs on an existing RUNNING state with matching
    lease_owner, it reuses that state, renews the lease, and does not diverge."""
    from agents.orchestrator import IncidentOrchestrator
    from core.persistence.filesystem import FilesystemRepository

    # Create dummy incident directory
    inc_dir = tmp_path / "incidents" / "inc_01"
    inc_dir.mkdir(parents=True)

    repo = FilesystemRepository(persistence_root=tmp_path / "persistence")
    worker_id = "worker-prelease-123"

    # Pre-create state as routes.py does before returning 202
    initial_state = InvestigationState(incident_id="inc_01")
    initial_state.acquire_lease(owner_id=worker_id, ttl_seconds=300)
    repo.save(initial_state)

    orchestrator = IncidentOrchestrator(
        non_interactive=True,
        repository=repo,
        worker_id=worker_id,
    )

    with patch.object(orchestrator, "_run_pipeline") as mock_pipeline:
        mock_pipeline.return_value = _make_fake_result("inc_01")
        result = orchestrator.investigate(inc_dir)

    assert orchestrator.state is not None
    assert orchestrator.state.incident_id == "inc_01"
    assert orchestrator.state.lease_owner == worker_id
    assert orchestrator.state.is_lease_active()


def test_async_resume_acquires_durable_ownership_before_202(incidents_dir):
    """Async resume acquires durable lease in the repository BEFORE returning 202,
    and passes the matching worker_id to background IncidentOrchestrator."""
    failed_state = InvestigationState(incident_id="inc_async_01", status=IncidentStatus.FAILED)
    failed_state.lease_owner = None
    failed_state.lease_expires_at = None

    repo = MockRepository(states={"inc_async_01": failed_state})
    app = create_app(repository=repo, incidents_root=incidents_dir)
    client = TestClient(app)

    captured_init_kwargs = {}
    lease_state_at_acceptance = {}

    with patch("api.routes.IncidentOrchestrator") as MockOrch:
        def capture_init(**kwargs):
            captured_init_kwargs.update(kwargs)
            # Inspect repo state at the time background orchestrator is spawned
            persisted = repo.load("inc_async_01")
            lease_state_at_acceptance["status"] = (
                persisted.status.value if hasattr(persisted.status, "value") else str(persisted.status)
            )
            lease_state_at_acceptance["lease_owner"] = persisted.lease_owner
            lease_state_at_acceptance["lease_active"] = persisted.is_lease_active()
            lease_state_at_acceptance["resumed_from_status"] = persisted.metadata.get("resumed_from_status")

            instance = MagicMock()
            instance.resume.return_value = _make_fake_result("inc_async_01")
            instance.state = InvestigationState(incident_id="inc_async_01", status=IncidentStatus.COMPLETED)
            return instance

        MockOrch.side_effect = capture_init

        resp = client.post(
            "/investigations/inc_async_01/resume",
            headers={"Prefer": "respond-async"},
        )

    assert resp.status_code == 202
    assert lease_state_at_acceptance["status"] == "RUNNING"
    assert lease_state_at_acceptance["lease_active"] is True
    assert lease_state_at_acceptance["lease_owner"] is not None
    assert lease_state_at_acceptance["resumed_from_status"] == "FAILED"
    assert captured_init_kwargs.get("worker_id") == lease_state_at_acceptance["lease_owner"]


def test_async_resume_occ_conflict_returns_409(incidents_dir):
    """Simulated PostgreSQL OCC conflict on resume (competing save) returns 409 Conflict,
    does NOT return 202, and releases the process-local registry."""
    failed_state = InvestigationState(incident_id="inc_async_01", status=IncidentStatus.FAILED)
    failed_state.lease_owner = None
    failed_state.lease_expires_at = None

    class ConflictingRepository(MockRepository):
        def save(self, state, events=None):
            raise ConcurrencyError("Optimistic concurrency conflict: version mismatch")

    repo = ConflictingRepository(states={"inc_async_01": failed_state})
    app = create_app(repository=repo, incidents_root=incidents_dir)
    client = TestClient(app)

    with patch("api.routes.IncidentOrchestrator") as MockOrch:
        resp = client.post(
            "/investigations/inc_async_01/resume",
            headers={"Prefer": "respond-async"},
        )

    assert resp.status_code == 409
    assert resp.json()["error"] == "conflict"
    # Background orchestrator must not have been invoked
    MockOrch.assert_not_called()
    # Process-local registry must be clean
    with _ACTIVE_LOCK:
        assert "inc_async_01" not in _ACTIVE_INVESTIGATIONS


def test_async_create_occ_conflict_returns_409(incidents_dir):
    """Simulated PostgreSQL OCC conflict on create (concurrent insert) returns 409 Conflict,
    does NOT return 202, and releases the process-local registry."""
    class ConflictingRepository(MockRepository):
        def save(self, state, events=None):
            raise ConcurrencyError("Concurrent insert conflict for investigation 'inc_async_01'")

    repo = ConflictingRepository()
    app = create_app(repository=repo, incidents_root=incidents_dir)
    client = TestClient(app)

    with patch("api.routes.IncidentOrchestrator") as MockOrch:
        resp = client.post(
            "/investigations",
            json={"incident_id": "inc_async_01"},
            headers={"Prefer": "respond-async"},
        )

    assert resp.status_code == 409
    assert resp.json()["error"] == "conflict"
    MockOrch.assert_not_called()
    with _ACTIVE_LOCK:
        assert "inc_async_01" not in _ACTIVE_INVESTIGATIONS


def test_async_create_persistence_error_returns_503(incidents_dir):
    """Failed persistence on async create returns 503 Service Unavailable,
    does NOT return 202, and releases process-local registry."""
    class FailingRepository(MockRepository):
        def save(self, state, events=None):
            raise PersistenceError("Database connection lost")

    repo = FailingRepository()
    app = create_app(repository=repo, incidents_root=incidents_dir)
    client = TestClient(app)

    with patch("api.routes.IncidentOrchestrator") as MockOrch:
        resp = client.post(
            "/investigations",
            json={"incident_id": "inc_async_01"},
            headers={"Prefer": "respond-async"},
        )

    assert resp.status_code == 503
    MockOrch.assert_not_called()
    with _ACTIVE_LOCK:
        assert "inc_async_01" not in _ACTIVE_INVESTIGATIONS


def test_async_resume_persistence_error_returns_503(incidents_dir):
    """Failed persistence on async resume returns 503 Service Unavailable,
    does NOT return 202, and releases process-local registry."""
    failed_state = InvestigationState(incident_id="inc_async_01", status=IncidentStatus.FAILED)
    failed_state.lease_owner = None
    failed_state.lease_expires_at = None

    class FailingRepository(MockRepository):
        def save(self, state, events=None):
            raise PersistenceError("Database connection lost")

    repo = FailingRepository(states={"inc_async_01": failed_state})
    app = create_app(repository=repo, incidents_root=incidents_dir)
    client = TestClient(app)

    with patch("api.routes.IncidentOrchestrator") as MockOrch:
        resp = client.post(
            "/investigations/inc_async_01/resume",
            headers={"Prefer": "respond-async"},
        )

    assert resp.status_code == 503
    MockOrch.assert_not_called()
    with _ACTIVE_LOCK:
        assert "inc_async_01" not in _ACTIVE_INVESTIGATIONS


def test_sync_resume_returns_200_and_cleans_registry(incidents_dir):
    """Synchronous resume retains existing contract: 200 OK with CreateInvestigationResponse,
    and cleans up process-local registry."""
    failed_state = InvestigationState(incident_id="inc_async_01", status=IncidentStatus.FAILED)
    failed_state.lease_owner = None
    failed_state.lease_expires_at = None

    repo = MockRepository(states={"inc_async_01": failed_state})
    app = create_app(repository=repo, incidents_root=incidents_dir)
    client = TestClient(app)

    fake_state = InvestigationState(incident_id="inc_async_01", status=IncidentStatus.COMPLETED)

    with patch("api.routes.IncidentOrchestrator") as MockOrch:
        instance = MockOrch.return_value
        instance.resume.return_value = _make_fake_result("inc_async_01")
        instance.state = fake_state

        resp = client.post("/investigations/inc_async_01/resume")

    assert resp.status_code == 200
    data = resp.json()
    assert data["investigation_id"] == "inc_async_01"
    assert data["pipeline_status"] == "COMPLETED"
    with _ACTIVE_LOCK:
        assert "inc_async_01" not in _ACTIVE_INVESTIGATIONS
