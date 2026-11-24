"""Focused tests for Mission 27.3.3, Phase 4 — GitHub provenance API projection.

The GitHub evidence provenance and the investigation-level ``github_context`` /
``github_association`` metadata already exist in the persisted investigation
state (Phases 1–3). This suite verifies the *API projection layer* that now
exposes them, asserting real HTTP response payloads:

 1. GitHub provenance appears in the investigation result response.
 2. Repository source URLs point to the resolved commit SHA.
 3. Bundle-derived evidence receives no fabricated GitHub provenance.
 4. Investigation-level ``github_context`` metadata is projected correctly.
 5. Missing GitHub metadata (legacy investigation) is handled safely.
 6. No credential / ``credential_ref`` / token leaks into any response.
 7. Partial and failed GitHub retrieval metadata are represented correctly.
 8. Bundle-only API responses remain backward-compatible.
 9. Investigation detail and result endpoints keep working.
10. Approval snapshots and their integrity remain unchanged.

No real GitHub request is made, no Groq/LLM call and no network access occurs;
the persistence backend is an in-memory repository.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

import pytest
from starlette.testclient import TestClient

from api.app import create_app
from api.routes import _ACTIVE_INVESTIGATIONS, _ACTIVE_LOCK
from core.approval_snapshots import (
    SnapshotIntegrityError,
    verify_integrity,
)
from core.domain.models import (
    CodeEvidenceItem,
    FileChange,
    FixProposal,
    Hypothesis,
    IncidentStatus,
    LogEvidenceItem,
    StageStatus,
)
from core.domain.state import InvestigationState, StageResult
from core.persistence import PersistenceRepository

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

CONNECTION_ID = "conn_gh_primary"
OWNER = "acme"
REPO = "widgets"
REQUESTED_REF = "release/1.2"
COMMIT_SHA = "0123456789abcdef0123456789abcdef01234567"
RETRIEVED_AT = "2026-03-01T10:00:00+00:00"
CONTENT_HASH = "sha256:" + "a" * 64
SOURCE_URL = f"https://github.com/{OWNER}/{REPO}/blob/{COMMIT_SHA}/src/app.py#L42"

# Sentinel values that must never appear in any response payload.
SECRET_CREDENTIAL_REF = "SECRET_CREDENTIAL_REF_VALUE"
SECRET_TOKEN = "ghp_" + "A" * 30
SECRET_TOKEN_VALUE = "SECRET_TOKEN_VALUE"


# ---------------------------------------------------------------------------
# Repository / client helpers
# ---------------------------------------------------------------------------

class SnapshotRepository(PersistenceRepository):
    """In-memory repository that also stores approval snapshots.

    Mirrors the durable backend contract: ``save`` stores snapshots atomically
    with state, and ``load_approval_snapshot`` re-verifies the integrity digest
    before returning one.
    """

    def __init__(self, states: Optional[Dict[str, InvestigationState]] = None) -> None:
        self._states: Dict[str, InvestigationState] = dict(states or {})
        self._snapshots: Dict[Tuple[str, str], Any] = {}

    def save(self, state: InvestigationState, events: Optional[Any] = None, snapshots: Optional[Any] = None) -> None:
        self._states[state.incident_id] = state
        for snap in snapshots or []:
            self._snapshots.setdefault((snap.investigation_id, snap.proposal_id), snap)

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
        return False

    def list_events(
        self,
        investigation_id: str,
        limit: int = 200,
        after_cursor: Optional[Tuple[str, str]] = None,
    ) -> List[Any]:
        return []

    @property
    def supports_approval_snapshots(self) -> bool:
        return True

    def load_approval_snapshot(self, investigation_id: str, proposal_id: str) -> Optional[Any]:
        snapshot = self._snapshots.get((investigation_id, proposal_id))
        if snapshot is None:
            return None
        if not verify_integrity(snapshot.payload, snapshot.integrity_hash):
            raise SnapshotIntegrityError("snapshot integrity mismatch")
        return snapshot

    def tamper_snapshot(self, investigation_id: str, proposal_id: str) -> None:
        """Corrupt a stored snapshot payload to exercise integrity detection."""
        snapshot = self._snapshots[(investigation_id, proposal_id)]
        snapshot.payload["decision"]["notes"] = "tampered"


@pytest.fixture(autouse=True)
def clean_active_registry():
    with _ACTIVE_LOCK:
        _ACTIVE_INVESTIGATIONS.clear()
    yield
    with _ACTIVE_LOCK:
        _ACTIVE_INVESTIGATIONS.clear()


def _client(repo: PersistenceRepository) -> TestClient:
    return TestClient(create_app(repository=repo, github_client=None))


# ---------------------------------------------------------------------------
# State builders
# ---------------------------------------------------------------------------

def _github_evidence(evidence_id: str = "EV-CODE-002", **provenance_overrides: Any) -> Dict[str, Any]:
    provenance = {
        "owner": OWNER,
        "repo": REPO,
        "connection_id": CONNECTION_ID,
        "ref": REQUESTED_REF,
        "commit_sha": COMMIT_SHA,
        "path": "src/app.py",
        "source_url": SOURCE_URL,
        "retrieved_at": RETRIEVED_AT,
        "content_hash": CONTENT_HASH,
    }
    provenance.update(provenance_overrides)
    return {
        "evidence_id": evidence_id,
        "source": "code",
        "reference": "src/app.py:42",
        "type": "missing_guard",
        "excerpt": "return user.profile.name",
        "interpretation": f"Retrieved repository file src/app.py at commit {COMMIT_SHA[:7]}.",
        "github": provenance,
    }


def _github_context_snapshot(**overrides: Any) -> Dict[str, Any]:
    snapshot = {
        "provider": "github",
        "connection_id": CONNECTION_ID,
        "owner": OWNER,
        "repo": REPO,
        "requested_ref": REQUESTED_REF,
        "commit_sha": COMMIT_SHA,
        "retrieved_at": RETRIEVED_AT,
        "retrieved_file_count": 3,
        "evidence_count": 1,
        "skipped": [],
        "truncated": False,
        "truncation_reason": None,
        "total_bytes": 1234,
        "status": "success",
        "error": None,
    }
    snapshot.update(overrides)
    return snapshot


def _build_state(
    investigation_id: str = "inc_gh_proj",
    *,
    github: bool = True,
    context: Optional[Dict[str, Any]] = None,
    association: Optional[Dict[str, Any]] = None,
    leak_probe: bool = False,
) -> InvestigationState:
    """A completed investigation, optionally enriched with GitHub provenance."""
    state = InvestigationState(incident_id=investigation_id, status=IncidentStatus.COMPLETED)
    state.started_at = "2026-03-01T09:59:00+00:00"
    state.completed_at = "2026-03-01T10:01:00+00:00"

    # Bundle-derived code evidence — must never receive GitHub provenance.
    state.add_evidence(
        CodeEvidenceItem(
            evidence_id="EV-CODE-001",
            source="service",
            reference="service/app.py:42",
            type="missing_guard",
            excerpt="return user.profile.name",
            interpretation="Dereference without a None check.",
        )
    )
    state.add_evidence(
        LogEvidenceItem(
            evidence_id="EV-LOG-001",
            reference="logs/application.log:9",
            type="error_spike",
            excerpt="TypeError: 'NoneType' object is not subscriptable",
            timestamp="2026-03-01T09:59:04+00:00",
            interpretation="Repeated 500s aligned with the request wave.",
        )
    )

    evidence_ids = ["EV-CODE-001", "EV-LOG-001"]

    if github:
        evidence = _github_evidence()
        if leak_probe:
            evidence["github"]["credential_ref"] = SECRET_CREDENTIAL_REF
            evidence["github"]["token"] = SECRET_TOKEN
        state.add_evidence(evidence)
        evidence_ids.append("EV-CODE-002")

        snapshot = context if context is not None else _github_context_snapshot()
        if leak_probe:
            snapshot["credential_ref"] = SECRET_CREDENTIAL_REF
            snapshot["token"] = SECRET_TOKEN
            snapshot["env"] = {"SENTINEL_GITHUB_TOKEN": SECRET_TOKEN}
        state.metadata["github_context"] = snapshot

        association = association if association is not None else {
            "connection_id": CONNECTION_ID,
            "ref": REQUESTED_REF,
        }
        if leak_probe:
            association["credential_ref"] = SECRET_CREDENTIAL_REF
        state.metadata["github_association"] = association

    state.add_hypotheses(
        [
            Hypothesis(
                hypothesis_id="HYP-001",
                claim="A missing None guard causes the request handler to raise.",
                evidence_ids=evidence_ids,
                supporting_reasoning="The traceback points at the dereference.",
                falsification_criteria=["Handler does not dereference user.profile."],
                verification_plan=["Inspect the handler around the referenced line."],
            )
        ]
    )

    state.stages["verification"] = StageResult(
        status=StageStatus.SUCCEEDED,
        output={
            "incident_id": investigation_id,
            "results": [
                {
                    "hypothesis_id": "HYP-001",
                    "verdict": "CONFIRMED",
                    "confidence": 0.9,
                    "reasoning": "All checks passed.",
                    "checks": [
                        {
                            "check_id": "CHK-001",
                            "description": "Handler dereferences without a guard.",
                            "result": "PASS",
                            "evidence": ["EV-CODE-001"],
                            "reference": "service/app.py:42",
                            "detail": "guard_absent=True",
                        }
                    ],
                }
            ],
        },
    )

    state.add_proposals(
        [
            FixProposal(
                proposal_id="FIX-001",
                hypothesis_id="HYP-001",
                incident_id=investigation_id,
                summary="Add a None guard before dereferencing the profile.",
                rationale="The traceback and the code evidence agree.",
                changes=[
                    FileChange(
                        file="service/app.py",
                        description="Guard the profile dereference.",
                        before="return user.profile.name",
                        after="return user.profile.name if user.profile else None",
                        start_line=42,
                        end_line=42,
                    )
                ],
                patch=(
                    "--- a/service/app.py\n"
                    "+++ b/service/app.py\n"
                    "@@ -42 +42 @@\n"
                    "-return user.profile.name\n"
                    "+return user.profile.name if user.profile else None\n"
                ),
                expected_effect="Handler returns None instead of raising.",
                risks=["Callers must tolerate a None name."],
                validation_plan=["Replay the failing request."],
                rollback_plan="Revert the commit.",
                evidence_ids=evidence_ids,
            )
        ]
    )
    return state


# ---------------------------------------------------------------------------
# 1. GitHub provenance appears in the investigation result response
# ---------------------------------------------------------------------------

def test_github_provenance_present_in_result_response():
    repo = SnapshotRepository({"inc_gh_proj": _build_state()})
    client = _client(repo)

    resp = client.get("/investigations/inc_gh_proj/result")
    assert resp.status_code == 200, resp.text
    body = resp.json()

    by_id = {e["evidence_id"]: e for e in body["evidence"]}
    assert "EV-CODE-002" in by_id
    provenance = by_id["EV-CODE-002"]["github"]
    assert provenance is not None
    assert provenance["owner"] == OWNER
    assert provenance["repo"] == REPO
    assert provenance["connection_id"] == CONNECTION_ID
    assert provenance["ref"] == REQUESTED_REF
    assert provenance["commit_sha"] == COMMIT_SHA
    assert provenance["path"] == "src/app.py"
    assert provenance["retrieved_at"] == RETRIEVED_AT
    assert provenance["content_hash"] == CONTENT_HASH


# ---------------------------------------------------------------------------
# 2. Repository source URLs point to the resolved commit SHA
# ---------------------------------------------------------------------------

def test_source_url_pinned_to_resolved_commit_sha():
    repo = SnapshotRepository({"inc_gh_proj": _build_state()})
    client = _client(repo)

    body = client.get("/investigations/inc_gh_proj/result").json()
    provenance = next(e["github"] for e in body["evidence"] if e["evidence_id"] == "EV-CODE-002")

    assert provenance["source_url"] == SOURCE_URL
    assert COMMIT_SHA in provenance["source_url"]
    assert "/blob/" in provenance["source_url"]
    assert provenance["line_anchor"] == "#L42"
    assert provenance["source_url"].endswith(provenance["line_anchor"])


def test_line_anchor_absent_when_source_url_has_no_fragment():
    state = _build_state(
        context=_github_context_snapshot(),
        github=True,
    )
    # Rewrite the provenance to a URL with no line anchor.
    for item in state.evidence:
        if getattr(item, "evidence_id", None) == "EV-CODE-002":
            provenance = item.model_extra["github"]
            provenance["source_url"] = (
                f"https://github.com/{OWNER}/{REPO}/blob/{COMMIT_SHA}/src/app.py"
            )
    repo = SnapshotRepository({"inc_gh_proj": state})

    body = _client(repo).get("/investigations/inc_gh_proj/result").json()
    provenance = next(e["github"] for e in body["evidence"] if e["evidence_id"] == "EV-CODE-002")
    assert provenance["line_anchor"] is None
    assert provenance["source_url"].endswith("src/app.py")


# ---------------------------------------------------------------------------
# 3. Bundle-derived evidence receives no fabricated provenance
# ---------------------------------------------------------------------------

def test_bundle_evidence_has_no_fabricated_github_provenance():
    repo = SnapshotRepository({"inc_gh_proj": _build_state()})
    body = _client(repo).get("/investigations/inc_gh_proj/result").json()

    by_id = {e["evidence_id"]: e for e in body["evidence"]}
    assert by_id["EV-CODE-001"]["github"] is None
    assert by_id["EV-LOG-001"]["github"] is None


def test_bundle_only_investigation_has_no_github_provenance_anywhere():
    repo = SnapshotRepository({"inc_bundle": _build_state("inc_bundle", github=False)})
    body = _client(repo).get("/investigations/inc_bundle/result").json()

    assert all(e["github"] is None for e in body["evidence"])
    assert body["github_context"] is None
    assert body["github_association"] is None


# ---------------------------------------------------------------------------
# 4. Investigation-level github_context metadata is projected correctly
# ---------------------------------------------------------------------------

def test_github_context_and_association_projected_in_result():
    repo = SnapshotRepository({"inc_gh_proj": _build_state()})
    body = _client(repo).get("/investigations/inc_gh_proj/result").json()

    ctx = body["github_context"]
    assert ctx == {
        "provider": "github",
        "connection_id": CONNECTION_ID,
        "owner": OWNER,
        "repo": REPO,
        "requested_ref": REQUESTED_REF,
        "commit_sha": COMMIT_SHA,
        "retrieved_at": RETRIEVED_AT,
        "retrieved_file_count": 3,
        "evidence_count": 1,
        "total_bytes": 1234,
        "truncated": False,
        "truncation_reason": None,
        "status": "success",
        "skipped": [],
        "error": None,
    }
    assert body["github_association"] == {"connection_id": CONNECTION_ID, "ref": REQUESTED_REF}


def test_github_context_projected_in_detail_endpoint():
    repo = SnapshotRepository({"inc_gh_proj": _build_state()})
    resp = _client(repo).get("/investigations/inc_gh_proj")
    assert resp.status_code == 200, resp.text

    body = resp.json()
    assert body["github_context"]["commit_sha"] == COMMIT_SHA
    assert body["github_context"]["status"] == "success"
    assert body["github_association"] == {"connection_id": CONNECTION_ID, "ref": REQUESTED_REF}
    # Pre-existing detail fields are untouched.
    assert body["investigation_id"] == "inc_gh_proj"
    assert body["status"] == "COMPLETED"
    assert "stages" in body and "approvals" in body


# ---------------------------------------------------------------------------
# 5. Missing GitHub metadata (legacy investigation) is handled safely
# ---------------------------------------------------------------------------

def test_legacy_investigation_without_github_metadata_is_readable():
    state = _build_state("inc_legacy", github=False)
    # An older investigation may carry unrelated metadata but no github keys.
    state.metadata["legacy_note"] = "pre-existing"
    repo = SnapshotRepository({"inc_legacy": state})
    client = _client(repo)

    result = client.get("/investigations/inc_legacy/result")
    assert result.status_code == 200
    assert result.json()["github_context"] is None
    assert result.json()["github_association"] is None

    detail = client.get("/investigations/inc_legacy")
    assert detail.status_code == 200
    assert detail.json()["github_context"] is None
    assert detail.json()["github_association"] is None


def test_malformed_github_metadata_is_ignored_not_fabricated():
    """A non-dict github_context/association value yields null, never a guess."""
    state = _build_state("inc_malformed", github=False)
    state.metadata["github_context"] = "not-a-dict"
    state.metadata["github_association"] = ["also", "wrong"]
    repo = SnapshotRepository({"inc_malformed": state})

    body = _client(repo).get("/investigations/inc_malformed/result").json()
    assert body["github_context"] is None
    assert body["github_association"] is None


# ---------------------------------------------------------------------------
# 6. No credential / credential_ref / token leaks into any response
# ---------------------------------------------------------------------------

def test_no_credentials_leak_into_any_response():
    repo = SnapshotRepository({"inc_gh_proj": _build_state(leak_probe=True)})
    client = _client(repo)

    responses = [
        client.get("/investigations/inc_gh_proj"),
        client.get("/investigations/inc_gh_proj/result"),
    ]
    for resp in responses:
        assert resp.status_code == 200, resp.text
        text = resp.text
        assert "credential_ref" not in text
        assert SECRET_CREDENTIAL_REF not in text
        assert SECRET_TOKEN not in text
        assert SECRET_TOKEN_VALUE not in text
        assert "SENTINEL_GITHUB_TOKEN" not in text
        # The allowlisted keys are still present.
        assert "github_context" in text


def test_unsafe_skipped_detail_and_error_message_are_sanitized():
    context = _github_context_snapshot(
        status="failure",
        commit_sha=None,
        truncated=True,
        truncation_reason="total byte budget reached",
        skipped=[
            {
                "path": "src/big.py",
                "reason": "oversized",
                "detail": "C:\\Users\\secret\\proj\\file.py: too large",
            }
        ],
        error={"reason": "unavailable", "message": f"boom {SECRET_TOKEN} at /home/user/x"},
    )
    repo = SnapshotRepository({"inc_gh_proj": _build_state(context=context)})
    resp = _client(repo).get("/investigations/inc_gh_proj/result")
    assert resp.status_code == 200

    text = resp.text
    assert SECRET_TOKEN not in text
    assert "C:\\Users\\secret" not in text
    assert "/home/user/x" not in text

    ctx = resp.json()["github_context"]
    assert ctx["error"]["reason"] == "unavailable"
    assert "boom" in ctx["error"]["message"]
    assert ctx["skipped"][0]["reason"] == "oversized"
    assert ctx["skipped"][0]["detail"] is not None


# ---------------------------------------------------------------------------
# 7. Partial and failed GitHub retrieval metadata are represented correctly
# ---------------------------------------------------------------------------

def test_partial_retrieval_metadata_represented():
    context = _github_context_snapshot(
        status="partial",
        truncated=True,
        truncation_reason="total byte budget reached",
        retrieved_file_count=2,
        evidence_count=1,
        skipped=[
            {"path": "src/big.py", "reason": "oversized", "detail": None},
            {"path": "src/bin.dat", "reason": "binary", "detail": None},
        ],
    )
    repo = SnapshotRepository({"inc_gh_proj": _build_state(context=context)})
    body = _client(repo).get("/investigations/inc_gh_proj/result").json()

    ctx = body["github_context"]
    assert ctx["status"] == "partial"
    assert ctx["truncated"] is True
    assert ctx["truncation_reason"] == "total byte budget reached"
    assert ctx["retrieved_file_count"] == 2
    assert ctx["evidence_count"] == 1
    assert [s["reason"] for s in ctx["skipped"]] == ["oversized", "binary"]
    assert ctx["error"] is None


def test_failed_retrieval_metadata_represented():
    context = _github_context_snapshot(
        status="failure",
        commit_sha=None,
        retrieved_at=None,
        retrieved_file_count=0,
        evidence_count=0,
        total_bytes=0,
        error={"reason": "unresolved_ref", "message": "ref 'release/1.2' not found"},
    )
    repo = SnapshotRepository({"inc_gh_proj": _build_state(context=context)})
    body = _client(repo).get("/investigations/inc_gh_proj/result").json()

    ctx = body["github_context"]
    assert ctx["status"] == "failure"
    assert ctx["commit_sha"] is None
    assert ctx["error"]["reason"] == "unresolved_ref"
    assert "not found" in ctx["error"]["message"]
    # Identity metadata is still projected so the failure is attributable.
    assert ctx["owner"] == OWNER
    assert ctx["repo"] == REPO
    assert ctx["connection_id"] == CONNECTION_ID


# ---------------------------------------------------------------------------
# 8. Bundle-only API responses remain backward-compatible
# ---------------------------------------------------------------------------

def test_bundle_only_result_response_keeps_original_evidence_shape():
    repo = SnapshotRepository({"inc_bundle": _build_state("inc_bundle", github=False)})
    body = _client(repo).get("/investigations/inc_bundle/result").json()

    assert body["github_context"] is None
    assert body["github_association"] is None

    original_fields = {
        "evidence_id", "source", "type", "reference", "description",
        "excerpt", "timestamp", "interpretation", "metric", "value",
    }
    assert body["evidence"], "expected bundled evidence"
    for item in body["evidence"]:
        assert original_fields.issubset(item.keys())
        # The only added key is the optional github projection, and it is null.
        assert item["github"] is None


def test_bundle_only_detail_response_keeps_original_shape():
    repo = SnapshotRepository({"inc_bundle": _build_state("inc_bundle", github=False)})
    body = _client(repo).get("/investigations/inc_bundle").json()

    original_fields = {
        "investigation_id", "status", "current_stage", "started_at",
        "completed_at", "error", "llm_call_count", "version",
        "lease_owner", "lease_expires_at", "stages", "approvals",
    }
    assert original_fields.issubset(body.keys())
    assert body["github_context"] is None
    assert body["github_association"] is None


# ---------------------------------------------------------------------------
# 9. Investigation detail and result endpoints keep working
# ---------------------------------------------------------------------------

def test_detail_and_result_endpoints_keep_their_contracts():
    repo = SnapshotRepository({"inc_gh_proj": _build_state()})
    client = _client(repo)

    detail = client.get("/investigations/inc_gh_proj")
    assert detail.status_code == 200
    assert detail.json()["investigation_id"] == "inc_gh_proj"

    result = client.get("/investigations/inc_gh_proj/result")
    assert result.status_code == 200
    assert result.json()["result_available"] is True
    assert result.json()["evidence"]

    # Error contracts are unchanged.
    assert client.get("/investigations/missing/result").status_code == 404
    assert client.get("/investigations/missing").status_code == 404


def test_serving_get_endpoints_does_not_invoke_github():
    """GET endpoints must never trigger a GitHub request (no new pipeline stage)."""

    class ExplodingGitHubClient:
        def __getattr__(self, name: str):
            def _boom(*args: Any, **kwargs: Any):
                raise AssertionError("GET endpoint attempted a GitHub call")
            return _boom

    repo = SnapshotRepository({"inc_gh_proj": _build_state()})
    app = create_app(repository=repo, github_client=ExplodingGitHubClient())
    client = TestClient(app)

    assert client.get("/investigations/inc_gh_proj").status_code == 200
    assert client.get("/investigations/inc_gh_proj/result").status_code == 200


# ---------------------------------------------------------------------------
# 10. Approval snapshots and their integrity remain unchanged
# ---------------------------------------------------------------------------

def test_approval_snapshot_content_and_integrity_unchanged():
    """A decision on a GitHub-enriched investigation keeps the snapshot shape."""
    repo = SnapshotRepository({"inc_gh_proj": _build_state()})
    client = _client(repo)

    approve = client.post(
        "/investigations/inc_gh_proj/approval",
        json={"proposal_id": "FIX-001", "decision": "approved", "reviewer": "alice", "notes": "looks good"},
    )
    assert approve.status_code == 200, approve.text

    snap_resp = client.get("/investigations/inc_gh_proj/approval/FIX-001/snapshot")
    assert snap_resp.status_code == 200, snap_resp.text
    body = snap_resp.json()

    assert body["snapshot_available"] is True
    assert body["integrity"]["algorithm"] == "sha256"
    assert body["integrity"]["verified"] is True

    # The snapshot evidence documents must NOT gain a github key.
    snapshot_evidence = body["snapshot"]["evidence"]
    assert snapshot_evidence, "expected snapshot evidence for the referenced proposal"
    for entry in snapshot_evidence:
        assert "github" not in entry

    # Legacy pre-existing key set is preserved exactly.
    original_fields = {
        "evidence_id", "source", "type", "reference", "description",
        "excerpt", "timestamp", "interpretation", "metric", "value",
    }
    for entry in snapshot_evidence:
        assert set(entry.keys()) == original_fields


def test_approval_snapshot_integrity_still_detects_tampering():
    repo = SnapshotRepository({"inc_gh_proj": _build_state()})
    client = _client(repo)

    assert client.post(
        "/investigations/inc_gh_proj/approval",
        json={"proposal_id": "FIX-001", "decision": "rejected", "reviewer": "bob"},
    ).status_code == 200

    repo.tamper_snapshot("inc_gh_proj", "FIX-001")

    tampered = client.get("/investigations/inc_gh_proj/approval/FIX-001/snapshot")
    assert tampered.status_code == 500
    assert tampered.json()["error"] == "snapshot_integrity_failed"


def test_snapshot_projection_omits_github_key_for_github_evidence():
    """Unit-level: the shared projection excludes github unless explicitly asked."""
    from api.routes import _project_evidence

    state = _build_state()
    default_docs = _project_evidence(state)
    api_docs = _project_evidence(state, include_github=True)

    # Snapshot projection: no github key at all.
    assert all("github" not in doc for doc in default_docs)
    # API projection: github key present (populated for GitHub evidence).
    github_doc = next(d for d in api_docs if d["evidence_id"] == "EV-CODE-002")
    assert github_doc["github"] is not None
    plain_doc = next(d for d in api_docs if d["evidence_id"] == "EV-CODE-001")
    assert plain_doc["github"] is None
    # Everything else is identical between the two projections.
    for default_doc, api_doc in zip(default_docs, api_docs):
        assert default_doc == {k: v for k, v in api_doc.items() if k != "github"}
