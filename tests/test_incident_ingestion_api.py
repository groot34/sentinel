"""Tests for Mission 19: POST /incidents safe incident bundle ingestion.

Covers the disabled-by-default opt-in, the strict request contract, the path
allowlist, resource limits, failure-safe atomic staging/publishing, the
rejection of ``overwrite=true`` (replacement is unsupported), coordination with
the active-investigation/lease guards, and compatibility of the pre-existing
investigation routes.
"""

from __future__ import annotations

import asyncio
import json
import os
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from unittest.mock import patch

import pytest
from starlette.testclient import TestClient

from agents import bundle_ingest
from agents.bundle_ingest import IngestionLimits
from api.app import create_app
from api.routes import _ACTIVE_INVESTIGATIONS, _ACTIVE_LOCK
from core.domain.models import IncidentStatus
from core.domain.state import InvestigationState
from core.persistence import PersistenceRepository


# ---------------------------------------------------------------------------
# Fixtures & helpers
# ---------------------------------------------------------------------------

class FakeRepository(PersistenceRepository):
    """Minimal in-memory repository for deterministic ingestion tests."""

    def __init__(self, states: Optional[Dict[str, InvestigationState]] = None) -> None:
        self._states: Dict[str, InvestigationState] = states or {}
        self.save_called: List[InvestigationState] = []

    def save(self, state: InvestigationState, events: Optional[List[Any]] = None) -> None:
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

    @property
    def supports_event_history(self) -> bool:
        return True

    def list_events(self, investigation_id: str, limit: int = 200, after_cursor: Any = None) -> List:
        return []


VALID_LOG = "2026-08-28T14:00:00Z INFO service started\n"
VALID_METRICS = "timestamp,cpu\n2026-08-28T14:00:00Z,0.5\n"
VALID_DIFF = "--- a/app.py\n+++ b/app.py\n"


def _valid_files() -> Dict[str, str]:
    """A minimal bundle that satisfies the Mission 18 preflight contract."""
    return {
        "logs/application.log": VALID_LOG,
        "metrics/metrics.csv": VALID_METRICS,
        "service/app.py": "print('hi')\n",
    }


@pytest.fixture(autouse=True)
def clean_process_registries():
    """Clear both process-local registries before and after every test."""
    with _ACTIVE_LOCK:
        _ACTIVE_INVESTIGATIONS.clear()
    with bundle_ingest._INFLIGHT_LOCK:
        bundle_ingest._INFLIGHT_IDS.clear()
    yield
    with _ACTIVE_LOCK:
        _ACTIVE_INVESTIGATIONS.clear()
    with bundle_ingest._INFLIGHT_LOCK:
        bundle_ingest._INFLIGHT_IDS.clear()


@pytest.fixture(autouse=True)
def clean_ingestion_env(monkeypatch: pytest.MonkeyPatch):
    """Ensure no ingestion-related environment variable leaks between tests."""
    for name in (
        "SENTINEL_ALLOW_INCIDENT_INGESTION",
        "SENTINEL_INGEST_MAX_FILES",
        "SENTINEL_INGEST_MAX_FILE_BYTES",
        "SENTINEL_INGEST_MAX_TOTAL_BYTES",
        "SENTINEL_INGEST_MAX_REQUEST_BYTES",
    ):
        monkeypatch.delenv(name, raising=False)
    yield


@pytest.fixture
def incidents_root(tmp_path: Path) -> Path:
    root = tmp_path / "incidents"
    root.mkdir()
    return root


def _make_app(
    root: Path | str,
    *,
    enabled: bool = True,
    limits: Optional[IngestionLimits] = None,
    repo: Optional[PersistenceRepository] = None,
):
    return create_app(
        repository=repo if repo is not None else FakeRepository(),
        incidents_root=root,
        ingestion_enabled=enabled,
        ingestion_limits=limits,
    )


def _post(client: TestClient, *, incident_id: str, files: Dict[str, str], overwrite: Optional[bool] = None):
    body: Dict[str, Any] = {"incident_id": incident_id, "files": files}
    if overwrite is not None:
        body["overwrite"] = overwrite
    return client.post("/incidents", json=body)


def _run_asgi(
    app: Any,
    path: str,
    *,
    headers: List[Tuple[bytes, bytes]],
    messages: List[Dict[str, Any]],
) -> Tuple[int, bytes]:
    """Drive the ASGI app directly with an explicit ``receive`` message list.

    ``TestClient`` always supplies a truthful ``Content-Length`` (or a chunked
    stream), so this harness is needed to exercise a *misleading* header: the
    ``receive`` messages below carry the real body independently of whatever
    ``Content-Length`` the scope advertises. Returns ``(status, body_bytes)``.
    """
    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode("ascii"),
        "query_string": b"",
        "root_path": "",
        "headers": headers,
        "server": ("testserver", 80),
        "client": ("testclient", 12345),
    }
    pending = list(messages)
    sent: List[Dict[str, Any]] = []

    async def receive():
        if pending:
            return pending.pop(0)
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: Dict[str, Any]):
        sent.append(message)

    asyncio.run(app(scope, receive, send))

    status = next(m["status"] for m in sent if m["type"] == "http.response.start")
    body = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    return status, body


# ===========================================================================
# 1. Disabled-by-default opt-in
# ===========================================================================

def test_ingestion_disabled_by_default(incidents_root: Path):
    """POST /incidents is disabled unless explicitly enabled and writes nothing."""
    app = create_app(repository=FakeRepository(), incidents_root=incidents_root)
    client = TestClient(app)

    resp = _post(client, incident_id="inc_x", files=_valid_files())

    assert resp.status_code == 404
    assert resp.json()["error"] == "ingestion_disabled"
    assert list(incidents_root.iterdir()) == []


def test_ingestion_enabled_via_env(incidents_root: Path, monkeypatch: pytest.MonkeyPatch):
    """SENTINEL_ALLOW_INCIDENT_INGESTION=true enables the endpoint."""
    monkeypatch.setenv("SENTINEL_ALLOW_INCIDENT_INGESTION", "true")
    app = create_app(repository=FakeRepository(), incidents_root=incidents_root)
    client = TestClient(app)

    resp = _post(client, incident_id="inc_env", files=_valid_files())

    assert resp.status_code == 201
    assert resp.json()["incident_id"] == "inc_env"


@pytest.mark.parametrize("value", ["false", "0", "no", "off", "disabled"])
def test_ingestion_env_falsey_leaves_disabled(
    incidents_root: Path, monkeypatch: pytest.MonkeyPatch, value: str
):
    monkeypatch.setenv("SENTINEL_ALLOW_INCIDENT_INGESTION", value)
    app = create_app(repository=FakeRepository(), incidents_root=incidents_root)
    client = TestClient(app)

    resp = _post(client, incident_id="inc_off", files=_valid_files())

    assert resp.status_code == 404
    assert resp.json()["error"] == "ingestion_disabled"
    assert list(incidents_root.iterdir()) == []


# ===========================================================================
# 2. Valid ingestion and exact file contents
# ===========================================================================

def test_valid_ingestion_writes_exact_contents(incidents_root: Path):
    app = _make_app(incidents_root)
    client = TestClient(app)
    files = _valid_files()

    resp = _post(client, incident_id="inc_ok", files=files)

    assert resp.status_code == 201
    data = resp.json()
    assert data["incident_id"] == "inc_ok"
    assert data["bundle_path"] == "incidents/inc_ok"
    assert set(data["evidence_sources"]) == {
        "logs/application.log",
        "metrics/metrics.csv",
        "service",
    }
    assert resp.headers["location"] == "/investigations/inc_ok"

    bundle = incidents_root / "inc_ok"
    for rel_path, content in files.items():
        assert (bundle / rel_path).read_text(encoding="utf-8") == content

    # Only the published bundle exists — staging is fully cleaned up.
    assert [p.name for p in incidents_root.iterdir()] == ["inc_ok"]


def test_ingestion_does_not_start_investigation(incidents_root: Path):
    repo = FakeRepository()
    app = _make_app(incidents_root, repo=repo)
    client = TestClient(app)

    assert _post(client, incident_id="inc_no_start", files=_valid_files()).status_code == 201
    assert not repo.exists("inc_no_start")
    assert repo.save_called == []


# ===========================================================================
# 3. Path policy
# ===========================================================================

@pytest.mark.parametrize(
    "bad_path",
    [
        "../evil.txt",
        "../logs/application.log",
        "logs/../../escape.txt",
        "/etc/passwd",
        "/logs/application.log",
        "C:/windows/system32/config",
        "c:/evil.txt",
        "logs\\application.log",
        "logs/a\x00b.log",
        "logs//application.log",
        "logs/./application.log",
        "logs/../application.log",
        "",
        ".",
        "..",
        "foo/bar.txt",
        "service/",
        "services/app.py",
        "ground_truth.md",
        "ground_truth.json",
        "results_baseline.csv",
        "baseline_summary.json",
        "logs/ground_truth.md",
    ],
)
def test_unsafe_or_disallowed_paths_rejected(incidents_root: Path, bad_path: str):
    app = _make_app(incidents_root)
    client = TestClient(app)
    files = _valid_files()
    files[bad_path] = "data"

    resp = _post(client, incident_id="inc_bad", files=files)

    assert resp.status_code == 400
    assert resp.json()["error"] == "invalid_file_path"
    assert not (incidents_root / "inc_bad").exists()
    assert list(incidents_root.iterdir()) == []


def test_overlong_path_rejected(incidents_root: Path):
    app = _make_app(incidents_root)
    client = TestClient(app)
    files = {"service/" + ("a" * 600): "data"}

    resp = _post(client, incident_id="inc_long", files=files)

    assert resp.status_code == 400
    assert resp.json()["error"] == "invalid_file_path"


def test_overdeep_path_rejected(incidents_root: Path):
    app = _make_app(incidents_root)
    client = TestClient(app)
    deep = "service/" + "/".join(f"d{i}" for i in range(20)) + "/f.py"
    files = {deep: "data"}

    resp = _post(client, incident_id="inc_deep", files=files)

    assert resp.status_code == 400
    assert resp.json()["error"] == "invalid_file_path"


def test_nested_service_files_allowed(incidents_root: Path):
    app = _make_app(incidents_root)
    client = TestClient(app)
    files = _valid_files()
    files["service/pkg/module.py"] = "x = 1\n"

    resp = _post(client, incident_id="inc_nested", files=files)

    assert resp.status_code == 201
    assert (incidents_root / "inc_nested" / "service/pkg/module.py").read_text() == "x = 1\n"


# ===========================================================================
# 4. Malformed requests, invalid IDs, non-string values
# ===========================================================================

def test_empty_files_rejected(incidents_root: Path):
    app = _make_app(incidents_root)
    client = TestClient(app)

    resp = _post(client, incident_id="inc_empty", files={})

    assert resp.status_code == 400
    assert resp.json()["error"] == "invalid_ingest_request"


def test_service_rejects_empty_bundle(incidents_root: Path):
    """The ingestion service itself refuses an empty bundle (defense in depth).

    The request model already rejects it, but the service owns the guarantee:
    a published bundle must be non-empty so the exclusive-publish rename can
    never replace an existing target.
    """
    with pytest.raises(bundle_ingest.MalformedRequestError):
        bundle_ingest.ingest_incident_bundle(incidents_root, "inc_empty_svc", {})

    assert not (incidents_root / "inc_empty_svc").exists()
    assert list(incidents_root.iterdir()) == []


def test_malformed_json_body_rejected(incidents_root: Path):
    app = _make_app(incidents_root)
    client = TestClient(app)

    resp = client.post(
        "/incidents",
        content=b"{not valid json",
        headers={"content-type": "application/json"},
    )

    assert resp.status_code == 400
    assert resp.json()["error"] == "invalid_json"


def test_non_object_json_body_rejected(incidents_root: Path):
    app = _make_app(incidents_root)
    client = TestClient(app)

    resp = client.post(
        "/incidents",
        content=b"[1, 2, 3]",
        headers={"content-type": "application/json"},
    )

    assert resp.status_code == 400
    assert resp.json()["error"] == "invalid_json"


def test_duplicate_json_key_rejected(incidents_root: Path):
    app = _make_app(incidents_root)
    client = TestClient(app)
    raw = (
        b'{"incident_id":"inc_a","incident_id":"inc_b",'
        b'"files":{"logs/application.log":"x"}}'
    )

    resp = client.post(
        "/incidents",
        content=raw,
        headers={"content-type": "application/json"},
    )

    assert resp.status_code == 400
    assert resp.json()["error"] == "invalid_json"
    assert "Duplicate" in resp.json()["detail"]


@pytest.mark.parametrize("bad_id", ["bad/id", "..", "inc id", "inc:1", "a" * 129, "", "   "])
def test_invalid_incident_id_rejected(incidents_root: Path, bad_id: str):
    app = _make_app(incidents_root)
    client = TestClient(app)

    resp = _post(client, incident_id=bad_id, files=_valid_files())

    assert resp.status_code == 400
    assert resp.json()["error"] == "invalid_ingest_request"


def test_non_string_file_content_rejected(incidents_root: Path):
    app = _make_app(incidents_root)
    client = TestClient(app)

    resp = _post(client, incident_id="inc_ty", files={"logs/application.log": 123})

    assert resp.status_code == 400
    assert resp.json()["error"] == "invalid_ingest_request"


def test_unknown_field_rejected(incidents_root: Path):
    app = _make_app(incidents_root)
    client = TestClient(app)
    body = {"incident_id": "inc_extra", "files": _valid_files(), "unexpected": True}

    resp = client.post("/incidents", json=body)

    assert resp.status_code == 400
    assert resp.json()["error"] == "invalid_ingest_request"


def test_non_boolean_overwrite_rejected(incidents_root: Path):
    app = _make_app(incidents_root)
    client = TestClient(app)
    body = {"incident_id": "inc_ow_ty", "files": _valid_files(), "overwrite": "yes"}

    resp = client.post("/incidents", json=body)

    assert resp.status_code == 400
    assert resp.json()["error"] == "invalid_ingest_request"


# ===========================================================================
# 5. Resource limits
# ===========================================================================

def test_per_file_byte_limit_enforced(incidents_root: Path):
    app = _make_app(incidents_root, limits=IngestionLimits(max_file_bytes=10))
    client = TestClient(app)

    resp = _post(client, incident_id="inc_pf", files={"logs/application.log": "x" * 11})

    assert resp.status_code == 413
    assert resp.json()["error"] == "payload_too_large"
    assert list(incidents_root.iterdir()) == []


def test_total_byte_limit_enforced(incidents_root: Path):
    app = _make_app(incidents_root, limits=IngestionLimits(max_total_bytes=20))
    client = TestClient(app)
    files = {"logs/application.log": "a" * 12, "service/a.py": "b" * 12}

    resp = _post(client, incident_id="inc_total", files=files)

    assert resp.status_code == 413
    assert resp.json()["error"] == "payload_too_large"
    assert list(incidents_root.iterdir()) == []


def test_file_count_limit_enforced(incidents_root: Path):
    app = _make_app(incidents_root, limits=IngestionLimits(max_files=2))
    client = TestClient(app)
    files = {"service/a.py": "1", "service/b.py": "2", "service/c.py": "3"}

    resp = _post(client, incident_id="inc_count", files=files)

    assert resp.status_code == 413
    assert resp.json()["error"] == "payload_too_large"
    assert list(incidents_root.iterdir()) == []


def test_multibyte_utf8_counted_by_bytes(incidents_root: Path):
    """6 multi-byte characters are 12 UTF-8 bytes, exceeding a 10-byte limit."""
    app = _make_app(incidents_root, limits=IngestionLimits(max_file_bytes=10))
    client = TestClient(app)

    resp = _post(client, incident_id="inc_mb", files={"logs/application.log": "\u00e9" * 6})

    assert resp.status_code == 413
    assert resp.json()["error"] == "payload_too_large"


def test_multibyte_utf8_within_limit_succeeds(incidents_root: Path):
    app = _make_app(incidents_root, limits=IngestionLimits(max_file_bytes=64))
    client = TestClient(app)
    content = "h\u00e9llo w\u00f6rld\n"

    resp = _post(client, incident_id="inc_mb_ok", files={"logs/application.log": content})

    assert resp.status_code == 201
    assert (incidents_root / "inc_mb_ok" / "logs/application.log").read_text() == content


def test_request_body_limit_enforced_via_content_length(incidents_root: Path):
    app = _make_app(incidents_root, limits=IngestionLimits(max_request_bytes=50))
    client = TestClient(app)
    payload = json.dumps(
        {"incident_id": "inc_big", "files": {"logs/application.log": "x" * 500}}
    )

    resp = client.post(
        "/incidents",
        content=payload.encode("utf-8"),
        headers={"content-type": "application/json"},
    )

    assert resp.status_code == 413
    assert resp.json()["error"] == "payload_too_large"


def test_request_body_limit_enforced_from_parsed_content(incidents_root: Path):
    """The parsed body is size-checked even without a Content-Length header."""
    app = _make_app(incidents_root, limits=IngestionLimits(max_request_bytes=50))
    client = TestClient(app)
    payload = json.dumps(
        {"incident_id": "inc_big2", "files": {"logs/application.log": "x" * 500}}
    ).encode("utf-8")

    def _stream():
        yield payload

    resp = client.post(
        "/incidents",
        content=_stream(),
        headers={"content-type": "application/json"},
    )

    assert resp.status_code == 413
    assert resp.json()["error"] == "payload_too_large"


def test_request_body_limit_enforced_and_no_path_leak_from_parsed_content(incidents_root: Path):
    """The bounded reader enforces the limit while streaming (no Content-Length)."""
    app = _make_app(incidents_root, limits=IngestionLimits(max_request_bytes=50))
    client = TestClient(app)
    payload = json.dumps(
        {"incident_id": "inc_big3", "files": {"logs/application.log": "x" * 500}}
    ).encode("utf-8")

    def _stream():
        # Two chunks: the limit is exceeded mid-stream, long before the end.
        yield payload[:16]
        yield payload[16:]

    resp = client.post(
        "/incidents",
        content=_stream(),
        headers={"content-type": "application/json"},
    )

    assert resp.status_code == 413
    assert resp.json()["error"] == "payload_too_large"
    # The error must not disclose the storage root or any submitted content.
    assert str(incidents_root) not in resp.text
    assert "xxxx" not in resp.text
    assert list(incidents_root.iterdir()) == []


def test_oversized_request_misleading_content_length(incidents_root: Path):
    """A *misleading* small Content-Length cannot bypass the body limit.

    The scope advertises ``Content-Length: 10`` (below the limit) while the
    receive channel delivers a body far larger than the limit, so the early
    header guard passes and the streaming reader must reject it.
    """
    app = _make_app(incidents_root, limits=IngestionLimits(max_request_bytes=50))
    payload = json.dumps(
        {"incident_id": "inc_ml", "files": {"logs/application.log": "x" * 500}}
    ).encode("utf-8")

    status, body = _run_asgi(
        app,
        "/incidents",
        headers=[
            (b"content-type", b"application/json"),
            (b"content-length", b"10"),
        ],
        messages=[{"type": "http.request", "body": payload, "more_body": False}],
    )

    assert status == 413
    assert json.loads(body)["error"] == "payload_too_large"
    assert str(incidents_root).encode() not in body
    assert list(incidents_root.iterdir()) == []


def test_oversized_request_streamed_chunks_without_content_length(incidents_root: Path):
    """A chunked (no Content-Length) oversized body is bounded while reading."""
    app = _make_app(incidents_root, limits=IngestionLimits(max_request_bytes=50))
    payload = json.dumps(
        {"incident_id": "inc_chunked", "files": {"logs/application.log": "x" * 500}}
    ).encode("utf-8")
    midpoint = len(payload) // 2

    status, body = _run_asgi(
        app,
        "/incidents",
        headers=[(b"content-type", b"application/json")],
        messages=[
            {"type": "http.request", "body": payload[:midpoint], "more_body": True},
            {"type": "http.request", "body": payload[midpoint:], "more_body": False},
        ],
    )

    assert status == 413
    assert json.loads(body)["error"] == "payload_too_large"
    assert list(incidents_root.iterdir()) == []


def test_within_limit_request_without_content_length_succeeds(incidents_root: Path):
    """A valid within-limit request (no Content-Length) is preserved end-to-end."""
    app = _make_app(incidents_root)
    payload = json.dumps(
        {"incident_id": "inc_ok_stream", "files": _valid_files()}
    ).encode("utf-8")

    status, body = _run_asgi(
        app,
        "/incidents",
        headers=[(b"content-type", b"application/json")],
        messages=[{"type": "http.request", "body": payload, "more_body": False}],
    )

    assert status == 201
    assert json.loads(body)["incident_id"] == "inc_ok_stream"
    assert (incidents_root / "inc_ok_stream" / "logs/application.log").read_text() == VALID_LOG


# ===========================================================================
# 6. Preflight validation failures & staging cleanup
# ===========================================================================

def test_malformed_metrics_returns_400_and_cleans_staging(incidents_root: Path):
    app = _make_app(incidents_root)
    client = TestClient(app)
    files = {"metrics/metrics.csv": "timestamp,cpu\n2026-08-28T14:00:00+00:00,0.5\n"}

    resp = _post(client, incident_id="inc_badmetrics", files=files)

    assert resp.status_code == 400
    assert resp.json()["error"] == "invalid_incident_bundle"
    assert not (incidents_root / "inc_badmetrics").exists()
    assert list(incidents_root.iterdir()) == []


def test_empty_metrics_file_returns_400_and_cleans_staging(incidents_root: Path):
    app = _make_app(incidents_root)
    client = TestClient(app)

    resp = _post(client, incident_id="inc_emptycsv", files={"metrics/metrics.csv": ""})

    assert resp.status_code == 400
    assert resp.json()["error"] == "invalid_incident_bundle"
    assert list(incidents_root.iterdir()) == []


def test_non_numeric_metric_value_returns_400(incidents_root: Path):
    app = _make_app(incidents_root)
    client = TestClient(app)
    files = {"metrics/metrics.csv": "timestamp,cpu\n2026-08-28T14:00:00Z,high\n"}

    resp = _post(client, incident_id="inc_nonnum", files=files)

    assert resp.status_code == 400
    assert resp.json()["error"] == "invalid_incident_bundle"


# ===========================================================================
# 7. Duplicate / overwrite / preservation
# ===========================================================================

def test_duplicate_incident_conflicts(incidents_root: Path):
    app = _make_app(incidents_root)
    client = TestClient(app)
    assert _post(client, incident_id="inc_dup", files=_valid_files()).status_code == 201

    resp = _post(client, incident_id="inc_dup", files=_valid_files())

    assert resp.status_code == 409
    assert resp.json()["error"] == "conflict"


def test_overwrite_true_is_rejected_with_stable_code(incidents_root: Path):
    """Replacement is unsupported: overwrite=true is a documented, stable 409."""
    app = _make_app(incidents_root)
    client = TestClient(app)

    resp = _post(
        client,
        incident_id="inc_ow_new",
        files={"git_diff.patch": "--- a\n+++ b\n"},
        overwrite=True,
    )

    assert resp.status_code == 409
    assert resp.json()["error"] == "overwrite_not_supported"
    # Rejected before any staging: nothing is written anywhere.
    assert list(incidents_root.iterdir()) == []


def test_overwrite_true_preserves_existing_bundle(incidents_root: Path):
    """A rejected overwrite leaves the previous valid bundle byte-for-byte intact."""
    app = _make_app(incidents_root)
    client = TestClient(app)
    original = _valid_files()
    assert _post(client, incident_id="inc_keep", files=original).status_code == 201
    before = sorted(p.relative_to(incidents_root / "inc_keep").as_posix()
                    for p in (incidents_root / "inc_keep").rglob("*"))

    replacement = {"git_diff.patch": "--- a\n+++ b\n"}
    resp = _post(client, incident_id="inc_keep", files=replacement, overwrite=True)

    assert resp.status_code == 409
    assert resp.json()["error"] == "overwrite_not_supported"
    bundle = incidents_root / "inc_keep"
    for rel_path, content in original.items():
        assert (bundle / rel_path).read_text(encoding="utf-8") == content
    assert not (bundle / "git_diff.patch").exists()
    after = sorted(p.relative_to(bundle).as_posix() for p in bundle.rglob("*"))
    assert after == before
    assert [p.name for p in incidents_root.iterdir()] == ["inc_keep"]


def test_overwrite_true_with_invalid_bundle_still_rejected(incidents_root: Path):
    """overwrite=true is refused before preflight, so an invalid body is a 409 too."""
    app = _make_app(incidents_root)
    client = TestClient(app)
    original = _valid_files()
    assert _post(client, incident_id="inc_keep2", files=original).status_code == 201

    bad = {"metrics/metrics.csv": "timestamp,cpu\nnot-a-timestamp,0.5\n"}
    resp = _post(client, incident_id="inc_keep2", files=bad, overwrite=True)

    assert resp.status_code == 409
    assert resp.json()["error"] == "overwrite_not_supported"
    assert (incidents_root / "inc_keep2" / "logs/application.log").read_text() == \
        original["logs/application.log"]
    assert [p.name for p in incidents_root.iterdir()] == ["inc_keep2"]


# ===========================================================================
# 8. Concurrent ingestion & active-investigation coordination
# ===========================================================================

def test_concurrent_same_id_single_winner(incidents_root: Path):
    app = _make_app(incidents_root)
    client = TestClient(app)
    real_validator = bundle_ingest.require_valid_incident_bundle
    results: List[int] = []

    def slow_validate(*args: Any, **kwargs: Any):
        time.sleep(0.25)
        return real_validator(*args, **kwargs)

    def worker():
        resp = _post(client, incident_id="inc_race", files=_valid_files())
        results.append(resp.status_code)

    with patch.object(bundle_ingest, "require_valid_incident_bundle", side_effect=slow_validate):
        t1 = threading.Thread(target=worker)
        t2 = threading.Thread(target=worker)
        t1.start()
        time.sleep(0.05)
        t2.start()
        t1.join()
        t2.join()

    assert sorted(results) == [201, 409]
    assert [p.name for p in incidents_root.iterdir()] == ["inc_race"]


def _different_content_bundles() -> Tuple[Dict[str, str], Dict[str, str]]:
    """Two bundles that are both valid but contain DIFFERENT evidence."""
    files_a = dict(_valid_files())
    files_a["service/app.py"] = "print('winner A')\n"
    files_b = dict(_valid_files())
    files_b["service/app.py"] = "print('winner B')\n"
    return files_a, files_b


def test_concurrent_same_id_different_content_single_winner(incidents_root: Path):
    """Concurrent same-ID submissions with DIFFERENT valid content: one wins.

    Exactly one request publishes; the other receives a stable ``409 conflict``.
    The winner's bundle is published intact and never mixed with the loser's
    content, and no staging directories leak.
    """
    app = _make_app(incidents_root)
    client = TestClient(app)
    real_validator = bundle_ingest.require_valid_incident_bundle
    files_a, files_b = _different_content_bundles()
    responses: List[Any] = []
    lock = threading.Lock()

    def slow_validate(*args: Any, **kwargs: Any):
        time.sleep(0.3)
        return real_validator(*args, **kwargs)

    def worker(files: Dict[str, str]):
        resp = _post(client, incident_id="inc_race_diff", files=files)
        with lock:
            responses.append(resp)

    with patch.object(bundle_ingest, "require_valid_incident_bundle", side_effect=slow_validate):
        t1 = threading.Thread(target=worker, args=(files_a,))
        t2 = threading.Thread(target=worker, args=(files_b,))
        t1.start()
        time.sleep(0.05)
        t2.start()
        t1.join()
        t2.join()

    assert sorted(r.status_code for r in responses) == [201, 409]
    loser = next(r for r in responses if r.status_code == 409)
    assert loser.json()["error"] == "conflict"

    # Exactly one bundle published; no leaked staging directories.
    assert [p.name for p in incidents_root.iterdir()] == ["inc_race_diff"]
    bundle = incidents_root / "inc_race_diff"
    published = {
        p.relative_to(bundle).as_posix(): p.read_text()
        for p in bundle.rglob("*") if p.is_file()
    }
    # The published bundle exactly equals one submission — never a mixture.
    assert published in (files_a, files_b)
    assert published["service/app.py"] in ("print('winner A')\n", "print('winner B')\n")


def test_cross_process_same_id_exclusive_publish(incidents_root: Path, monkeypatch: pytest.MonkeyPatch):
    """Simulate two independent server processes publishing the same new ID.

    The in-process in-flight registry and lock are process-local and cannot
    coordinate separate server processes, so both are neutralised here (each
    "process" gets its own lock, and the shared registry is bypassed). The only
    remaining coordination is the atomic ``os.rename`` publish: exactly one
    request must win, the loser must get ``IncidentConflictError``, the
    published bundle must match the winner exactly, and nothing may leak.
    """
    incident_id = "inc_xproc"
    files_a, files_b = _different_content_bundles()

    # Two processes ⇒ two independent in-flight registries.
    monkeypatch.setattr(bundle_ingest, "_reserve_ingestion", lambda _id: None)
    monkeypatch.setattr(bundle_ingest, "_release_ingestion", lambda _id: None)

    # Force both racers past the "target exists?" check before either renames,
    # so both attempt the publish and only os.rename arbitrates. Gate only the
    # publish rename so unrelated filesystem calls pass straight through.
    barrier = threading.Barrier(2, timeout=10)
    real_rename = os.rename

    def gated_rename(src: Any, dst: Any) -> None:
        if Path(dst).name == incident_id and Path(dst).parent == incidents_root:
            barrier.wait()
        return real_rename(src, dst)

    monkeypatch.setattr(os, "rename", gated_rename)

    outcomes: List[Tuple[str, Any]] = []
    lock = threading.Lock()

    def worker(files: Dict[str, str]):
        try:
            result = bundle_ingest.ingest_incident_bundle(
                incidents_root, incident_id, files, registry_lock=threading.Lock()
            )
            with lock:
                outcomes.append(("published", result))
        except bundle_ingest.IncidentConflictError:
            with lock:
                outcomes.append(("conflict", None))

    t1 = threading.Thread(target=worker, args=(files_a,))
    t2 = threading.Thread(target=worker, args=(files_b,))
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    assert sorted(kind for kind, _ in outcomes) == ["conflict", "published"]

    # Exactly one bundle, published intact and matching the winner; no leaks.
    assert [p.name for p in incidents_root.iterdir()] == [incident_id]
    bundle = incidents_root / incident_id
    published = {
        p.relative_to(bundle).as_posix(): p.read_text()
        for p in bundle.rglob("*") if p.is_file()
    }
    assert published in (files_a, files_b)


def test_ingestion_refused_while_investigation_active_in_process(incidents_root: Path):
    """Publishing a bundle for an in-process active investigation is vetoed."""
    app = _make_app(incidents_root)
    client = TestClient(app)

    with _ACTIVE_LOCK:
        _ACTIVE_INVESTIGATIONS.add("inc_busy")
    try:
        resp = _post(client, incident_id="inc_busy", files=_valid_files())
    finally:
        with _ACTIVE_LOCK:
            _ACTIVE_INVESTIGATIONS.discard("inc_busy")

    assert resp.status_code == 409
    assert resp.json()["error"] == "conflict"
    assert not (incidents_root / "inc_busy").exists()
    assert list(incidents_root.iterdir()) == []


def test_ingestion_refused_while_incident_actively_leased(incidents_root: Path):
    state = InvestigationState(incident_id="inc_leased", status=IncidentStatus.RUNNING)
    state.acquire_lease(owner_id="worker-1", ttl_seconds=300)
    repo = FakeRepository(states={"inc_leased": state})
    app = _make_app(incidents_root, repo=repo)
    client = TestClient(app)

    resp = _post(client, incident_id="inc_leased", files=_valid_files())

    assert resp.status_code == 409
    assert resp.json()["error"] == "conflict"
    assert not (incidents_root / "inc_leased").exists()


def test_ingestion_allowed_when_no_active_lease(incidents_root: Path):
    """A completed (unleased) investigation does not block a first-time ingestion."""
    state = InvestigationState(incident_id="inc_rel", status=IncidentStatus.COMPLETED)
    repo = FakeRepository(states={"inc_rel": state})
    app = _make_app(incidents_root, repo=repo)
    client = TestClient(app)

    resp = _post(client, incident_id="inc_rel", files=_valid_files())

    assert resp.status_code == 201
    assert (incidents_root / "inc_rel" / "logs/application.log").exists()


# ===========================================================================
# 9. Storage failures & fault injection
# ===========================================================================

def test_unavailable_incidents_root_returns_503(tmp_path: Path):
    app = _make_app(tmp_path / "does-not-exist")
    client = TestClient(app, raise_server_exceptions=False)

    resp = _post(client, incident_id="inc_nostore", files=_valid_files())

    assert resp.status_code == 503
    assert resp.json()["error"] == "storage_unavailable"


def test_injected_write_failure_returns_503_and_cleans_up(incidents_root: Path):
    app = _make_app(incidents_root)
    client = TestClient(app, raise_server_exceptions=False)

    with patch.object(
        bundle_ingest,
        "_write_staged_files",
        side_effect=bundle_ingest.StorageUnavailableError("simulated write failure"),
    ):
        resp = _post(client, incident_id="inc_wfail", files=_valid_files())

    assert resp.status_code == 503
    assert resp.json()["error"] == "storage_unavailable"
    assert list(incidents_root.iterdir()) == []


def test_injected_publish_failure_returns_503_and_cleans_up(incidents_root: Path):
    app = _make_app(incidents_root)
    client = TestClient(app, raise_server_exceptions=False)

    with patch.object(bundle_ingest.os, "rename", side_effect=OSError("simulated commit failure")):
        resp = _post(client, incident_id="inc_pfail", files=_valid_files())

    assert resp.status_code == 503
    assert resp.json()["error"] == "storage_unavailable"
    assert list(incidents_root.iterdir()) == []


def test_duplicate_submission_never_touches_existing_bundle(incidents_root: Path):
    """A duplicate (conflict) leaves the published bundle byte-for-byte unchanged."""
    app = _make_app(incidents_root)
    client = TestClient(app)
    original = _valid_files()
    assert _post(client, incident_id="inc_keep3", files=original).status_code == 201
    bundle = incidents_root / "inc_keep3"
    before = {
        p.relative_to(bundle).as_posix(): p.read_bytes()
        for p in bundle.rglob("*") if p.is_file()
    }

    resp = _post(client, incident_id="inc_keep3", files={"git_diff.patch": "changed"})

    assert resp.status_code == 409
    assert resp.json()["error"] == "conflict"
    after = {
        p.relative_to(bundle).as_posix(): p.read_bytes()
        for p in bundle.rglob("*") if p.is_file()
    }
    assert after == before
    assert [p.name for p in incidents_root.iterdir()] == ["inc_keep3"]


def test_staged_write_rejects_path_escaping_staging_root(incidents_root: Path):
    """Defense in depth: the write helper refuses a path outside the staging root.

    ``validate_bundle_relative_path`` already rejects traversal, but the write
    layer is independently containment-checked, so a path that bypassed the
    policy (e.g. via a future bug) still cannot escape the staging directory.
    """
    staging = incidents_root / ".probe"
    staging.mkdir()

    with pytest.raises(bundle_ingest.InvalidBundlePathError):
        bundle_ingest._write_staged_files(
            staging, staging.resolve(), {"../escape.txt": "x"}
        )

    assert not (incidents_root / "escape.txt").exists()


def test_symlinked_target_is_refused(incidents_root: Path, tmp_path: Path):
    """A symlink at the target path is never followed or replaced."""
    outside = tmp_path / "outside"
    outside.mkdir()
    link = incidents_root / "inc_sym"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation is not permitted in this environment")

    app = _make_app(incidents_root)
    client = TestClient(app)

    resp = _post(client, incident_id="inc_sym", files=_valid_files())

    assert resp.status_code == 409
    assert resp.json()["error"] == "conflict"
    assert list(outside.iterdir()) == []


# ===========================================================================
# 10. No absolute path leakage / no evaluation-file access
# ===========================================================================

def test_no_absolute_path_leak_in_error_response(incidents_root: Path):
    app = _make_app(incidents_root)
    client = TestClient(app)
    bad = {"metrics/metrics.csv": "timestamp,cpu\nnope,1\n"}

    resp = _post(client, incident_id="inc_leak", files=bad)

    assert resp.status_code == 400
    body = resp.text
    assert str(incidents_root) not in body
    assert str(incidents_root.resolve()) not in body
    assert ".ingest-" not in body
    assert "ground_truth" not in body


def test_reserved_evaluation_files_never_written(incidents_root: Path):
    app = _make_app(incidents_root)
    client = TestClient(app)
    files = _valid_files()
    files["ground_truth.md"] = "SECRET_GROUND_TRUTH"

    resp = _post(client, incident_id="inc_res", files=files)

    assert resp.status_code == 400
    assert resp.json()["error"] == "invalid_file_path"
    assert "SECRET_GROUND_TRUTH" not in resp.text
    assert not (incidents_root / "inc_res").exists()
    for path in incidents_root.rglob("*"):
        if path.is_file():
            assert "SECRET_GROUND_TRUTH" not in path.read_text(errors="ignore")


# ===========================================================================
# 11. Existing route compatibility (ingest -> investigate -> result)
# ===========================================================================

def test_health_endpoint_still_works(incidents_root: Path):
    app = _make_app(incidents_root)
    client = TestClient(app)

    resp = client.get("/health")

    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


def test_ingest_then_investigate_workflow(incidents_root: Path):
    """Ingest a bundle, then start an investigation through the existing route."""
    repo = FakeRepository()
    app = _make_app(incidents_root, repo=repo)
    client = TestClient(app)

    assert _post(client, incident_id="inc_flow", files=_valid_files()).status_code == 201

    fake_state = InvestigationState(incident_id="inc_flow", status=IncidentStatus.COMPLETED)
    fake_result = {
        "incident_id": "inc_flow",
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
        instance.state = fake_state
        instance.investigate.return_value = fake_result
        resp = client.post("/investigations", json={"incident_id": "inc_flow"})

    assert resp.status_code == 200
    assert resp.json()["investigation_id"] == "inc_flow"


def test_async_dispatch_still_works(incidents_root: Path):
    repo = FakeRepository()
    app = _make_app(incidents_root, repo=repo)
    client = TestClient(app)

    assert _post(client, incident_id="inc_async", files=_valid_files()).status_code == 201

    with patch("api.routes._run_investigation_background", lambda *a, **k: None):
        resp = client.post(
            "/investigations",
            json={"incident_id": "inc_async"},
            headers={"Prefer": "respond-async"},
        )

    assert resp.status_code == 202
    assert resp.json()["investigation_id"] == "inc_async"


def test_result_retrieval_still_works(incidents_root: Path):
    repo = FakeRepository(
        states={"inc_res": InvestigationState(incident_id="inc_res", status=IncidentStatus.RUNNING)}
    )
    app = _make_app(incidents_root, repo=repo)
    client = TestClient(app)

    resp = client.get("/investigations/inc_res/result")

    assert resp.status_code == 200
    assert resp.json()["investigation_id"] == "inc_res"
    assert resp.json()["result_available"] is False
