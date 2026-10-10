"""Focused tests for Mission 12C: Live Investigation Event Streaming via SSE.

Tests cover:
 1. Content negotiation and JSON regression (Accept header handling)
 2. Historical events on connection
 3. Events committed after connection
 4. Canonical ordering (created_at ASC, event_id ASC)
 5. Last-Event-ID reconnection (header and query param fallback)
 6. Unknown / unresolvable cursor handling
 7. No duplicate delivery across polling iterations
 8. Terminal investigation history draining and clean closure
 9. Client disconnect / cancellation cleanup
10. Filesystem unsupported-history behaviour (honest notice event)
11. Investigation isolation and malformed IDs
12. Payload allowlisting (forbidden keys stripped from SSE data)
13. Periodic keep-alive comments
14. Existing API regression suite
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pytest
from starlette.testclient import TestClient

from api.app import create_app
from core.domain.models import IncidentStatus
from core.domain.state import InvestigationState
from core.persistence.filesystem import FilesystemRepository
from core.persistence.outbox import OutboxEvent
from core.persistence.repository import PersistenceError, PersistenceRepository


# ---------------------------------------------------------------------------
# Test Doubles
# ---------------------------------------------------------------------------

class MockStreamingRepository(PersistenceRepository):
    """In-memory repository supporting outbox events and dynamic additions."""

    def __init__(
        self,
        states: Optional[Dict[str, InvestigationState]] = None,
        events: Optional[List[OutboxEvent]] = None,
        supports_history: bool = True,
    ) -> None:
        self._states: Dict[str, InvestigationState] = dict(states or {})
        self._events: List[OutboxEvent] = list(events or [])
        self._supports_history = supports_history

    @property
    def supports_event_history(self) -> bool:
        return self._supports_history

    def exists(self, investigation_id: str) -> bool:
        return investigation_id in self._states

    def load(self, investigation_id: str) -> Optional[InvestigationState]:
        return self._states.get(investigation_id)

    def save(self, state: InvestigationState, events: Optional[List[Any]] = None) -> None:
        self._states[state.incident_id] = state
        if events:
            for ev in events:
                if isinstance(ev, OutboxEvent):
                    self._events.append(ev)
                elif hasattr(ev, "event_id"):
                    self._events.append(OutboxEvent.from_domain_event(ev))

    def delete(self, investigation_id: str) -> None:
        self._states.pop(investigation_id, None)
        self._events = [e for e in self._events if e.investigation_id != investigation_id]

    def list(self) -> List[str]:
        return sorted(self._states.keys())

    def list_events(
        self,
        investigation_id: str,
        limit: int = 200,
        after_cursor: Optional[Tuple[str, str]] = None,
    ) -> List[OutboxEvent]:
        if not self._supports_history:
            return []
        matching = [e for e in self._events if e.investigation_id == investigation_id]
        # Canonical ordering: created_at ASC, event_id ASC
        matching.sort(key=lambda e: (e.created_at or "", e.event_id or ""))
        if after_cursor is not None:
            c_ts, c_eid = after_cursor
            matching = [
                e
                for e in matching
                if (
                    e.created_at.isoformat() if hasattr(e.created_at, "isoformat") else str(e.created_at or ""),
                    e.event_id or "",
                )
                > (c_ts, c_eid)
            ]
        return matching[:limit]

    def add_event(self, event: OutboxEvent) -> None:
        self._events.append(event)


def _make_state(
    incident_id: str = "INV-001",
    status: IncidentStatus = IncidentStatus.RUNNING,
) -> InvestigationState:
    state = InvestigationState(incident_id=incident_id)
    state.status = status
    return state


def _make_outbox_event(
    event_id: str,
    investigation_id: str = "INV-001",
    event_type: str = "StageStarted",
    created_at: str = "2026-10-10T18:00:00.000000Z",
    payload: Optional[Dict[str, Any]] = None,
) -> OutboxEvent:
    return OutboxEvent(
        event_id=event_id,
        investigation_id=investigation_id,
        event_type=event_type,
        payload=payload if payload is not None else {"stage": "logs"},
        status="pending",
        created_at=created_at,
    )


def parse_sse_text(text: str) -> List[Dict[str, Any]]:
    """Parse raw SSE payload into structured event dicts."""
    events: List[Dict[str, Any]] = []
    blocks = [b for b in text.split("\n\n") if b.strip()]
    for block in blocks:
        item: Dict[str, Any] = {}
        for line in block.split("\n"):
            line = line.strip()
            if not line:
                continue
            if line.startswith(":"):
                item["comment"] = line[1:].strip()
            elif ":" in line:
                k, v = line.split(":", 1)
                k = k.strip()
                v = v.strip()
                if k == "data":
                    try:
                        item["data"] = json.loads(v)
                    except Exception:
                        item["data"] = v
                else:
                    item[k] = v
        if item:
            events.append(item)
    return events


# ---------------------------------------------------------------------------
# 1. Content Negotiation and JSON Regression
# ---------------------------------------------------------------------------

class TestContentNegotiation:
    def test_accept_event_stream_returns_sse(self):
        repo = MockStreamingRepository(
            states={"INV-001": _make_state(status=IncidentStatus.COMPLETED)},
            events=[_make_outbox_event("evt_1")],
        )
        app = create_app(repository=repo)
        client = TestClient(app)

        resp = client.get(
            "/investigations/INV-001/events",
            headers={"Accept": "text/event-stream"},
        )
        assert resp.status_code == 200
        assert "text/event-stream" in resp.headers.get("content-type", "")
        events = parse_sse_text(resp.text)
        assert len(events) == 1
        assert events[0]["id"] == "evt_1"
        assert events[0]["event"] == "StageStarted"

    def test_accept_json_returns_mission12b_json(self):
        repo = MockStreamingRepository(
            states={"INV-001": _make_state(status=IncidentStatus.COMPLETED)},
            events=[_make_outbox_event("evt_1")],
        )
        app = create_app(repository=repo)
        client = TestClient(app)

        resp = client.get(
            "/investigations/INV-001/events",
            headers={"Accept": "application/json"},
        )
        assert resp.status_code == 200
        assert "application/json" in resp.headers.get("content-type", "")
        data = resp.json()
        assert data["investigation_id"] == "INV-001"
        assert data["total"] == 1
        assert data["events"][0]["event_id"] == "evt_1"
        assert data["backend_supports_event_history"] is True

    def test_default_accept_header_returns_json(self):
        repo = MockStreamingRepository(
            states={"INV-001": _make_state(status=IncidentStatus.COMPLETED)},
            events=[_make_outbox_event("evt_1")],
        )
        app = create_app(repository=repo)
        client = TestClient(app)

        resp = client.get("/investigations/INV-001/events")
        assert resp.status_code == 200
        assert "application/json" in resp.headers.get("content-type", "")


# ---------------------------------------------------------------------------
# 2. Historical Events on Connection
# ---------------------------------------------------------------------------

class TestHistoricalEventsOnConnection:
    def test_delivers_all_historical_events(self):
        ev1 = _make_outbox_event("evt_1", created_at="2026-10-10T18:00:00Z")
        ev2 = _make_outbox_event("evt_2", created_at="2026-10-10T18:00:01Z")
        ev3 = _make_outbox_event("evt_3", created_at="2026-10-10T18:00:02Z", event_type="InvestigationCompleted")
        repo = MockStreamingRepository(
            states={"INV-001": _make_state(status=IncidentStatus.COMPLETED)},
            events=[ev1, ev2, ev3],
        )
        app = create_app(repository=repo)
        client = TestClient(app)

        resp = client.get(
            "/investigations/INV-001/events",
            headers={"Accept": "text/event-stream"},
        )
        assert resp.status_code == 200
        events = parse_sse_text(resp.text)
        assert len(events) == 3
        assert [e["id"] for e in events] == ["evt_1", "evt_2", "evt_3"]
        assert events[2]["event"] == "InvestigationCompleted"


# ---------------------------------------------------------------------------
# 3. Canonical Ordering
# ---------------------------------------------------------------------------

class TestCanonicalOrdering:
    def test_ordering_by_created_at_and_event_id(self):
        ev_c = _make_outbox_event("evt_c", created_at="2026-10-10T18:00:05Z")
        ev_a = _make_outbox_event("evt_a", created_at="2026-10-10T18:00:01Z")
        ev_b2 = _make_outbox_event("evt_b2", created_at="2026-10-10T18:00:03Z")
        ev_b1 = _make_outbox_event("evt_b1", created_at="2026-10-10T18:00:03Z")  # tie in timestamp

        repo = MockStreamingRepository(
            states={"INV-001": _make_state(status=IncidentStatus.COMPLETED)},
            events=[ev_c, ev_a, ev_b2, ev_b1],
        )
        app = create_app(repository=repo)
        client = TestClient(app)

        resp = client.get(
            "/investigations/INV-001/events",
            headers={"Accept": "text/event-stream"},
        )
        events = parse_sse_text(resp.text)
        ids = [e["id"] for e in events]
        assert ids == ["evt_a", "evt_b1", "evt_b2", "evt_c"]


# ---------------------------------------------------------------------------
# 4. Last-Event-ID Reconnection
# ---------------------------------------------------------------------------

class TestLastEventIdReconnection:
    def test_reconnect_via_header_skips_prior_events(self):
        ev1 = _make_outbox_event("evt_1", created_at="2026-10-10T18:00:01Z")
        ev2 = _make_outbox_event("evt_2", created_at="2026-10-10T18:00:02Z")
        ev3 = _make_outbox_event("evt_3", created_at="2026-10-10T18:00:03Z")
        repo = MockStreamingRepository(
            states={"INV-001": _make_state(status=IncidentStatus.COMPLETED)},
            events=[ev1, ev2, ev3],
        )
        app = create_app(repository=repo)
        client = TestClient(app)

        resp = client.get(
            "/investigations/INV-001/events",
            headers={
                "Accept": "text/event-stream",
                "Last-Event-ID": "evt_2",
            },
        )
        events = parse_sse_text(resp.text)
        assert len(events) == 1
        assert events[0]["id"] == "evt_3"

    def test_reconnect_via_query_param_fallback(self):
        ev1 = _make_outbox_event("evt_1", created_at="2026-10-10T18:00:01Z")
        ev2 = _make_outbox_event("evt_2", created_at="2026-10-10T18:00:02Z")
        ev3 = _make_outbox_event("evt_3", created_at="2026-10-10T18:00:03Z")
        repo = MockStreamingRepository(
            states={"INV-001": _make_state(status=IncidentStatus.COMPLETED)},
            events=[ev1, ev2, ev3],
        )
        app = create_app(repository=repo)
        client = TestClient(app)

        resp = client.get(
            "/investigations/INV-001/events?last_event_id=evt_1",
            headers={"Accept": "text/event-stream"},
        )
        events = parse_sse_text(resp.text)
        assert len(events) == 2
        assert [e["id"] for e in events] == ["evt_2", "evt_3"]

    def test_reconnect_near_end_of_first_page_across_page_boundary(self):
        # 250 total events; page_size defaults to 200.
        # evt_198 is near the end of page 1.
        all_events = [
            _make_outbox_event(
                event_id=f"evt_{i:04d}",
                created_at=f"2026-10-10T18:00:00.{i:06d}Z",
            )
            for i in range(1, 251)
        ]
        repo = MockStreamingRepository(
            states={"INV-001": _make_state(status=IncidentStatus.COMPLETED)},
            events=all_events,
        )
        app = create_app(repository=repo)
        client = TestClient(app)

        resp = client.get(
            "/investigations/INV-001/events",
            headers={
                "Accept": "text/event-stream",
                "Last-Event-ID": "evt_0198",
            },
        )
        assert resp.status_code == 200
        events = parse_sse_text(resp.text)
        assert len(events) == 52  # evt_0199 through evt_0250
        delivered_ids = [e["id"] for e in events]
        expected_ids = [f"evt_{i:04d}" for i in range(199, 251)]
        assert delivered_ids == expected_ids

    def test_reconnect_across_page_boundary_on_second_page(self):
        # 250 total events; page_size defaults to 200.
        # evt_0210 is located on page 2 in stored history.
        all_events = [
            _make_outbox_event(
                event_id=f"evt_{i:04d}",
                created_at=f"2026-10-10T18:00:00.{i:06d}Z",
            )
            for i in range(1, 251)
        ]
        repo = MockStreamingRepository(
            states={"INV-001": _make_state(status=IncidentStatus.COMPLETED)},
            events=all_events,
        )
        app = create_app(repository=repo)
        client = TestClient(app)

        resp = client.get(
            "/investigations/INV-001/events",
            headers={
                "Accept": "text/event-stream",
                "Last-Event-ID": "evt_0210",
            },
        )
        assert resp.status_code == 200
        events = parse_sse_text(resp.text)
        assert len(events) == 40  # evt_0211 through evt_0250
        delivered_ids = [e["id"] for e in events]
        expected_ids = [f"evt_{i:04d}" for i in range(211, 251)]
        assert delivered_ids == expected_ids


# ---------------------------------------------------------------------------
# 5. Unknown / Unresolvable Cursor Handling
# ---------------------------------------------------------------------------

class TestUnknownCursorHandling:
    def test_unresolvable_cursor_emits_notice_and_delivers_from_start(self):
        ev1 = _make_outbox_event("evt_1", created_at="2026-10-10T18:00:01Z")
        ev2 = _make_outbox_event("evt_2", created_at="2026-10-10T18:00:02Z")
        repo = MockStreamingRepository(
            states={"INV-001": _make_state(status=IncidentStatus.COMPLETED)},
            events=[ev1, ev2],
        )
        app = create_app(repository=repo)
        client = TestClient(app)

        resp = client.get(
            "/investigations/INV-001/events",
            headers={
                "Accept": "text/event-stream",
                "Last-Event-ID": "nonexistent_id",
            },
        )
        assert resp.status_code == 200
        assert "cursor 'nonexistent_id' could not be resolved" in resp.text
        events = parse_sse_text(resp.text)
        data_events = [e for e in events if "id" in e]
        assert len(data_events) == 2
        assert [e["id"] for e in data_events] == ["evt_1", "evt_2"]


# ---------------------------------------------------------------------------
# 6. Events Committed After Connection & No Duplicates
# ---------------------------------------------------------------------------

class TestLiveEventStreaming:
    def test_events_committed_after_connection_received_without_duplicates(self):
        ev1 = _make_outbox_event("evt_1", created_at="2026-10-10T18:00:01Z")
        state = _make_state(status=IncidentStatus.RUNNING)
        repo = MockStreamingRepository(
            states={"INV-001": state},
            events=[ev1],
        )
        app = create_app(repository=repo)
        app.state.sse_poll_interval = 0.02
        client = TestClient(app)

        def delayed_commit():
            time.sleep(0.05)
            ev2 = _make_outbox_event("evt_2", created_at="2026-10-10T18:00:02Z", event_type="InvestigationCompleted")
            repo.add_event(ev2)
            state.status = IncidentStatus.COMPLETED

        threading.Thread(target=delayed_commit, daemon=True).start()

        delivered = []
        with client.stream("GET", "/investigations/INV-001/events", headers={"Accept": "text/event-stream"}) as resp:
            for line in resp.iter_lines():
                if line.startswith("id:"):
                    delivered.append(line.split(":", 1)[1].strip())

        assert delivered == ["evt_1", "evt_2"]
        assert len(delivered) == len(set(delivered))

    def test_events_inserted_during_pagination_and_polling_delivered_in_order(self):
        # 50 initial events
        initial_events = [
            _make_outbox_event(
                event_id=f"evt_{i:04d}",
                created_at=f"2026-10-10T18:00:00.{i:06d}Z",
            )
            for i in range(1, 51)
        ]
        state = _make_state(status=IncidentStatus.RUNNING)
        repo = MockStreamingRepository(
            states={"INV-001": state},
            events=initial_events,
        )
        app = create_app(repository=repo)
        app.state.sse_poll_interval = 0.02
        client = TestClient(app)

        def delayed_insert():
            time.sleep(0.05)
            # Add 170 more events (total 220, spanning across 200 default page limit)
            for i in range(51, 221):
                ev = _make_outbox_event(
                    event_id=f"evt_{i:04d}",
                    created_at=f"2026-10-10T18:00:00.{i:06d}Z",
                )
                repo.add_event(ev)
            state.status = IncidentStatus.COMPLETED

        threading.Thread(target=delayed_insert, daemon=True).start()

        delivered = []
        with client.stream("GET", "/investigations/INV-001/events", headers={"Accept": "text/event-stream"}) as resp:
            for line in resp.iter_lines():
                if line.startswith("id:"):
                    delivered.append(line.split(":", 1)[1].strip())

        expected_ids = [f"evt_{i:04d}" for i in range(1, 221)]
        assert delivered == expected_ids
        assert len(delivered) == 220
        assert len(delivered) == len(set(delivered))


# ---------------------------------------------------------------------------
# 7. Terminal Investigation History Draining and Clean Closure
# ---------------------------------------------------------------------------

class TestTerminalDraining:
    def test_drains_events_and_closes_cleanly(self):
        ev1 = _make_outbox_event("evt_1", created_at="2026-10-10T18:00:01Z")
        ev2 = _make_outbox_event("evt_2", created_at="2026-10-10T18:00:02Z", event_type="InvestigationCompleted")
        repo = MockStreamingRepository(
            states={"INV-001": _make_state(status=IncidentStatus.COMPLETED)},
            events=[ev1, ev2],
        )
        app = create_app(repository=repo)
        app.state.sse_poll_interval = 0.02
        client = TestClient(app)

        resp = client.get(
            "/investigations/INV-001/events",
            headers={"Accept": "text/event-stream"},
        )
        assert resp.status_code == 200
        events = parse_sse_text(resp.text)
        assert len(events) == 2
        assert events[1]["event"] == "InvestigationCompleted"

    def test_drains_more_than_200_events_across_page_boundaries_and_closes(self):
        # 250 total events on completed investigation; default page size is 200.
        all_events = [
            _make_outbox_event(
                event_id=f"evt_{i:04d}",
                created_at=f"2026-10-10T18:00:00.{i:06d}Z",
            )
            for i in range(1, 251)
        ]
        repo = MockStreamingRepository(
            states={"INV-001": _make_state(status=IncidentStatus.COMPLETED)},
            events=all_events,
        )
        app = create_app(repository=repo)
        client = TestClient(app)

        resp = client.get(
            "/investigations/INV-001/events",
            headers={"Accept": "text/event-stream"},
        )
        assert resp.status_code == 200
        events = parse_sse_text(resp.text)
        assert len(events) == 250
        delivered_ids = [e["id"] for e in events]
        expected_ids = [f"evt_{i:04d}" for i in range(1, 251)]
        assert delivered_ids == expected_ids
        # Verify canonical order and uniqueness
        assert delivered_ids == sorted(delivered_ids)
        assert len(delivered_ids) == len(set(delivered_ids))

    def test_drains_multiple_pages_with_custom_page_size(self):
        # 120 total events with page_size=25 (spans across 5 pages).
        all_events = [
            _make_outbox_event(
                event_id=f"evt_{i:04d}",
                created_at=f"2026-10-10T18:00:00.{i:06d}Z",
            )
            for i in range(1, 121)
        ]
        repo = MockStreamingRepository(
            states={"INV-001": _make_state(status=IncidentStatus.COMPLETED)},
            events=all_events,
        )
        app = create_app(repository=repo)
        app.state.sse_page_size = 25
        client = TestClient(app)

        resp = client.get(
            "/investigations/INV-001/events",
            headers={"Accept": "text/event-stream"},
        )
        assert resp.status_code == 200
        events = parse_sse_text(resp.text)
        assert len(events) == 120
        delivered_ids = [e["id"] for e in events]
        expected_ids = [f"evt_{i:04d}" for i in range(1, 121)]
        assert delivered_ids == expected_ids


# ---------------------------------------------------------------------------
# 8. Client Disconnect / Cancellation Cleanup
# ---------------------------------------------------------------------------

class TestClientDisconnect:
    def test_disconnect_cleans_up_without_error(self):
        ev1 = _make_outbox_event("evt_1", created_at="2026-10-10T18:00:01Z")
        repo = MockStreamingRepository(
            states={"INV-001": _make_state(status=IncidentStatus.RUNNING)},
            events=[ev1],
        )
        app = create_app(repository=repo)
        app.state.sse_poll_interval = 0.02
        app.state.sse_max_polls = 2
        client = TestClient(app)

        with client.stream("GET", "/investigations/INV-001/events", headers={"Accept": "text/event-stream"}) as resp:
            for line in resp.iter_lines():
                if "id:" in line:
                    break


# ---------------------------------------------------------------------------
# 9. Filesystem Unsupported-History Behaviour
# ---------------------------------------------------------------------------

class TestFilesystemBackend:
    def test_filesystem_backend_emits_honest_notice_and_closes(self, tmp_path):
        incident_dir = tmp_path / "incidents" / "INV-FS"
        incident_dir.mkdir(parents=True)
        repo = FilesystemRepository(persistence_root=tmp_path / "storage")
        state = InvestigationState(incident_id="INV-FS")
        state.status = IncidentStatus.COMPLETED
        repo.save(state)

        app = create_app(repository=repo, incidents_root=tmp_path / "incidents")
        client = TestClient(app)

        resp = client.get(
            "/investigations/INV-FS/events",
            headers={"Accept": "text/event-stream"},
        )
        assert resp.status_code == 200
        events = parse_sse_text(resp.text)
        assert len(events) == 1
        assert events[0]["event"] == "notice"
        assert events[0]["data"]["error"] == "event_history_unsupported"
        assert events[0]["data"]["backend_supports_event_history"] is False


# ---------------------------------------------------------------------------
# 10. Investigation Isolation and Malformed IDs
# ---------------------------------------------------------------------------

class TestInvestigationIsolationAndValidation:
    def test_404_for_unknown_investigation(self):
        repo = MockStreamingRepository(states={})
        app = create_app(repository=repo)
        client = TestClient(app)

        resp = client.get(
            "/investigations/UNKNOWN/events",
            headers={"Accept": "text/event-stream"},
        )
        assert resp.status_code == 404
        assert resp.json()["error"] == "investigation_not_found"

    def test_400_for_path_traversal(self):
        repo = MockStreamingRepository(states={})
        app = create_app(repository=repo)
        client = TestClient(app)

        resp = client.get(
            "/investigations/../../../etc/passwd/events",
            headers={"Accept": "text/event-stream"},
        )
        assert resp.status_code in (400, 404)

    def test_investigation_isolation(self):
        ev_a = _make_outbox_event("evt_a", investigation_id="INV-A")
        ev_b = _make_outbox_event("evt_b", investigation_id="INV-B")
        repo = MockStreamingRepository(
            states={
                "INV-A": _make_state("INV-A", IncidentStatus.COMPLETED),
                "INV-B": _make_state("INV-B", IncidentStatus.COMPLETED),
            },
            events=[ev_a, ev_b],
        )
        app = create_app(repository=repo)
        client = TestClient(app)

        resp = client.get(
            "/investigations/INV-A/events",
            headers={"Accept": "text/event-stream"},
        )
        events = parse_sse_text(resp.text)
        ids = [e["id"] for e in events]
        assert "evt_a" in ids
        assert "evt_b" not in ids


# ---------------------------------------------------------------------------
# 11. Payload Allowlisting in Streamed Data
# ---------------------------------------------------------------------------

class TestPayloadAllowlisting:
    def test_unsafe_keys_stripped_from_sse_payload(self):
        raw_payload = {
            "stage": "metrics",
            "llm_calls": 2,
            "ground_truth": "ATTACKER_READ_ME",
            "internal_db_password": "supersecretpassword",
            "traceback": "Traceback (most recent call last)...",
        }
        ev = _make_outbox_event("evt_secret", payload=raw_payload)
        repo = MockStreamingRepository(
            states={"INV-001": _make_state(status=IncidentStatus.COMPLETED)},
            events=[ev],
        )
        app = create_app(repository=repo)
        client = TestClient(app)

        resp = client.get(
            "/investigations/INV-001/events",
            headers={"Accept": "text/event-stream"},
        )
        events = parse_sse_text(resp.text)
        payload = events[0]["data"]["payload"]
        assert payload["stage"] == "metrics"
        assert payload["llm_calls"] == 2
        assert "ground_truth" not in payload
        assert "internal_db_password" not in payload
        assert "traceback" not in payload
        assert "supersecretpassword" not in resp.text
        assert "ATTACKER_READ_ME" not in resp.text


# ---------------------------------------------------------------------------
# 12. Periodic Keep-Alive Comments
# ---------------------------------------------------------------------------

class TestKeepAliveComments:
    def test_keepalive_comment_emitted_during_idle(self):
        state = _make_state(status=IncidentStatus.RUNNING)
        repo = MockStreamingRepository(states={"INV-001": state})
        app = create_app(repository=repo)
        app.state.sse_poll_interval = 0.02
        app.state.sse_keepalive_interval = 0.01
        app.state.sse_max_polls = 5
        client = TestClient(app)

        def delayed_finish():
            time.sleep(0.05)
            state.status = IncidentStatus.COMPLETED

        threading.Thread(target=delayed_finish, daemon=True).start()

        comments = []
        with client.stream("GET", "/investigations/INV-001/events", headers={"Accept": "text/event-stream"}) as resp:
            for line in resp.iter_lines():
                if line.startswith(":"):
                    comments.append(line)
                    break

        assert any("keep-alive" in c for c in comments)


# ---------------------------------------------------------------------------
# 13. Regression Suite
# ---------------------------------------------------------------------------

class TestApiRegression:
    def test_health(self):
        app = create_app(repository=MockStreamingRepository())
        client = TestClient(app)
        assert client.get("/health").status_code == 200

    def test_list_investigations(self):
        repo = MockStreamingRepository(states={"INV-001": _make_state()})
        app = create_app(repository=repo)
        client = TestClient(app)
        resp = client.get("/investigations")
        assert resp.status_code == 200
        assert "INV-001" in resp.json()["investigations"]

    def test_get_investigation(self):
        repo = MockStreamingRepository(states={"INV-001": _make_state()})
        app = create_app(repository=repo)
        client = TestClient(app)
        resp = client.get("/investigations/INV-001")
        assert resp.status_code == 200
        assert resp.json()["investigation_id"] == "INV-001"
