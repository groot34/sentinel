"""API tests for optional GitHub repository context on investigation creation.

Mission 27.3.3, Phase 3. These tests cover the API association layer only: the
request contract, connection resolution, ref handling, sync/async parity,
non-fatal GitHub failures, secret hygiene and resume association retention.

Every GitHub network request is served by an ``httpx.MockTransport`` handler, so
no real GitHub account or token is ever used. Tests that exercise the real
orchestrator mock every pipeline agent, so no LLM is called either.
"""

from __future__ import annotations

import base64
import hashlib
import logging
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Optional
from unittest.mock import MagicMock, patch

import httpx
import pytest
from starlette.testclient import TestClient

from agents.fix_tools import HUMAN_APPROVAL_NOTICE
from agents.orchestrator import (
    GITHUB_ASSOCIATION_METADATA_KEY,
    GITHUB_CONTEXT_METADATA_KEY,
    GITHUB_STATUS_FAILURE,
    GitHubContextSource,
)
from api.app import create_app
from api.routes import (
    _ACTIVE_INVESTIGATIONS,
    _ACTIVE_LOCK,
    _build_connection_id,
)
from core.domain.models import IncidentStatus, StageStatus
from core.domain.state import InvestigationState, StageResult
from core.integrations.filesystem_store import FilesystemIntegrationStore
from core.integrations.github_client import ENV_GITHUB_TOKEN, GitHubClient
from core.integrations.repository_store import GitHubConnection
from core.persistence import PersistenceRepository

#: A distinctive, well-formed classic PAT-style token used by the mocked client.
TOKEN = "ghp_" + "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q7R8"

#: The immutable commit SHA every retrieval is pinned to.
COMMIT_SHA = "c0ffee1234567890abcdef1234567890abcdef12"

CONNECTIONS = "/integrations/github/connections"
INVESTIGATIONS = "/investigations"

#: The connection ID the API derives for octo/demo (never hard-coded by hand).
OCTO_DEMO_ID = _build_connection_id("octo", "demo")

#: Source content the shared deterministic detectors flag twice.
PATTERN_SOURCE = "MAX_RETRIES = 7\nTIMEOUT = 60\n"

Handler = Callable[[httpx.Request], httpx.Response]


# ============================================================================
# Repository stub
# ============================================================================

class FakeRepository(PersistenceRepository):
    """Minimal in-memory repository so create_app() has a usable backend."""

    def __init__(self) -> None:
        self._states: Dict[str, InvestigationState] = {}
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


# ============================================================================
# GitHub payloads / mock transport
# ============================================================================

def _repo_payload(**overrides: Any) -> Dict[str, Any]:
    payload = {
        "name": "demo",
        "full_name": "octo/demo",
        "private": True,
        "visibility": "private",
        "default_branch": "main",
        "description": "Demo repository",
        "html_url": "https://github.com/octo/demo",
        "owner": {"login": "octo"},
    }
    payload.update(overrides)
    return payload


def _commit_payload(sha: str = COMMIT_SHA) -> Dict[str, Any]:
    return {
        "sha": sha,
        "commit": {
            "message": "Add auth",
            "author": {"name": "Dev", "date": "2026-01-01T00:00:00Z"},
        },
        "author": {"login": "octo"},
        "html_url": "https://github.com/octo/demo/commit/" + sha,
    }


def _file_payload(path: str, text: str) -> Dict[str, Any]:
    data = text.encode("utf-8")
    return {
        "type": "file",
        "path": path,
        "size": len(data),
        "sha": "blob" + hashlib.sha1(data).hexdigest()[:12],
        "encoding": "base64",
        "content": base64.b64encode(data).decode("ascii"),
    }


def _handler(
    *,
    contents: Optional[Dict[str, str]] = None,
    commit_status: int = 200,
    requests: Optional[List[httpx.Request]] = None,
) -> Handler:
    """MockTransport handler serving repository, commit and content lookups."""
    table = contents or {}

    def handler(request: httpx.Request) -> httpx.Response:
        if requests is not None:
            requests.append(request)
        path = request.url.path
        if path == "/repos/octo/demo":
            return httpx.Response(200, json=_repo_payload())
        if path.endswith("/branches"):
            return httpx.Response(200, json=[{"name": "main", "protected": True, "commit": {"sha": COMMIT_SHA}}])
        if "/commits/" in path:
            if commit_status != 200:
                return httpx.Response(commit_status, json={"message": "error"})
            return httpx.Response(200, json=_commit_payload())
        if "/contents/" in path:
            relative = path.split("/contents/", 1)[1]
            payload = table.get(relative)
            if isinstance(payload, str):
                return httpx.Response(200, json=_file_payload(relative, payload))
            return httpx.Response(404, json={"message": "Not Found"})
        return httpx.Response(404, json={"message": "Not Found"})

    return handler


# ============================================================================
# Orchestrator capture / pipeline stubs
# ============================================================================

def _fake_result(incident_id: str) -> Dict[str, Any]:
    return {
        "incident_id": incident_id,
        "pipeline_status": "COMPLETED",
        "human_approval_notice": HUMAN_APPROVAL_NOTICE,
        "llm_call_count": 0,
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "total_tokens": 0,
        "stages": {},
        "summary": {},
        "error": None,
    }


def _final_state(incident_id: str) -> InvestigationState:
    return InvestigationState(incident_id=incident_id, status=IncidentStatus.COMPLETED)


def _mk_logs(incident_id: str, n: int = 3) -> Dict[str, Any]:
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


def _mk_metrics(incident_id: str, n: int = 2) -> Dict[str, Any]:
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


def _mk_code(incident_id: str, n: int = 2) -> Dict[str, Any]:
    return {
        "incident_id": incident_id,
        "agent": "code_agent",
        "summary": "test code",
        "evidence": [
            {"evidence_id": f"EV-CODE-{i:03d}", "source": "code",
             "reference": f"service/app.py:{i}", "timestamp": "",
             "type": "suspicious_pattern", "excerpt": f"code excerpt {i}",
             "interpretation": f"interp {i}"}
            for i in range(1, n + 1)
        ],
    }


def _mk_hypotheses(incident_id: str) -> Dict[str, Any]:
    return {
        "incident_id": incident_id,
        "hypotheses": [
            {
                "hypothesis_id": "HYP-001",
                "claim": "Retry storm amplifies load.",
                "evidence_ids": ["EV-LOG-001", "EV-CODE-001"],
                "supporting_reasoning": "Retries multiply traffic.",
                "falsification_criteria": ["constant retry count"],
                "verification_plan": ["check retry count"],
            }
        ],
    }


def _mk_verification(incident_id: str) -> Dict[str, Any]:
    return {
        "incident_id": incident_id,
        "results": [
            {
                "hypothesis_id": "HYP-001",
                "verdict": "CONFIRMED",
                "checks": [{"check_id": "CHK-001", "description": "check", "result": "PASS",
                            "evidence": ["EV-LOG-001"], "reference": "service/app.py:22"}],
                "reasoning": "All checks passed.",
                "confidence": 0.9,
            }
        ],
    }


def _mk_fix_proposals(incident_id: str) -> Dict[str, Any]:
    return {
        "incident_id": incident_id,
        "proposals": [
            {
                "proposal_id": "FIX-001",
                "hypothesis_id": "HYP-001",
                "incident_id": incident_id,
                "status": "PROPOSED",
                "human_approval_notice": HUMAN_APPROVAL_NOTICE,
                "summary": "Fix 1",
                "rationale": "rationale",
                "changes": [{"file": "service/app.py", "start_line": None, "end_line": None,
                             "description": "desc", "before": "old", "after": "new"}],
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


@contextmanager
def _pipeline(incident_id: str) -> Iterator[None]:
    """Patch every pipeline agent so a full run never calls an LLM."""
    with (
        patch("agents.orchestrator.LogsAgent") as MockLogs,
        patch("agents.orchestrator.MetricsAgent") as MockMetrics,
        patch("agents.orchestrator.CodeAgent") as MockCode,
        patch("agents.orchestrator.HypothesisEngine") as MockHyp,
        patch("agents.orchestrator.VerificationAgent") as MockVer,
        patch("agents.orchestrator.FixProposalAgent") as MockFix,
        patch("agents.orchestrator.ApprovalGate") as MockGate,
    ):
        MockLogs.return_value.extract_evidence.return_value = _mk_logs(incident_id)
        MockMetrics.return_value.extract_evidence.return_value = _mk_metrics(incident_id)
        MockCode.return_value.extract_evidence.return_value = _mk_code(incident_id)
        MockHyp.return_value.generate_hypotheses.return_value = _mk_hypotheses(incident_id)
        MockVer.return_value.verify.return_value = _mk_verification(incident_id)
        MockFix.return_value.propose_fix.return_value = _mk_fix_proposals(incident_id)
        MockGate.return_value.review_all.return_value = {
            "incident_id": incident_id,
            "approval_records": [],
            "summary": {"total": 0, "approved": 0, "rejected": 0},
        }
        yield


# ============================================================================
# Fixtures
# ============================================================================

@pytest.fixture(autouse=True)
def _no_inherited_token(monkeypatch: pytest.MonkeyPatch):
    """Never let an ambient token leak into the mocked tests."""
    monkeypatch.delenv(ENV_GITHUB_TOKEN, raising=False)


@pytest.fixture(autouse=True)
def _clean_active_registry():
    with _ACTIVE_LOCK:
        _ACTIVE_INVESTIGATIONS.clear()
    yield
    with _ACTIVE_LOCK:
        _ACTIVE_INVESTIGATIONS.clear()


@pytest.fixture
def incidents_dir(tmp_path: Path) -> Path:
    """An incidents root holding two valid bundles with a service/ source file."""
    root = tmp_path / "incidents"
    for name in ("inc_api_01", "inc_api_02"):
        (root / name / "service").mkdir(parents=True)
        (root / name / "service" / "app.py").write_text(PATTERN_SOURCE, encoding="utf-8")
    return root


@pytest.fixture
def build_client(tmp_path: Path):
    """Factory building a TestClient wired to an isolated integration store."""
    created: List[GitHubClient] = []

    def _build(
        handler: Handler,
        *,
        store: Optional[Any] = None,
        token: Any = TOKEN,
        repository: Optional[PersistenceRepository] = None,
        client: Optional[GitHubClient] = None,
        **app_kwargs: Any,
    ) -> TestClient:
        # ``github_client=None`` in app_kwargs models a deployment with the
        # GitHub integration unconfigured: no client is auto-created.
        client_disabled = "github_client" in app_kwargs and app_kwargs["github_client"] is None
        if client is None and not client_disabled:
            client = GitHubClient(token=token, transport=httpx.MockTransport(handler))
        if client is not None:
            created.append(client)
            app_kwargs.setdefault("github_client", client)
        if store is None:
            store = FilesystemIntegrationStore(tmp_path / "persistence")
        if repository is None:
            repository = FakeRepository()
        app = create_app(
            repository=repository,
            integration_store=store,
            **app_kwargs,
        )
        return TestClient(app)

    yield _build

    for client in created:
        client.close()


def _connect(client: TestClient, owner: str = "octo", repo: str = "demo", **extra: Any) -> Any:
    return client.post(CONNECTIONS, json={"owner": owner, "repo": repo, **extra})


def _seed_connection(store: FilesystemIntegrationStore, status: str = "connected") -> GitHubConnection:
    """Seed a connection record directly (no HTTP), for token-less/unusable cases."""
    connection = GitHubConnection(
        connection_id=OCTO_DEMO_ID,
        label="octo/demo",
        owner="octo",
        repo="demo",
        default_branch="main",
        status=status,
    )
    store.create(connection)
    return connection


# ============================================================================
# 1. Backward compatibility — bundle-only creation is unchanged
# ============================================================================

def test_bundle_only_creation_is_unchanged(incidents_dir, build_client) -> None:
    """Without GitHub fields the orchestrator receives no GitHub context."""
    captured: Dict[str, Any] = {}

    with patch("api.routes.IncidentOrchestrator") as MockOrch:
        def capture(**kwargs):
            captured.update(kwargs)
            inst = MagicMock()
            inst.state = _final_state("inc_api_01")
            inst.investigate.return_value = _fake_result("inc_api_01")
            return inst

        MockOrch.side_effect = capture
        client = build_client(_handler(), incidents_root=incidents_dir)
        resp = client.post(INVESTIGATIONS, json={"incident_id": "inc_api_01"})

    assert resp.status_code == 200
    assert resp.json()["pipeline_status"] == "COMPLETED"
    assert captured.get("github_context") is None


def test_bundle_only_creation_does_not_require_github_integration(incidents_dir, build_client, tmp_path) -> None:
    """A deployment without a GitHub client still runs a bundle-only investigation."""
    captured: Dict[str, Any] = {}

    with patch("api.routes.IncidentOrchestrator") as MockOrch:
        def capture(**kwargs):
            captured.update(kwargs)
            inst = MagicMock()
            inst.state = _final_state("inc_api_01")
            inst.investigate.return_value = _fake_result("inc_api_01")
            return inst

        MockOrch.side_effect = capture
        # github_client=None models an unconfigured GitHub integration.
        client = build_client(
            _handler(),
            token=None,
            incidents_root=incidents_dir,
            github_client=None,
        )
        resp = client.post(INVESTIGATIONS, json={"incident_id": "inc_api_01"})

    assert resp.status_code == 200
    assert captured.get("github_context") is None


# ============================================================================
# 2. Request contract / credential rejection
# ============================================================================

@pytest.mark.parametrize(
    "payload",
    [
        {"incident_id": "inc_api_01", "token": TOKEN},
        {"incident_id": "inc_api_01", "github_token": TOKEN},
        {"incident_id": "inc_api_01", "credential": TOKEN},
        {"incident_id": "inc_api_01", "credential_ref": "env:SENTINEL_GITHUB_TOKEN"},
        {"incident_id": "inc_api_01", "github_connection_id": TOKEN},
        {"incident_id": "inc_api_01", "github_connection_id": OCTO_DEMO_ID, "github_ref": TOKEN},
        {"incident_id": "inc_api_01", "github_connection_id": OCTO_DEMO_ID, "github_token": TOKEN},
    ],
)
def test_create_rejects_credential_fields_and_token_values(incidents_dir, build_client, payload) -> None:
    """Unexpected credential fields and token-shaped values are rejected outright."""
    client = build_client(_handler(), incidents_root=incidents_dir)
    resp = client.post(INVESTIGATIONS, json=payload)

    assert resp.status_code == 400
    body = resp.text
    assert TOKEN not in body
    assert "credential_ref" not in body


def test_create_rejects_ref_without_connection(incidents_dir, build_client) -> None:
    """A ref without a connection is ambiguous and rejected."""
    client = build_client(_handler(), incidents_root=incidents_dir)
    resp = client.post(INVESTIGATIONS, json={"incident_id": "inc_api_01", "github_ref": "main"})
    assert resp.status_code == 400
    assert "github_ref" in resp.json()["detail"]


@pytest.mark.parametrize(
    "bad_ref",
    ["bad ref", "../evil", "refs//x", "main.lock", "/leading", "trailing/", "a" * 256],
)
def test_create_rejects_invalid_ref_syntax(incidents_dir, build_client, bad_ref) -> None:
    """Syntactically invalid refs are rejected before any dispatch."""
    client = build_client(_handler(), incidents_root=incidents_dir)

    with patch("api.routes.IncidentOrchestrator") as MockOrch:
        resp = client.post(
            INVESTIGATIONS,
            json={
                "incident_id": "inc_api_01",
                "github_connection_id": OCTO_DEMO_ID,
                "github_ref": bad_ref,
            },
        )

    assert resp.status_code == 400
    MockOrch.assert_not_called()


@pytest.mark.parametrize(
    "bad_id",
    ["", "   ", "gh id", "../evil", "a" * 200, "bad:colon"],
)
def test_create_rejects_invalid_connection_id(incidents_dir, build_client, bad_id) -> None:
    client = build_client(_handler(), incidents_root=incidents_dir)
    resp = client.post(
        INVESTIGATIONS,
        json={"incident_id": "inc_api_01", "github_connection_id": bad_id},
    )
    assert resp.status_code == 400


# ============================================================================
# 3. Connection resolution
# ============================================================================

def test_valid_connection_resolved_and_injected(incidents_dir, build_client) -> None:
    """A valid connection is resolved and injected into the orchestrator."""
    handler = _handler(contents={"service/app.py": PATTERN_SOURCE})
    gh_client = GitHubClient(token=TOKEN, transport=httpx.MockTransport(handler))
    captured: Dict[str, Any] = {}

    client = build_client(handler, client=gh_client, incidents_root=incidents_dir)
    assert _connect(client).status_code == 201

    with patch("api.routes.IncidentOrchestrator") as MockOrch:
        def capture(**kwargs):
            captured.update(kwargs)
            inst = MagicMock()
            inst.state = _final_state("inc_api_01")
            inst.investigate.return_value = _fake_result("inc_api_01")
            return inst

        MockOrch.side_effect = capture
        resp = client.post(
            INVESTIGATIONS,
            json={"incident_id": "inc_api_01", "github_connection_id": OCTO_DEMO_ID},
        )

    assert resp.status_code == 200
    source = captured["github_context"]
    assert isinstance(source, GitHubContextSource)
    # Owner/repo come from the STORED connection, not from client input.
    assert source.connection.connection_id == OCTO_DEMO_ID
    assert source.connection.owner == "octo"
    assert source.connection.repo == "demo"
    # No ref supplied -> the producer default (connection branch) is applied at
    # retrieval time, so the source itself carries no ref.
    assert source.ref is None
    # The server-configured client is reused; no new client is created.
    assert source.client is gh_client


def test_unknown_connection_returns_client_error(incidents_dir, build_client) -> None:
    client = build_client(_handler(), incidents_root=incidents_dir)

    with patch("api.routes.IncidentOrchestrator") as MockOrch:
        resp = client.post(
            INVESTIGATIONS,
            json={"incident_id": "inc_api_01", "github_connection_id": "gh-missing-0000000000"},
        )

    assert resp.status_code == 404
    assert resp.json()["error"] == "connection_not_found"
    MockOrch.assert_not_called()


@pytest.mark.parametrize("status", ["error", "disabled"])
def test_unusable_connection_is_rejected(incidents_dir, build_client, tmp_path, status) -> None:
    """A disconnected/errored connection is an explicit 409, never bundle-only."""
    store = FilesystemIntegrationStore(tmp_path / "persistence")
    _seed_connection(store, status=status)
    client = build_client(_handler(), store=store, incidents_root=incidents_dir)

    with patch("api.routes.IncidentOrchestrator") as MockOrch:
        resp = client.post(
            INVESTIGATIONS,
            json={"incident_id": "inc_api_01", "github_connection_id": OCTO_DEMO_ID},
        )

    assert resp.status_code == 409
    assert resp.json()["error"] == "github_connection_unusable"
    MockOrch.assert_not_called()


def test_integration_unavailable_without_github_client(incidents_dir, build_client) -> None:
    """An unconfigured integration answers 503 instead of silently degrading."""
    client = build_client(_handler(), incidents_root=incidents_dir, github_client=None)

    with patch("api.routes.IncidentOrchestrator") as MockOrch:
        resp = client.post(
            INVESTIGATIONS,
            json={"incident_id": "inc_api_01", "github_connection_id": OCTO_DEMO_ID},
        )

    assert resp.status_code == 503
    assert resp.json()["error"] == "github_integration_unavailable"
    MockOrch.assert_not_called()


# ============================================================================
# 4. Sync / async parity
# ============================================================================

def test_sync_and_async_receive_same_github_context(incidents_dir, build_client) -> None:
    handler = _handler(contents={"service/app.py": PATTERN_SOURCE})
    gh_client = GitHubClient(token=TOKEN, transport=httpx.MockTransport(handler))
    client = build_client(handler, client=gh_client, incidents_root=incidents_dir)
    assert _connect(client).status_code == 201

    payload = {
        "github_connection_id": OCTO_DEMO_ID,
        "github_ref": "release/1.0",
    }
    sync_seen: Dict[str, Any] = {}
    async_seen: Dict[str, Any] = {}

    def _capture(store: Dict[str, Any], method: str):
        def capture(**kwargs):
            store.update(kwargs)
            inst = MagicMock()
            inst.state = _final_state("inc")
            getattr(inst, method).return_value = _fake_result("inc")
            return inst

        return capture

    with patch("api.routes.IncidentOrchestrator") as MockOrch:
        MockOrch.side_effect = _capture(sync_seen, "investigate")
        sync_resp = client.post(
            INVESTIGATIONS, json={"incident_id": "inc_api_01", **payload}
        )

    with patch("api.routes.IncidentOrchestrator") as MockOrch:
        MockOrch.side_effect = _capture(async_seen, "investigate")
        async_resp = client.post(
            INVESTIGATIONS,
            json={"incident_id": "inc_api_02", **payload},
            headers={"Prefer": "respond-async"},
        )

    assert sync_resp.status_code == 200
    assert async_resp.status_code == 202

    sync_source = sync_seen["github_context"]
    async_source = async_seen["github_context"]
    assert isinstance(sync_source, GitHubContextSource)
    assert isinstance(async_source, GitHubContextSource)
    assert sync_source.connection.connection_id == async_source.connection.connection_id == OCTO_DEMO_ID
    assert sync_source.ref == async_source.ref == "release/1.0"
    assert sync_source.client is async_source.client is gh_client


# ============================================================================
# 5. Failure handling
# ============================================================================

def test_missing_server_side_token_handled_safely(incidents_dir, build_client, tmp_path) -> None:
    """A token-less server client fails safely: no request, no credential leak."""
    calls: List[httpx.Request] = []
    store = FilesystemIntegrationStore(tmp_path / "persistence")
    _seed_connection(store)
    repository = FakeRepository()

    client = build_client(
        _handler(requests=calls),
        store=store,
        token=None,  # no server-side SENTINEL_GITHUB_TOKEN
        repository=repository,
        incidents_root=incidents_dir,
    )

    with _pipeline("inc_api_01"):
        resp = client.post(
            INVESTIGATIONS,
            json={"incident_id": "inc_api_01", "github_connection_id": OCTO_DEMO_ID},
        )

    assert resp.status_code == 200
    assert resp.json()["pipeline_status"] == "COMPLETED"
    assert TOKEN not in resp.text
    # No unauthenticated fallback request is ever issued.
    assert calls == []

    snapshot = repository.load("inc_api_01").metadata[GITHUB_CONTEXT_METADATA_KEY]
    assert snapshot["status"] == GITHUB_STATUS_FAILURE
    assert snapshot["error"]["reason"] == "configuration_error"
    assert TOKEN not in str(snapshot)


def test_github_retrieval_failure_does_not_fail_investigation(incidents_dir, build_client) -> None:
    """A GitHub 5xx during retrieval is non-fatal (Phase 2 behaviour)."""
    repository = FakeRepository()
    client = build_client(
        _handler(commit_status=500),
        repository=repository,
        incidents_root=incidents_dir,
    )
    assert _connect(client).status_code == 201

    with _pipeline("inc_api_01"):
        resp = client.post(
            INVESTIGATIONS,
            json={"incident_id": "inc_api_01", "github_connection_id": OCTO_DEMO_ID},
        )

    assert resp.status_code == 200
    assert resp.json()["pipeline_status"] == "COMPLETED"

    snapshot = repository.load("inc_api_01").metadata[GITHUB_CONTEXT_METADATA_KEY]
    assert snapshot["status"] == GITHUB_STATUS_FAILURE
    assert snapshot["error"]["reason"] == "unavailable"
    assert TOKEN not in str(snapshot)


def test_unresolvable_ref_is_non_fatal_and_ref_is_preserved(incidents_dir, build_client) -> None:
    """An accepted ref GitHub cannot resolve never switches branch silently."""
    repository = FakeRepository()
    client = build_client(
        _handler(commit_status=404),
        repository=repository,
        incidents_root=incidents_dir,
    )
    assert _connect(client).status_code == 201

    with _pipeline("inc_api_01"):
        resp = client.post(
            INVESTIGATIONS,
            json={
                "incident_id": "inc_api_01",
                "github_connection_id": OCTO_DEMO_ID,
                "github_ref": "release/nope",
            },
        )

    assert resp.status_code == 200
    snapshot = repository.load("inc_api_01").metadata[GITHUB_CONTEXT_METADATA_KEY]
    assert snapshot["status"] == GITHUB_STATUS_FAILURE
    assert snapshot["error"]["reason"] == "not_found"
    # The requested ref is recorded; the default branch is never substituted.
    assert snapshot["requested_ref"] == "release/nope"


def test_github_enrichment_succeeds_end_to_end(incidents_dir, build_client) -> None:
    """A healthy GitHub connection enriches the successful Code stage."""
    repository = FakeRepository()
    client = build_client(
        _handler(contents={"service/app.py": PATTERN_SOURCE}),
        repository=repository,
        incidents_root=incidents_dir,
    )
    assert _connect(client).status_code == 201

    with _pipeline("inc_api_01"):
        resp = client.post(
            INVESTIGATIONS,
            json={
                "incident_id": "inc_api_01",
                "github_connection_id": OCTO_DEMO_ID,
                "github_ref": "main",
            },
        )

    assert resp.status_code == 200
    snapshot = repository.load("inc_api_01").metadata[GITHUB_CONTEXT_METADATA_KEY]
    assert snapshot["status"] == "success"
    assert snapshot["commit_sha"] == COMMIT_SHA
    assert snapshot["evidence_count"] >= 1
    assert snapshot["requested_ref"] == "main"


# ============================================================================
# 6. Secret hygiene
# ============================================================================

def test_no_token_or_credential_ref_in_response_logs_or_state(
    incidents_dir, build_client, caplog, capsys
) -> None:
    repository = FakeRepository()
    client = build_client(
        _handler(contents={"service/app.py": PATTERN_SOURCE}),
        repository=repository,
        incidents_root=incidents_dir,
    )
    assert _connect(client).status_code == 201

    with caplog.at_level(logging.DEBUG):
        with _pipeline("inc_api_01"):
            resp = client.post(
                INVESTIGATIONS,
                json={
                    "incident_id": "inc_api_01",
                    "github_connection_id": OCTO_DEMO_ID,
                    "github_ref": "main",
                },
            )

    stdout = capsys.readouterr().out
    state = repository.load("inc_api_01")

    for blob in (resp.text, stdout, caplog.text, str(state.metadata)):
        assert TOKEN not in blob, "the GitHub token must never be exposed"
        assert "credential_ref" not in blob
        assert "env:SENTINEL_GITHUB_TOKEN" not in blob

    # The connection projection never carries the credential reference.
    conn_resp = client.get(f"{CONNECTIONS}/{OCTO_DEMO_ID}")
    assert conn_resp.status_code == 200
    assert "credential_ref" not in conn_resp.text
    assert TOKEN not in conn_resp.text
    # The persisted association stores only non-secret identifiers.
    assert state.metadata[GITHUB_ASSOCIATION_METADATA_KEY] == {
        "connection_id": OCTO_DEMO_ID,
        "ref": "main",
    }


# ============================================================================
# 7. Resume consistency
# ============================================================================

def test_resume_retains_original_connection_and_ref(incidents_dir, build_client) -> None:
    """A resumed investigation re-attaches to its original connection and ref."""
    repository = FakeRepository()
    client = build_client(
        _handler(contents={"service/app.py": PATTERN_SOURCE}),
        repository=repository,
        incidents_root=incidents_dir,
    )
    assert _connect(client).status_code == 201

    # Async create persists the association before dispatch.
    with patch("api.routes.IncidentOrchestrator") as MockOrch:
        inst = MagicMock()
        inst.state = _final_state("inc_api_01")
        inst.investigate.return_value = _fake_result("inc_api_01")
        MockOrch.return_value = inst
        created = client.post(
            INVESTIGATIONS,
            json={
                "incident_id": "inc_api_01",
                "github_connection_id": OCTO_DEMO_ID,
                "github_ref": "release/1.0",
            },
            headers={"Prefer": "respond-async"},
        )

    assert created.status_code == 202
    state = repository.load("inc_api_01")
    assert state.metadata[GITHUB_ASSOCIATION_METADATA_KEY] == {
        "connection_id": OCTO_DEMO_ID,
        "ref": "release/1.0",
    }

    # Make the investigation resumable (no active lease).
    state.status = IncidentStatus.FAILED
    state.lease_owner = None
    state.lease_expires_at = None
    repository.save(state)

    captured: Dict[str, Any] = {}

    with patch("api.routes.IncidentOrchestrator") as MockOrch:
        def capture(**kwargs):
            captured.update(kwargs)
            inst = MagicMock()
            inst.state = _final_state("inc_api_01")
            inst.resume.return_value = _fake_result("inc_api_01")
            return inst

        MockOrch.side_effect = capture
        resumed = client.post(f"{INVESTIGATIONS}/inc_api_01/resume")

    assert resumed.status_code == 200
    source = captured["github_context"]
    assert isinstance(source, GitHubContextSource)
    assert source.connection.connection_id == OCTO_DEMO_ID
    # The original requested ref is preserved (not the default branch).
    assert source.ref == "release/1.0"
    assert repository.load("inc_api_01").metadata[GITHUB_ASSOCIATION_METADATA_KEY]["ref"] == "release/1.0"


def test_resume_without_association_stays_bundle_only(incidents_dir, build_client) -> None:
    repository = FakeRepository()
    client = build_client(_handler(), repository=repository, incidents_root=incidents_dir)

    # A bundle-only async create leaves no GitHub association.
    with patch("api.routes.IncidentOrchestrator") as MockOrch:
        inst = MagicMock()
        inst.state = _final_state("inc_api_01")
        inst.investigate.return_value = _fake_result("inc_api_01")
        MockOrch.return_value = inst
        created = client.post(
            INVESTIGATIONS,
            json={"incident_id": "inc_api_01"},
            headers={"Prefer": "respond-async"},
        )
    assert created.status_code == 202

    state = repository.load("inc_api_01")
    assert GITHUB_ASSOCIATION_METADATA_KEY not in state.metadata

    state.status = IncidentStatus.FAILED
    state.lease_owner = None
    state.lease_expires_at = None
    repository.save(state)

    captured: Dict[str, Any] = {}

    with patch("api.routes.IncidentOrchestrator") as MockOrch:
        def capture(**kwargs):
            captured.update(kwargs)
            inst = MagicMock()
            inst.state = _final_state("inc_api_01")
            inst.resume.return_value = _fake_result("inc_api_01")
            return inst

        MockOrch.side_effect = capture
        resumed = client.post(f"{INVESTIGATIONS}/inc_api_01/resume")

    assert resumed.status_code == 200
    assert captured.get("github_context") is None


def test_resume_falls_back_to_phase2_snapshot_metadata(incidents_dir, build_client) -> None:
    """An investigation created before the association field existed still resumes."""
    repository = FakeRepository()
    client = build_client(_handler(), repository=repository, incidents_root=incidents_dir)
    assert _connect(client).status_code == 201

    # Simulate a pre-Phase-3 persisted state: only the Phase 2 snapshot exists.
    state = InvestigationState(incident_id="inc_api_01", status=IncidentStatus.FAILED)
    state.metadata[GITHUB_CONTEXT_METADATA_KEY] = {
        "provider": "github",
        "connection_id": OCTO_DEMO_ID,
        "requested_ref": "develop",
        "status": "success",
    }
    repository.save(state)

    captured: Dict[str, Any] = {}

    with patch("api.routes.IncidentOrchestrator") as MockOrch:
        def capture(**kwargs):
            captured.update(kwargs)
            inst = MagicMock()
            inst.state = _final_state("inc_api_01")
            inst.resume.return_value = _fake_result("inc_api_01")
            return inst

        MockOrch.side_effect = capture
        resumed = client.post(f"{INVESTIGATIONS}/inc_api_01/resume")

    assert resumed.status_code == 200
    source = captured["github_context"]
    assert source.connection.connection_id == OCTO_DEMO_ID
    assert source.ref == "develop"


def test_resume_with_unresolvable_connection_returns_client_error(incidents_dir, build_client, tmp_path) -> None:
    """A resumed association that can no longer be resolved is an explicit error."""
    store = FilesystemIntegrationStore(tmp_path / "persistence")
    repository = FakeRepository()
    client = build_client(_handler(), store=store, repository=repository, incidents_root=incidents_dir)

    state = InvestigationState(incident_id="inc_api_01", status=IncidentStatus.FAILED)
    state.metadata[GITHUB_ASSOCIATION_METADATA_KEY] = {
        "connection_id": OCTO_DEMO_ID,
        "ref": "main",
    }
    repository.save(state)

    with patch("api.routes.IncidentOrchestrator") as MockOrch:
        resp = client.post(f"{INVESTIGATIONS}/inc_api_01/resume")

    assert resp.status_code == 404
    assert resp.json()["error"] == "connection_not_found"
    MockOrch.assert_not_called()
