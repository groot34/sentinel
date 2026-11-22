"""Focused orchestrator tests for optional GitHub context enrichment.

Mission 27.3.3, Phase 2 and Phase 2.1. These tests never touch the network:
every GitHub request is served by an ``httpx.MockTransport`` handler and no real
GitHub token is required. All pipeline agents are mocked, so no LLM is called.

Coverage:
* Bundle-only investigations remain byte-for-byte unchanged.
* Enrichment appends GitHub evidence to the successful Code stage output.
* Bundle-derived evidence is always preserved.
* Evidence IDs continue from the highest existing ``EV-CODE-*`` ID and stay
  unique after fusion.
* REUSED Code-stage results receive enrichment.
* Repeated enrichment never duplicates evidence.
* Rate-limit, authentication, permission and unexpected producer failures
  never fail the Code stage.
* Partial retrieval preserves available evidence and metadata.
* Metadata survives state serialisation and contains no credentials.

Phase 2.1 adds resume/recomputation coverage:
* When a ``metadata["github_context"]`` snapshot already exists, previously
  persisted GitHub evidence is restored into the fusion input instead of being
  dropped by the recomputed fusion.
* Restoring evidence never triggers another GitHub client request.
* A real ``FilesystemRepository`` persistence round-trip followed by ``resume()``
  from ``evidence_fusion`` retains GitHub evidence in the recomputed fusion, and
  repeated resume does not introduce duplicates.
* Malformed Code-stage evidence is handled safely.
"""

from __future__ import annotations

import base64
import hashlib
import json
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Optional
from unittest.mock import patch

import httpx
import pytest

from agents.fix_tools import HUMAN_APPROVAL_NOTICE
from agents.github_context import ERROR_UNEXPECTED
from agents.orchestrator import (
    GITHUB_CONTEXT_METADATA_KEY,
    GITHUB_STATUS_FAILURE,
    GITHUB_STATUS_PARTIAL,
    GITHUB_STATUS_SUCCESS,
    PIPELINE_COMPLETED,
    STATUS_REUSED,
    STATUS_SUCCEEDED,
    GitHubContextSource,
    IncidentOrchestrator,
    _next_code_evidence_index,
    _validate_evidence_fusion,
)
from core.domain.models import IncidentStatus
from core.domain.state import InvestigationState
from core.integrations.github_client import GitHubClient
from core.integrations.repository_store import GitHubConnection
from core.persistence.filesystem import FilesystemRepository

#: A distinctive, well-formed classic PAT-style token used throughout.
TOKEN = "ghp_" + "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q7R8"

#: The immutable commit SHA every retrieval is pinned to.
COMMIT_SHA = "c0ffee1234567890abcdef1234567890abcdef12"

CONNECTION_ID = "gh-octo-demo-0badc0ffee"

#: Source content the shared deterministic detectors flag twice.
PATTERN_SOURCE = "MAX_RETRIES = 7\nTIMEOUT = 60\n"

#: Source content the deterministic detectors do not flag.
CLEAN_SOURCE = "def add(a, b):\n    return a + b\n"

Handler = Callable[[httpx.Request], httpx.Response]


# ============================================================================
# Pipeline fixtures (all agents mocked — no LLM calls)
# ============================================================================

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


def _mk_verification(incident_id: str, verdict: str = "CONFIRMED") -> Dict[str, Any]:
    return {
        "incident_id": incident_id,
        "results": [
            {
                "hypothesis_id": "HYP-001",
                "verdict": verdict,
                "checks": [{"check_id": "CHK-001", "description": "check", "result": "PASS",
                            "evidence": ["EV-LOG-001"], "reference": "service/app.py:22"}],
                "reasoning": "All checks passed.",
                "confidence": 0.9,
            }
        ],
    }


def _mk_fix_proposals(incident_id: str, n: int = 1) -> Dict[str, Any]:
    proposals = []
    for i in range(1, n + 1):
        proposals.append({
            "proposal_id": f"FIX-{i:03d}",
            "hypothesis_id": "HYP-001",
            "incident_id": incident_id,
            "status": "PROPOSED",
            "human_approval_notice": HUMAN_APPROVAL_NOTICE,
            "summary": f"Fix {i}",
            "rationale": "rationale",
            "changes": [{"file": "service/app.py", "start_line": None, "end_line": None,
                         "description": "desc", "before": "old", "after": "new"}],
            "patch": "--- a/service/app.py\n+++ b/service/app.py\n@@ -1 +1 @@\n-old\n+new",
            "expected_effect": "better",
            "risks": [],
            "validation_plan": ["run tests"],
            "rollback_plan": "revert",
            "evidence_ids": ["EV-LOG-001"],
        })
    return {
        "incident_id": incident_id,
        "proposals": proposals,
        "validation_errors": {},
        "skipped_hypotheses": [],
    }


@contextmanager
def _pipeline(incident_id: str, *, code: Optional[Dict[str, Any]] = None) -> Iterator[None]:
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
        MockCode.return_value.extract_evidence.return_value = code or _mk_code(incident_id)
        MockHyp.return_value.generate_hypotheses.return_value = _mk_hypotheses(incident_id)
        MockVer.return_value.verify.return_value = _mk_verification(incident_id)
        MockFix.return_value.propose_fix.return_value = _mk_fix_proposals(incident_id, 1)
        MockGate.return_value.review_all.return_value = {
            "incident_id": incident_id,
            "approval_records": [],
            "summary": {"total": 0, "approved": 0, "rejected": 0},
        }
        yield


# ============================================================================
# Bundle + GitHub fixtures
# ============================================================================

def _bundle(
    tmp_path: Path,
    *,
    service: Optional[Dict[str, str]] = None,
    diff: Optional[str] = None,
    name: str = "inc_gh_test",
) -> Path:
    root = tmp_path / name
    (root / "service").mkdir(parents=True, exist_ok=True)
    for filename, text in (service or {"app.py": PATTERN_SOURCE}).items():
        (root / "service" / filename).write_text(text, encoding="utf-8")
    if diff is not None:
        (root / "git_diff.patch").write_text(diff, encoding="utf-8")
    return root


def _connection(default_branch: str = "main") -> GitHubConnection:
    return GitHubConnection(
        connection_id=CONNECTION_ID,
        label="demo",
        owner="octo",
        repo="demo",
        default_branch=default_branch,
    )


def _source(client: Any, *, ref: Optional[str] = None) -> GitHubContextSource:
    return GitHubContextSource(client=client, connection=_connection(), ref=ref)


def _commit_payload(sha: str = COMMIT_SHA) -> Dict[str, Any]:
    return {
        "sha": sha,
        "commit": {"message": "Add auth", "author": {"name": "Dev", "date": "2026-01-01T00:00:00Z"}},
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


def _client(handler: Handler) -> GitHubClient:
    return GitHubClient(token=TOKEN, transport=httpx.MockTransport(handler))


def _handler(
    files: Optional[Dict[str, str]] = None,
    *,
    requests: Optional[List[httpx.Request]] = None,
    commit_status: int = 200,
    contents_status: Optional[Callable[[str], Optional[int]]] = None,
) -> Handler:
    """Build a MockTransport handler serving a repository revision and files."""
    table = files or {}

    def handler(request: httpx.Request) -> httpx.Response:
        if requests is not None:
            requests.append(request)
        path = request.url.path
        if "/commits/" in path:
            if commit_status != 200:
                return httpx.Response(commit_status, json={"message": "error"})
            return httpx.Response(200, json=_commit_payload())
        if "/contents/" in path:
            relative = path.split("/contents/", 1)[1]
            if contents_status is not None:
                status = contents_status(relative)
                if status is not None:
                    return httpx.Response(status, json={"message": "error"})
            payload = table.get(relative)
            if isinstance(payload, str):
                return httpx.Response(200, json=_file_payload(relative, payload))
            return httpx.Response(404, json={"message": "Not Found"})
        return httpx.Response(404, json={"message": "Not Found"})

    return handler


class _ExplodingClient:
    """A client whose revision lookup always fails unexpectedly."""

    def get_commit(self, *args: Any, **kwargs: Any) -> Any:
        raise RuntimeError(f"boom {TOKEN}")

    def get_file_contents(self, *args: Any, **kwargs: Any) -> Any:  # pragma: no cover
        raise AssertionError("get_file_contents must not be reached")


def _fused(result: Dict[str, Any]) -> Dict[str, Any]:
    return result["stages"]["evidence_fusion"]["output"]


def _github_evidence(result: Dict[str, Any]) -> List[Dict[str, Any]]:
    return [ev for ev in _fused(result)["evidence"] if "github" in ev]


def _contents_requests(requests: List[httpx.Request]) -> List[httpx.Request]:
    return [r for r in requests if "/contents/" in r.url.path]


# ============================================================================
# 1. Bundle-only behaviour is unchanged
# ============================================================================

def test_bundle_only_investigation_is_unchanged_without_github_context(tmp_path: Path):
    bundle = _bundle(tmp_path)
    orch = IncidentOrchestrator(non_interactive=True)
    with _pipeline(bundle.name):
        result = orch.investigate(bundle)

    fused = _fused(result)
    ids = {ev["evidence_id"] for ev in fused["evidence"]}
    assert ids == {
        "EV-LOG-001", "EV-LOG-002", "EV-LOG-003",
        "EV-MET-001", "EV-MET-002",
        "EV-CODE-001", "EV-CODE-002",
    }
    assert all("github" not in ev for ev in fused["evidence"])
    assert GITHUB_CONTEXT_METADATA_KEY not in orch.last_state.metadata
    assert result["pipeline_status"] == PIPELINE_COMPLETED


def test_producer_is_never_called_without_github_context(tmp_path: Path):
    bundle = _bundle(tmp_path)
    # An exploding client would raise if the orchestrator ever called it.
    orch = IncidentOrchestrator(non_interactive=True, github_context=None)
    with _pipeline(bundle.name):
        result = orch.investigate(bundle)
    assert _github_evidence(result) == []
    assert GITHUB_CONTEXT_METADATA_KEY not in orch.last_state.metadata


# ============================================================================
# 2-3. Enrichment appends evidence; bundle evidence is preserved
# ============================================================================

def test_github_enrichment_adds_evidence_to_successful_code_output(tmp_path: Path):
    bundle = _bundle(tmp_path, service={"app.py": PATTERN_SOURCE})
    client = _client(_handler({"service/app.py": PATTERN_SOURCE}))
    orch = IncidentOrchestrator(non_interactive=True, github_context=_source(client))

    with _pipeline(bundle.name):
        result = orch.investigate(bundle)

    github_items = _github_evidence(result)
    assert len(github_items) == 2
    assert result["stages"]["code"]["status"] == STATUS_SUCCEEDED
    assert result["pipeline_status"] == PIPELINE_COMPLETED

    provenance = github_items[0]["github"]
    assert provenance["owner"] == "octo"
    assert provenance["repo"] == "demo"
    assert provenance["connection_id"] == CONNECTION_ID
    assert provenance["ref"] == "main"
    assert provenance["commit_sha"] == COMMIT_SHA
    assert provenance["path"] == "service/app.py"
    assert provenance["source_url"].startswith(
        f"https://github.com/octo/demo/blob/{COMMIT_SHA}/service/app.py"
    )


def test_bundle_evidence_is_preserved(tmp_path: Path):
    bundle = _bundle(tmp_path)
    client = _client(_handler({"service/app.py": PATTERN_SOURCE}))
    orch = IncidentOrchestrator(non_interactive=True, github_context=_source(client))

    with _pipeline(bundle.name):
        result = orch.investigate(bundle)

    ids = {ev["evidence_id"] for ev in _fused(result)["evidence"]}
    assert {"EV-CODE-001", "EV-CODE-002", "EV-LOG-001", "EV-MET-001"} <= ids
    state_ids = {ev.evidence_id for ev in orch.last_state.evidence}
    assert {"EV-CODE-001", "EV-CODE-002"} <= state_ids


def test_cached_code_stage_output_does_not_contain_github_evidence(tmp_path: Path):
    bundle = _bundle(tmp_path)
    cache_root = tmp_path / "stage-cache"
    client = _client(_handler({"service/app.py": PATTERN_SOURCE}))
    orch = IncidentOrchestrator(
        non_interactive=True, output_dir=cache_root, github_context=_source(client)
    )
    with _pipeline(bundle.name):
        result = orch.investigate(bundle)

    assert _github_evidence(result), "enrichment should have produced evidence"
    # The persisted Code-stage output (state + on-disk cache) stays bundle-only.
    stage_evidence = result["stages"]["code"]["output"]["evidence"]
    assert all("github" not in ev for ev in stage_evidence)
    cache_file = cache_root / bundle.name / "code.json"
    assert cache_file.is_file()
    cached = json.loads(cache_file.read_text(encoding="utf-8"))
    assert all("github" not in ev for ev in cached["evidence"])


# ============================================================================
# 4-5. Evidence ID allocation and uniqueness
# ============================================================================

def test_evidence_ids_continue_from_highest_bundle_id(tmp_path: Path):
    bundle = _bundle(tmp_path)
    code = _mk_code(bundle.name, n=5)
    client = _client(_handler({"service/app.py": PATTERN_SOURCE}))
    orch = IncidentOrchestrator(non_interactive=True, github_context=_source(client))

    with _pipeline(bundle.name, code=code):
        result = orch.investigate(bundle)

    ids = [ev["evidence_id"] for ev in _github_evidence(result)]
    assert ids == ["EV-CODE-006", "EV-CODE-007"]
    assert _next_code_evidence_index(code) == 6


def test_evidence_ids_remain_unique_after_fusion(tmp_path: Path):
    bundle = _bundle(tmp_path)
    client = _client(_handler({"service/app.py": PATTERN_SOURCE}))
    orch = IncidentOrchestrator(non_interactive=True, github_context=_source(client))

    with _pipeline(bundle.name):
        result = orch.investigate(bundle)

    fused = _fused(result)
    assert _validate_evidence_fusion(fused) == []
    ids = [ev["evidence_id"] for ev in fused["evidence"]]
    assert len(ids) == len(set(ids))


def test_next_code_evidence_index_ignores_non_code_and_missing_ids():
    bundle = {
        "evidence": [
            {"evidence_id": "EV-LOG-042"},
            {"evidence_id": "EV-CODE-007"},
            {"evidence_id": "EV-MET-099"},
            {"evidence_id": "not-an-id"},
            {},
        ]
    }
    assert _next_code_evidence_index(bundle) == 8
    assert _next_code_evidence_index(None, []) == 1


# ============================================================================
# 6-7. REUSED stages and idempotency
# ============================================================================

def test_reused_code_stage_result_receives_enrichment(tmp_path: Path):
    bundle = _bundle(tmp_path)
    cache_root = tmp_path / "stage-cache"
    cache_dir = cache_root / bundle.name
    cache_dir.mkdir(parents=True, exist_ok=True)
    (cache_dir / "code.json").write_text(
        json.dumps(_mk_code(bundle.name)), encoding="utf-8"
    )

    client = _client(_handler({"service/app.py": PATTERN_SOURCE}))
    orch = IncidentOrchestrator(
        non_interactive=True, output_dir=cache_root, github_context=_source(client)
    )
    with _pipeline(bundle.name):
        result = orch.investigate(bundle)

    assert result["stages"]["code"]["status"] == STATUS_REUSED
    assert [ev["evidence_id"] for ev in _github_evidence(result)] == [
        "EV-CODE-003", "EV-CODE-004",
    ]


def test_repeated_enrichment_does_not_duplicate_evidence(tmp_path: Path):
    bundle = _bundle(tmp_path)
    requests: List[httpx.Request] = []
    client = _client(_handler({"service/app.py": PATTERN_SOURCE}, requests=requests))
    orch = IncidentOrchestrator(non_interactive=True, github_context=_source(client))

    state = InvestigationState(incident_id=bundle.name)
    code = _mk_code(bundle.name)

    enriched, recorded = orch._enrich_code_evidence_with_github(bundle, code, state)
    assert recorded is True
    assert len(_github_evidence({"stages": {"evidence_fusion": {"output": {
        "evidence": enriched["evidence"]}}}})) == 2
    evidence_count = len(state.evidence)
    request_count = len(requests)

    again, recorded_again = orch._enrich_code_evidence_with_github(bundle, enriched, state)

    assert recorded_again is False
    assert again is enriched
    assert len(state.evidence) == evidence_count
    assert len(requests) == request_count


# ============================================================================
# 8-10. GitHub failures never fail the Code stage
# ============================================================================

def _assert_graceful_failure(bundle: Path, client: Any) -> Dict[str, Any]:
    orch = IncidentOrchestrator(non_interactive=True, github_context=_source(client))
    with _pipeline(bundle.name):
        result = orch.investigate(bundle)

    assert result["stages"]["code"]["status"] == STATUS_SUCCEEDED
    assert result["pipeline_status"] == PIPELINE_COMPLETED
    snapshot = orch.last_state.metadata[GITHUB_CONTEXT_METADATA_KEY]
    assert snapshot["status"] == GITHUB_STATUS_FAILURE
    assert _github_evidence(result) == []
    # Bundle evidence survives untouched.
    assert {ev["evidence_id"] for ev in _fused(result)["evidence"]} >= {
        "EV-CODE-001", "EV-CODE-002",
    }
    return snapshot


def test_rate_limit_does_not_fail_code_stage_and_stops_retrieval(tmp_path: Path):
    bundle = _bundle(tmp_path, service={"a.py": PATTERN_SOURCE, "b.py": PATTERN_SOURCE})
    requests: List[httpx.Request] = []
    client = _client(
        _handler(
            {"service/a.py": PATTERN_SOURCE, "service/b.py": PATTERN_SOURCE},
            requests=requests,
            contents_status=lambda _rel: 429,
        )
    )
    snapshot = _assert_graceful_failure(bundle, client)

    assert snapshot["error"]["reason"] == "rate_limited"
    # Retrieval stops immediately after the first rate-limit error.
    assert len(_contents_requests(requests)) == 1


def test_authentication_failure_does_not_fail_code_stage(tmp_path: Path):
    bundle = _bundle(tmp_path)
    client = _client(_handler({"service/app.py": PATTERN_SOURCE}, commit_status=401))
    snapshot = _assert_graceful_failure(bundle, client)
    assert snapshot["error"]["reason"] == "authentication_failed"


def test_permission_failure_does_not_fail_code_stage(tmp_path: Path):
    bundle = _bundle(tmp_path)
    client = _client(_handler({"service/app.py": PATTERN_SOURCE}, commit_status=403))
    snapshot = _assert_graceful_failure(bundle, client)
    assert snapshot["error"]["reason"] == "permission_denied"


def test_missing_repository_does_not_fail_code_stage(tmp_path: Path):
    bundle = _bundle(tmp_path)
    client = _client(_handler({"service/app.py": PATTERN_SOURCE}, commit_status=404))
    snapshot = _assert_graceful_failure(bundle, client)
    assert snapshot["error"]["reason"] == "not_found"


def test_unexpected_producer_exception_does_not_fail_code_stage(tmp_path: Path):
    bundle = _bundle(tmp_path)
    snapshot = _assert_graceful_failure(bundle, _ExplodingClient())
    assert snapshot["status"] == GITHUB_STATUS_FAILURE
    assert snapshot["error"]["reason"] == ERROR_UNEXPECTED
    # Diagnostics are preserved but the credential is redacted.
    assert TOKEN not in snapshot["error"]["message"]
    assert "RuntimeError" in snapshot["error"]["message"]


# ============================================================================
# 11. Partial retrieval
# ============================================================================

def test_partial_retrieval_preserves_available_evidence_and_metadata(tmp_path: Path):
    bundle = _bundle(tmp_path, service={"app.py": PATTERN_SOURCE, "missing.py": CLEAN_SOURCE})
    client = _client(_handler({"service/app.py": PATTERN_SOURCE}))
    orch = IncidentOrchestrator(non_interactive=True, github_context=_source(client))

    with _pipeline(bundle.name):
        result = orch.investigate(bundle)

    snapshot = orch.last_state.metadata[GITHUB_CONTEXT_METADATA_KEY]
    assert snapshot["status"] == GITHUB_STATUS_PARTIAL
    assert snapshot["evidence_count"] == 2
    assert snapshot["retrieved_file_count"] == 1
    assert [s["path"] for s in snapshot["skipped"]] == ["service/missing.py"]
    assert snapshot["skipped"][0]["reason"] == "missing"
    assert len(_github_evidence(result)) == 2


def test_successful_enrichment_records_revision_and_counts(tmp_path: Path):
    bundle = _bundle(tmp_path)
    client = _client(_handler({"service/app.py": PATTERN_SOURCE}))
    orch = IncidentOrchestrator(
        non_interactive=True, github_context=_source(client, ref="release/1.0")
    )
    with _pipeline(bundle.name):
        orch.investigate(bundle)

    snapshot = orch.last_state.metadata[GITHUB_CONTEXT_METADATA_KEY]
    assert snapshot["status"] == GITHUB_STATUS_SUCCESS
    assert snapshot["requested_ref"] == "release/1.0"
    assert snapshot["commit_sha"] == COMMIT_SHA
    assert snapshot["retrieved_file_count"] == 1
    assert snapshot["evidence_count"] == 2
    assert snapshot["truncated"] is False
    assert snapshot["skipped"] == []
    assert snapshot["retrieved_at"]


# ============================================================================
# 12-13. Serialisation and credential hygiene
# ============================================================================

def test_metadata_survives_state_serialisation(tmp_path: Path):
    bundle = _bundle(tmp_path)
    client = _client(_handler({"service/app.py": PATTERN_SOURCE}))
    orch = IncidentOrchestrator(non_interactive=True, github_context=_source(client))
    with _pipeline(bundle.name):
        orch.investigate(bundle)

    state = orch.last_state
    restored = InvestigationState.from_dict(json.loads(json.dumps(state.to_dict())))
    assert restored.metadata[GITHUB_CONTEXT_METADATA_KEY] == state.metadata[GITHUB_CONTEXT_METADATA_KEY]


def test_no_token_or_credential_enters_investigation_state(tmp_path: Path):
    bundle = _bundle(tmp_path)

    for client in (
        _client(_handler({"service/app.py": PATTERN_SOURCE})),
        _client(_handler({"service/app.py": PATTERN_SOURCE}, commit_status=401)),
        _ExplodingClient(),
    ):
        orch = IncidentOrchestrator(non_interactive=True, github_context=_source(client))
        with _pipeline(bundle.name):
            orch.investigate(bundle)
        serialised = json.dumps(orch.last_state.to_dict())
        assert TOKEN not in serialised
        assert "credential_ref" not in serialised


# ============================================================================
# Phase 2.1 — Restoring persisted GitHub evidence on resume / recomputation
# ============================================================================

def _github_item(evidence_id: str, path: str = "service/app.py") -> Dict[str, Any]:
    """A previously retrieved GitHub evidence item, as persisted in state."""
    return {
        "evidence_id": evidence_id,
        "source": "code",
        "reference": f"{path}:1",
        "timestamp": "",
        "type": "suspicious_pattern",
        "excerpt": "github excerpt",
        "interpretation": "github interp",
        "github": {
            "owner": "octo",
            "repo": "demo",
            "connection_id": CONNECTION_ID,
            "ref": "main",
            "commit_sha": COMMIT_SHA,
            "path": path,
            "source_url": f"https://github.com/octo/demo/blob/{COMMIT_SHA}/{path}",
            "retrieved_at": "2026-01-01T00:00:00Z",
            "content_hash": "sha256:" + "0" * 64,
        },
    }


def _snapshot() -> Dict[str, Any]:
    """A previously recorded GitHub context snapshot (successful retrieval)."""
    return {
        "provider": "github",
        "connection_id": CONNECTION_ID,
        "owner": "octo",
        "repo": "demo",
        "requested_ref": "main",
        "commit_sha": COMMIT_SHA,
        "retrieved_at": "2026-01-01T00:00:00Z",
        "retrieved_file_count": 1,
        "evidence_count": 2,
        "skipped": [],
        "truncated": False,
        "truncation_reason": None,
        "total_bytes": 64,
        "status": GITHUB_STATUS_SUCCESS,
        "error": None,
    }


def _rewind_to_evidence_fusion(repo: FilesystemRepository, incident_id: str) -> None:
    """Drop the fusion stage and everything downstream, as a crash would."""
    state = repo.load(incident_id)
    assert state is not None
    for stage in (
        "evidence_fusion", "hypotheses", "verification", "fix_proposals", "approvals",
    ):
        state.stages.pop(stage, None)
    state.status = IncidentStatus.RUNNING
    state.current_stage = None
    state.release_lease()
    repo.save(state)


def test_fresh_enrichment_still_records_snapshot_and_evidence(tmp_path: Path):
    """Requirement: fresh GitHub enrichment still works."""
    bundle = _bundle(tmp_path)
    requests: List[httpx.Request] = []
    client = _client(_handler({"service/app.py": PATTERN_SOURCE}, requests=requests))
    orch = IncidentOrchestrator(non_interactive=True, github_context=_source(client))

    state = InvestigationState(incident_id=bundle.name)
    code = _mk_code(bundle.name)

    enriched, recorded = orch._enrich_code_evidence_with_github(bundle, code, state)

    assert recorded is True
    assert state.metadata[GITHUB_CONTEXT_METADATA_KEY]["status"] == GITHUB_STATUS_SUCCESS
    assert [ev["evidence_id"] for ev in enriched["evidence"]] == [
        "EV-CODE-001", "EV-CODE-002", "EV-CODE-003", "EV-CODE-004",
    ]
    # The fresh path fetched exactly once.
    assert len(_contents_requests(requests)) == 1


def test_snapshot_restores_persisted_github_evidence_into_fusion_input(tmp_path: Path):
    """Requirement: persisted GitHub evidence is restored when a snapshot exists."""
    bundle = _bundle(tmp_path)
    requests: List[httpx.Request] = []
    client = _client(_handler({"service/app.py": PATTERN_SOURCE}, requests=requests))
    orch = IncidentOrchestrator(non_interactive=True, github_context=_source(client))

    state = InvestigationState(incident_id=bundle.name)
    state.add_evidence([_github_item("EV-CODE-003"), _github_item("EV-CODE-004")])
    state.metadata[GITHUB_CONTEXT_METADATA_KEY] = _snapshot()

    bundle_output = _mk_code(bundle.name)  # EV-CODE-001, EV-CODE-002 (bundle-derived)
    enriched, recorded = orch._enrich_code_evidence_with_github(bundle, bundle_output, state)

    assert recorded is False
    assert [ev["evidence_id"] for ev in enriched["evidence"]] == [
        "EV-CODE-001", "EV-CODE-002", "EV-CODE-003", "EV-CODE-004",
    ]
    # Only GitHub-provenance items are restored; bundle evidence is intact.
    restored = enriched["evidence"][2:]
    assert all("github" in ev for ev in restored)
    # The original persisted Code-stage output object is never mutated.
    assert [ev["evidence_id"] for ev in bundle_output["evidence"]] == [
        "EV-CODE-001", "EV-CODE-002",
    ]
    # Restoring does not duplicate persisted state evidence.
    assert len(state.evidence) == 2


def test_restoring_evidence_does_not_trigger_another_github_request(tmp_path: Path):
    """Requirement: restoring evidence makes no additional GitHub API calls."""
    bundle = _bundle(tmp_path)
    requests: List[httpx.Request] = []
    client = _client(_handler({"service/app.py": PATTERN_SOURCE}, requests=requests))
    orch = IncidentOrchestrator(non_interactive=True, github_context=_source(client))
    # Prime the client so a request *would* be visible if it were made.
    client.get_commit("octo", "demo", COMMIT_SHA)
    assert len(requests) == 1

    state = InvestigationState(incident_id=bundle.name)
    state.add_evidence([_github_item("EV-CODE-003"), _github_item("EV-CODE-004")])
    state.metadata[GITHUB_CONTEXT_METADATA_KEY] = _snapshot()

    request_count = len(requests)
    _, recorded = orch._enrich_code_evidence_with_github(bundle, _mk_code(bundle.name), state)
    assert recorded is False
    assert len(requests) == request_count


def test_restore_path_never_calls_the_github_client(tmp_path: Path):
    """An exploding client proves the restore path never touches the network."""
    bundle = _bundle(tmp_path)
    orch = IncidentOrchestrator(non_interactive=True, github_context=_source(_ExplodingClient()))

    state = InvestigationState(incident_id=bundle.name)
    state.add_evidence([_github_item("EV-CODE-003")])
    snapshot = _snapshot()
    state.metadata[GITHUB_CONTEXT_METADATA_KEY] = snapshot

    enriched, recorded = orch._enrich_code_evidence_with_github(bundle, _mk_code(bundle.name), state)

    assert recorded is False
    assert [ev["evidence_id"] for ev in enriched["evidence"]] == [
        "EV-CODE-001", "EV-CODE-002", "EV-CODE-003",
    ]
    # The existing snapshot is not re-derived or overwritten.
    assert state.metadata[GITHUB_CONTEXT_METADATA_KEY] is snapshot


def test_snapshot_without_github_evidence_leaves_bundle_unchanged(tmp_path: Path):
    """Bundle-only state with a snapshot restores nothing and stays unchanged."""
    bundle = _bundle(tmp_path)
    orch = IncidentOrchestrator(non_interactive=True, github_context=_source(_ExplodingClient()))

    state = InvestigationState(incident_id=bundle.name)
    # Bundle-derived evidence shares the EV-CODE- prefix but has no provenance.
    state.add_evidence([
        {"evidence_id": "EV-CODE-001", "source": "code", "reference": "service/app.py:1",
         "timestamp": "", "type": "suspicious_pattern", "excerpt": "x", "interpretation": "y"},
    ])
    state.metadata[GITHUB_CONTEXT_METADATA_KEY] = _snapshot()

    code = _mk_code(bundle.name)
    enriched, recorded = orch._enrich_code_evidence_with_github(bundle, code, state)

    assert recorded is False
    assert enriched is code


@pytest.mark.parametrize(
    "evidence_value",
    ["not-a-list", 42, {"nested": "mapping"}, ["valid", 3], ["valid", None]],
)
def test_malformed_code_evidence_is_handled_safely(tmp_path: Path, evidence_value: Any):
    """Requirement: malformed evidence is never coerced into evidence items."""
    bundle = _bundle(tmp_path)
    requests: List[httpx.Request] = []
    client = _client(_handler({"service/app.py": PATTERN_SOURCE}, requests=requests))
    orch = IncidentOrchestrator(non_interactive=True, github_context=_source(client))

    state = InvestigationState(incident_id=bundle.name)
    malformed = {"incident_id": bundle.name, "agent": "code_agent", "evidence": evidence_value}

    enriched, recorded = orch._enrich_code_evidence_with_github(bundle, malformed, state)

    assert recorded is False
    assert enriched is malformed
    assert malformed["evidence"] == evidence_value  # untouched, not coerced
    assert GITHUB_CONTEXT_METADATA_KEY not in state.metadata
    assert requests == []


def test_resume_from_evidence_fusion_restores_github_evidence(tmp_path: Path):
    """Requirement: a real persistence round-trip + resume retains GitHub evidence."""
    bundle = _bundle(tmp_path, name="inc_gh_resume")
    repo = FilesystemRepository(persistence_root=tmp_path / "persistence")
    requests: List[httpx.Request] = []
    client = _client(_handler({"service/app.py": PATTERN_SOURCE}, requests=requests))

    orch = IncidentOrchestrator(
        non_interactive=True, repository=repo, github_context=_source(client)
    )
    with _pipeline(bundle.name):
        first = orch.investigate(bundle)

    assert _github_evidence(first)
    assert len(_contents_requests(requests)) == 1

    # The persisted state survives the on-disk round-trip with its snapshot.
    persisted = repo.load(bundle.name)
    assert persisted is not None
    assert persisted.metadata[GITHUB_CONTEXT_METADATA_KEY]["commit_sha"] == COMMIT_SHA
    assert {ev.evidence_id for ev in persisted.evidence} >= {"EV-CODE-003", "EV-CODE-004"}

    _rewind_to_evidence_fusion(repo, bundle.name)

    # A client that raises if called proves the resume does not re-fetch.
    orch2 = IncidentOrchestrator(
        non_interactive=True, repository=repo, github_context=_source(_ExplodingClient())
    )
    with _pipeline(bundle.name):
        resumed = orch2.resume(bundle.name, bundle)

    fused = _fused(resumed)
    restored = [ev for ev in fused["evidence"] if "github" in ev]
    assert [ev["evidence_id"] for ev in restored] == ["EV-CODE-003", "EV-CODE-004"]
    assert _validate_evidence_fusion(fused) == []
    # The persisted Code-stage output stays bundle-only.
    stage_evidence = resumed["stages"]["code"]["output"]["evidence"]
    assert all("github" not in ev for ev in stage_evidence)


def test_repeated_resume_does_not_duplicate_github_evidence(tmp_path: Path):
    """Requirement: repeated resume does not introduce duplicates."""
    bundle = _bundle(tmp_path, name="inc_gh_resume_twice")
    repo = FilesystemRepository(persistence_root=tmp_path / "persistence")
    client = _client(_handler({"service/app.py": PATTERN_SOURCE}))

    orch = IncidentOrchestrator(
        non_interactive=True, repository=repo, github_context=_source(client)
    )
    with _pipeline(bundle.name):
        orch.investigate(bundle)

    for _ in range(2):
        _rewind_to_evidence_fusion(repo, bundle.name)
        orch2 = IncidentOrchestrator(
            non_interactive=True, repository=repo, github_context=_source(_ExplodingClient())
        )
        with _pipeline(bundle.name):
            resumed = orch2.resume(bundle.name, bundle)

    fused = _fused(resumed)
    restored = [ev for ev in fused["evidence"] if "github" in ev]
    assert [ev["evidence_id"] for ev in restored] == ["EV-CODE-003", "EV-CODE-004"]

    state = repo.load(bundle.name)
    assert state is not None
    state_ids = [ev.evidence_id for ev in state.evidence]
    assert state_ids.count("EV-CODE-003") == 1
    assert state_ids.count("EV-CODE-004") == 1


def test_bundle_and_github_evidence_ids_remain_unique_after_resume(tmp_path: Path):
    """Requirement: bundle and GitHub evidence IDs stay unique across resume."""
    bundle = _bundle(tmp_path, name="inc_gh_unique")
    repo = FilesystemRepository(persistence_root=tmp_path / "persistence")
    client = _client(_handler({"service/app.py": PATTERN_SOURCE}))

    orch = IncidentOrchestrator(
        non_interactive=True, repository=repo, github_context=_source(client)
    )
    with _pipeline(bundle.name):
        orch.investigate(bundle)

    _rewind_to_evidence_fusion(repo, bundle.name)

    orch2 = IncidentOrchestrator(
        non_interactive=True, repository=repo, github_context=_source(_ExplodingClient())
    )
    with _pipeline(bundle.name):
        resumed = orch2.resume(bundle.name, bundle)

    fused = _fused(resumed)
    ids = [ev["evidence_id"] for ev in fused["evidence"]]
    assert len(ids) == len(set(ids))
    assert {"EV-CODE-001", "EV-CODE-002", "EV-CODE-003", "EV-CODE-004"} <= set(ids)
    assert _validate_evidence_fusion(fused) == []


def test_failure_snapshot_restores_nothing_and_stays_non_fatal(tmp_path: Path):
    """Requirement: partial/failure behaviour remains intact on the restore path."""
    bundle = _bundle(tmp_path, name="inc_gh_failure")
    repo = FilesystemRepository(persistence_root=tmp_path / "persistence")
    client = _client(_handler({"service/app.py": PATTERN_SOURCE}, commit_status=401))

    orch = IncidentOrchestrator(
        non_interactive=True, repository=repo, github_context=_source(client)
    )
    with _pipeline(bundle.name):
        orch.investigate(bundle)

    snapshot = repo.load(bundle.name).metadata[GITHUB_CONTEXT_METADATA_KEY]
    assert snapshot["status"] == GITHUB_STATUS_FAILURE

    _rewind_to_evidence_fusion(repo, bundle.name)

    orch2 = IncidentOrchestrator(
        non_interactive=True, repository=repo, github_context=_source(_ExplodingClient())
    )
    with _pipeline(bundle.name):
        resumed = orch2.resume(bundle.name, bundle)

    # No GitHub evidence exists to restore, and the Code stage is still intact.
    assert _github_evidence(resumed) == []
    assert resumed["stages"]["code"]["status"] == STATUS_SUCCEEDED
    assert {ev["evidence_id"] for ev in _fused(resumed)["evidence"]} >= {
        "EV-CODE-001", "EV-CODE-002",
    }


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
