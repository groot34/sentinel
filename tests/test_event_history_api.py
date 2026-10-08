"""Focused tests for Mission 12B: GET /investigations/{investigation_id}/events.

Tests cover:
 1. Successful event-history retrieval
 2. Chronological ordering (created_at ASC, event_id ASC)
 3. Deterministic ordering for equal timestamps
 4. Empty history (investigation exists, no events)
 5. Nonexistent investigation -> 404
 6. Malformed investigation ID -> 400/404
 7. Traversal/path-attack validation -> 400/404
 8. Response schema validation (extra fields rejected)
 9. Payload allowlisting (unsafe keys stripped)
10. Investigation isolation (events from another investigation not returned)
11. Filesystem backend behaviour (empty + backend_supports_event_history=False)
12. Repository unavailable -> 503
13. PersistenceError propagated through existing handler
14. Existing API regression (no regressions in other routes)

No LLM calls, no Groq, no network.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

import pytest
from starlette.testclient import TestClient

from api.app import create_app
from api.routes import _ACTIVE_INVESTIGATIONS, _ACTIVE_LOCK
from core.domain.models import IncidentStatus
from core.domain.state import InvestigationState
from core.persistence import PersistenceError, PersistenceRepository
from core.persistence.outbox import OutboxEvent


# ---------------------------------------------------------------------------
# Helper factories
# ---------------------------------------------------------------------------

def _make_state(incident_id: str = "test-incident") -> InvestigationState:
    state = InvestigationState(incident_id=incident_id)
    state.status = IncidentStatus.COMPLETED
    state.started_at = "2026-01-01T00:00:00+00:00"
    return state


def _make_outbox_event(
    event_id: str,
    investigation_id: str,
    event_type: str = "StageCompleted",
    created_at: str = "2026-01-01T00:00:00+00:00",
    status: str = "processed",
    payload: Optional[Dict[str, Any]] = None,
) -> OutboxEvent:
    return OutboxEvent(
        event_id=event_id,
        investigation_id=investigation_id,
        event_type=event_type,
        payload=payload or {
            "event_id": event_id,
            "event_type": event_type,
            "timestamp": created_at,
            "investigation_id": investigation_id,
            "stage": "logs",
            "llm_calls": 2,
        },
        status=status,
        created_at=created_at,
        processed_at="2026-01-01T00:01:00+00:00" if status == "processed" else None,
        retry_count=0,
    )


# ---------------------------------------------------------------------------
# Mock repository with configurable event list
# ---------------------------------------------------------------------------

class MockRepository(PersistenceRepository):
    """In-memory mock with per-investigation event lists."""

    def __init__(
        self,
        states: Optional[Dict[str, InvestigationState]] = None,
        events: Optional[Dict[str, List[OutboxEvent]]] = None,
        *,
        supports_history: bool = True,
        raise_on_list_events: Optional[Exception] = None,
    ) -> None:
        self._states: Dict[str, InvestigationState] = states or {}
        self._events: Dict[str, List[OutboxEvent]] = events or {}
        self._supports_history = supports_history
        self._raise_on_list_events = raise_on_list_events

    def save(self, state: InvestigationState, events=None) -> None:
        self._states[state.incident_id] = state

    def load(self, investigation_id: str) -> Optional[InvestigationState]:
        return self._states.get(investigation_id)

    def exists(self, investigation_id: str) -> bool:
        return investigation_id in self._states

    def delete(self, investigation_id: str) -> None:
        self._states.pop(investigation_id, None)

    def list(self) -> List[str]:
        return sorted(self._states.keys())

    @property
    def supports_event_history(self) -> bool:
        return self._supports_history

    def list_events(self, investigation_id: str, limit: int = 200) -> List[OutboxEvent]:
        if self._raise_on_list_events is not None:
            raise self._raise_on_list_events
        return self._events.get(investigation_id, [])[:limit]


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


def _client(repository=None) -> TestClient:
    return TestClient(create_app(repository=repository), raise_server_exceptions=False)


# ---------------------------------------------------------------------------
# 1. Successful retrieval
# ---------------------------------------------------------------------------

class TestSuccessfulRetrieval:
    def test_returns_200_with_events(self):
        ev = _make_outbox_event("evt-001", "inc-1")
        repo = MockRepository(states={"inc-1": _make_state("inc-1")}, events={"inc-1": [ev]})
        resp = _client(repo).get("/investigations/inc-1/events")
        assert resp.status_code == 200
        body = resp.json()
        assert body["investigation_id"] == "inc-1"
        assert body["total"] == 1
        assert len(body["events"]) == 1
        assert body["backend_supports_event_history"] is True

    def test_event_fields_present(self):
        ev = OutboxEvent(
            event_id="evt-002",
            investigation_id="inc-1",
            event_type="StageStarted",
            payload={"event_id": "evt-002", "event_type": "StageStarted",
                     "timestamp": "2026-01-01T00:00:00+00:00",
                     "investigation_id": "inc-1", "stage": "metrics"},
            status="pending",
            created_at="2026-01-01T00:00:00+00:00",
            retry_count=1,
        )
        repo = MockRepository(states={"inc-1": _make_state("inc-1")}, events={"inc-1": [ev]})
        resp = _client(repo).get("/investigations/inc-1/events")
        item = resp.json()["events"][0]
        assert item["event_id"] == "evt-002"
        assert item["event_type"] == "StageStarted"
        assert item["status"] == "pending"
        assert item["retry_count"] == 1

    def test_all_delivery_statuses_returned(self):
        """pending, processed, and failed events all appear in history."""
        events = [
            _make_outbox_event("e1", "inc-1", status="pending"),
            _make_outbox_event("e2", "inc-1", status="processed"),
            _make_outbox_event("e3", "inc-1", status="failed"),
        ]
        repo = MockRepository(states={"inc-1": _make_state("inc-1")}, events={"inc-1": events})
        resp = _client(repo).get("/investigations/inc-1/events")
        returned = {e["status"] for e in resp.json()["events"]}
        assert returned == {"pending", "processed", "failed"}


# ---------------------------------------------------------------------------
# 2 & 3. Ordering
# ---------------------------------------------------------------------------

class TestOrdering:
    def test_chronological_order(self):
        events = sorted([
            _make_outbox_event("e3", "inc-1", created_at="2026-01-03T00:00:00+00:00"),
            _make_outbox_event("e1", "inc-1", created_at="2026-01-01T00:00:00+00:00"),
            _make_outbox_event("e2", "inc-1", created_at="2026-01-02T00:00:00+00:00"),
        ], key=lambda e: e.created_at)
        repo = MockRepository(states={"inc-1": _make_state("inc-1")}, events={"inc-1": events})
        ids = [e["event_id"] for e in _client(repo).get("/investigations/inc-1/events").json()["events"]]
        assert ids == ["e1", "e2", "e3"]

    def test_deterministic_order_equal_timestamps(self):
        same_ts = "2026-01-01T00:00:00+00:00"
        events = [
            _make_outbox_event("e-aaa", "inc-1", created_at=same_ts),
            _make_outbox_event("e-bbb", "inc-1", created_at=same_ts),
            _make_outbox_event("e-ccc", "inc-1", created_at=same_ts),
        ]
        repo = MockRepository(states={"inc-1": _make_state("inc-1")}, events={"inc-1": events})
        ids = [e["event_id"] for e in _client(repo).get("/investigations/inc-1/events").json()["events"]]
        assert ids == ["e-aaa", "e-bbb", "e-ccc"]


# ---------------------------------------------------------------------------
# 4. Empty history
# ---------------------------------------------------------------------------

class TestEmptyHistory:
    def test_investigation_exists_no_events(self):
        repo = MockRepository(states={"inc-1": _make_state("inc-1")}, events={})
        resp = _client(repo).get("/investigations/inc-1/events")
        assert resp.status_code == 200
        body = resp.json()
        assert body["total"] == 0
        assert body["events"] == []


# ---------------------------------------------------------------------------
# 5. Nonexistent investigation -> 404
# ---------------------------------------------------------------------------

class TestNonexistentInvestigation:
    def test_404_for_unknown_id(self):
        resp = _client(MockRepository()).get("/investigations/no-such-id/events")
        assert resp.status_code == 404
        assert resp.json()["error"] == "investigation_not_found"

    def test_404_does_not_look_like_empty_history(self):
        resp = _client(MockRepository()).get("/investigations/missing/events")
        assert resp.status_code == 404
        assert "events" not in resp.json()


# ---------------------------------------------------------------------------
# 6 & 7. Malformed and traversal IDs
# ---------------------------------------------------------------------------

class TestIdValidation:
    @pytest.mark.parametrize("bad_id", ["a" * 200])
    def test_400_for_too_long_id(self, bad_id):
        resp = _client(MockRepository()).get(f"/investigations/{bad_id}/events")
        assert resp.status_code in (400, 404)

    @pytest.mark.parametrize("attack_id", [
        "../../../etc/passwd",
        "valid/../evil",
    ])
    def test_traversal_rejected(self, attack_id):
        resp = _client(MockRepository()).get(f"/investigations/{attack_id}/events")
        assert resp.status_code in (400, 404)

    def test_colon_in_id_rejected(self):
        resp = _client(MockRepository()).get("/investigations/inc:1/events")
        assert resp.status_code in (400, 404)


# ---------------------------------------------------------------------------
# 8. Response schema validation
# ---------------------------------------------------------------------------

class TestResponseSchema:
    def test_no_extra_top_level_keys(self):
        ev = _make_outbox_event("e1", "inc-1")
        repo = MockRepository(states={"inc-1": _make_state("inc-1")}, events={"inc-1": [ev]})
        body = _client(repo).get("/investigations/inc-1/events").json()
        allowed = {"investigation_id", "total", "events", "backend_supports_event_history"}
        assert not set(body.keys()) - allowed

    def test_no_extra_event_item_keys(self):
        ev = _make_outbox_event("e1", "inc-1")
        repo = MockRepository(states={"inc-1": _make_state("inc-1")}, events={"inc-1": [ev]})
        item = _client(repo).get("/investigations/inc-1/events").json()["events"][0]
        allowed = {
            "event_id", "investigation_id", "event_type", "timestamp",
            "created_at", "processed_at", "status", "retry_count",
            "error_message", "payload",
        }
        assert not set(item.keys()) - allowed

    def test_total_equals_len_events(self):
        events = [_make_outbox_event(f"e{i}", "inc-1") for i in range(3)]
        repo = MockRepository(states={"inc-1": _make_state("inc-1")}, events={"inc-1": events})
        body = _client(repo).get("/investigations/inc-1/events").json()
        assert body["total"] == len(body["events"])


# ---------------------------------------------------------------------------
# 9. Payload allowlisting
# ---------------------------------------------------------------------------

class TestPayloadAllowlisting:
    def test_unsafe_keys_stripped(self):
        dirty = {
            "event_id": "e1", "event_type": "StageCompleted",
            "timestamp": "2026-01-01T00:00:00+00:00",
            "investigation_id": "inc-1", "stage": "logs", "llm_calls": 1,
            "internal_secret": "sk-abc123",
            "ground_truth": "do not expose",
            "filesystem_path": "/home/user/.ssh/id_rsa",
            "_private": True,
        }
        ev = OutboxEvent(
            event_id="e1", investigation_id="inc-1",
            event_type="StageCompleted", payload=dirty,
            status="processed", created_at="2026-01-01T00:00:00+00:00",
        )
        repo = MockRepository(states={"inc-1": _make_state("inc-1")}, events={"inc-1": [ev]})
        payload = _client(repo).get("/investigations/inc-1/events").json()["events"][0]["payload"]
        assert payload["stage"] == "logs"
        assert "internal_secret" not in payload
        assert "ground_truth" not in payload
        assert "filesystem_path" not in payload
        assert "_private" not in payload

    def test_allowlisted_keys_survive(self):
        full = {
            "event_id": "e1", "event_type": "InvestigationCompleted",
            "timestamp": "2026-01-01T00:00:00+00:00",
            "investigation_id": "inc-1", "status": "COMPLETED",
            "total_llm_calls": 42, "total_tokens": 8000,
            "confirmed_hypotheses": 3, "proposals_generated": 2,
            "proposals_approved": 1, "error": None,
        }
        ev = OutboxEvent(
            event_id="e1", investigation_id="inc-1",
            event_type="InvestigationCompleted", payload=full,
            status="processed", created_at="2026-01-01T00:00:00+00:00",
        )
        repo = MockRepository(states={"inc-1": _make_state("inc-1")}, events={"inc-1": [ev]})
        payload = _client(repo).get("/investigations/inc-1/events").json()["events"][0]["payload"]
        assert payload["total_llm_calls"] == 42
        assert payload["confirmed_hypotheses"] == 3
        assert payload["proposals_approved"] == 1

    def test_project_event_payload_helper_directly(self):
        from api.models import _project_event_payload
        raw = {"stage": "logs", "llm_calls": 3, "secret_key": "sk-xyz", "ground_truth": "leak"}
        result = _project_event_payload(raw)
        assert "stage" in result
        assert "secret_key" not in result
        assert "ground_truth" not in result


# ---------------------------------------------------------------------------
# 10. Investigation isolation
# ---------------------------------------------------------------------------

class TestInvestigationIsolation:
    def test_events_isolated(self):
        ev_a = _make_outbox_event("ea1", "inc-a")
        ev_b = _make_outbox_event("eb1", "inc-b")
        repo = MockRepository(
            states={"inc-a": _make_state("inc-a"), "inc-b": _make_state("inc-b")},
            events={"inc-a": [ev_a], "inc-b": [ev_b]},
        )
        ids_a = {e["event_id"] for e in _client(repo).get("/investigations/inc-a/events").json()["events"]}
        ids_b = {e["event_id"] for e in _client(repo).get("/investigations/inc-b/events").json()["events"]}
        assert ids_a == {"ea1"}
        assert ids_b == {"eb1"}
        assert ids_a.isdisjoint(ids_b)


# ---------------------------------------------------------------------------
# 11. Filesystem backend
# ---------------------------------------------------------------------------

class TestFilesystemBackend:
    def test_returns_empty_with_backend_flag_false(self, tmp_path):
        from core.persistence.filesystem import FilesystemRepository
        repo = FilesystemRepository(tmp_path)
        repo.save(_make_state("inc-fs"))
        client = TestClient(create_app(repository=repo), raise_server_exceptions=False)
        resp = client.get("/investigations/inc-fs/events")
        assert resp.status_code == 200
        body = resp.json()
        assert body["total"] == 0
        assert body["events"] == []
        assert body["backend_supports_event_history"] is False

    def test_filesystem_list_events_validates_id(self, tmp_path):
        from core.persistence.filesystem import FilesystemRepository
        repo = FilesystemRepository(tmp_path)
        with pytest.raises(PersistenceError):
            repo.list_events("../etc/passwd")

    def test_filesystem_list_events_valid_id_returns_empty(self, tmp_path):
        from core.persistence.filesystem import FilesystemRepository
        assert FilesystemRepository(tmp_path).list_events("valid-id") == []

    def test_filesystem_supports_event_history_false(self, tmp_path):
        from core.persistence.filesystem import FilesystemRepository
        assert FilesystemRepository(tmp_path).supports_event_history is False


# ---------------------------------------------------------------------------
# 12. Repository unavailable -> 503
# ---------------------------------------------------------------------------

class TestRepositoryUnavailable:
    def test_503_when_no_repository(self):
        # create_app() always assigns a default repo; build with a real MockRepository
        # then forcibly set app.state.repository = None to exercise the "no repo" guard.
        app = create_app(repository=MockRepository())
        app.state.repository = None
        resp = TestClient(app, raise_server_exceptions=False).get(
            "/investigations/inc-1/events"
        )
        assert resp.status_code == 503
        assert resp.json()["error"] == "persistence_unavailable"




# ---------------------------------------------------------------------------
# 13. PersistenceError propagation
# ---------------------------------------------------------------------------

class TestPersistenceError:
    def test_persistence_error_propagates(self):
        repo = MockRepository(
            states={"inc-1": _make_state("inc-1")},
            raise_on_list_events=PersistenceError("DB is down"),
        )
        resp = _client(repo).get("/investigations/inc-1/events")
        assert resp.status_code in (500, 503)

    def test_generic_exception_sanitized(self):
        repo = MockRepository(
            states={"inc-1": _make_state("inc-1")},
            raise_on_list_events=RuntimeError("internal surprise"),
        )
        resp = _client(repo).get("/investigations/inc-1/events")
        assert "internal surprise" not in resp.text


# ---------------------------------------------------------------------------
# 14. Regression: existing routes unaffected
# ---------------------------------------------------------------------------

class TestExistingRoutesRegression:
    def test_health(self):
        assert _client(MockRepository()).get("/health").status_code == 200

    def test_list_investigations(self):
        repo = MockRepository(states={"inc-1": _make_state("inc-1")})
        assert "inc-1" in _client(repo).get("/investigations").json()["investigations"]

    def test_get_investigation(self):
        repo = MockRepository(states={"inc-1": _make_state("inc-1")})
        resp = _client(repo).get("/investigations/inc-1")
        assert resp.status_code == 200
        assert resp.json()["investigation_id"] == "inc-1"
