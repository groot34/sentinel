"""Mission 26.2 — Approval Evidence Snapshots & Auditability.

Backend coverage for immutable approval snapshots, exercised against a real
PostgreSQL instance (never mocks) so transaction atomicity, write-once history
and integrity verification are validated for real.

Covered scenarios:

 1. Approval creates a snapshot.
 2. Rejection creates a snapshot.
 3. Snapshot content matches the decision-time context.
 4. Snapshot and decision persist atomically; a failed transaction leaves no
    orphaned snapshot.
 5. Duplicate submissions do not create duplicate snapshots.
 6. Later evidence / hypothesis / proposal changes never modify an old snapshot.
 7. Integrity verification succeeds; tampering is detected.
 8. A legacy approval without a snapshot stays readable and is never replaced
    with current evidence.
 9. Unauthorised snapshot access is rejected.

The suite is skipped automatically when PostgreSQL is unreachable.

Note: the ``TestClient`` is deliberately *not* used as a context manager here.
Entering/exiting it runs the application lifespan, whose shutdown closes the
injected repository pool — the repository must outlive the client so the tests
can inspect durable state directly afterwards. This mirrors the convention in
``tests/test_investigation_result_api.py``.
"""

from __future__ import annotations

import os
from typing import Any, Dict, List, Optional

import psycopg
import pytest
from starlette.testclient import TestClient

from agents.approval_gate import _build_approval_record
from api.app import create_app
from api.routes import _ACTIVE_INVESTIGATIONS, _ACTIVE_LOCK
from core.approval_snapshots import (
    ApprovalSnapshot,
    SnapshotIntegrityError,
    compute_integrity_hash,
)
from core.domain.models import (
    ApprovalRecord,
    CodeEvidenceItem,
    FileChange,
    FixProposal,
    Hypothesis,
    IncidentStatus,
    LogEvidenceItem,
    StageStatus,
)
from core.domain.state import InvestigationState, StageResult
from core.persistence.postgres import PostgresRepository

PG_URL = os.getenv(
    "SENTINEL_DATABASE_URL",
    "postgresql://sentinel:sentinel_dev_password@127.0.0.1:5432/sentinel_db",
)


def _is_postgres_available(url: str) -> bool:
    try:
        with psycopg.connect(url, connect_timeout=1) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1;")
                return cur.fetchone() == (1,)
    except Exception:
        return False


POSTGRES_AVAILABLE = _is_postgres_available(PG_URL)

pytestmark = pytest.mark.skipif(
    not POSTGRES_AVAILABLE, reason="PostgreSQL is not available at " + PG_URL
)

ID_PREFIX = "snap_"


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def clean_active_registry():
    with _ACTIVE_LOCK:
        _ACTIVE_INVESTIGATIONS.clear()
    yield
    with _ACTIVE_LOCK:
        _ACTIVE_INVESTIGATIONS.clear()


def _clean_test_rows(r: PostgresRepository) -> None:
    """Remove any rows owned by this suite. Idempotent, best-effort."""
    try:
        with r.pool.connection() as conn:
            with conn.transaction():
                with conn.cursor() as cur:
                    cur.execute(
                        "DELETE FROM investigations WHERE investigation_id LIKE %s;",
                        (ID_PREFIX + "%",),
                    )
                    cur.execute(
                        "DELETE FROM event_outbox WHERE investigation_id LIKE %s;",
                        (ID_PREFIX + "%",),
                    )
                    cur.execute(
                        "DELETE FROM approval_snapshots WHERE investigation_id LIKE %s;",
                        (ID_PREFIX + "%",),
                    )
    except Exception:
        pass


@pytest.fixture
def repo():
    """A live PostgresRepository whose schema is ensured and test rows cleaned.

    Rows are cleaned both before and after each test so a previously interrupted
    run cannot leave an ``snap_*`` row behind that would trip optimistic
    concurrency control on the next run.
    """
    r = PostgresRepository(PG_URL)
    r.initialize_schema()
    _clean_test_rows(r)
    yield r
    _clean_test_rows(r)
    r.close()


def _client(repo: PostgresRepository, **kwargs: Any) -> TestClient:
    return TestClient(create_app(repository=repo, **kwargs))


def _verification_output(incident_id: str, results: List[Dict[str, Any]]) -> Dict[str, Any]:
    return {"incident_id": incident_id, "results": results}


def _build_state(investigation_id: str) -> InvestigationState:
    """A completed investigation with evidence, hypothesis, verification and a proposal."""
    state = InvestigationState(incident_id=investigation_id, status=IncidentStatus.COMPLETED)
    state.started_at = "2026-02-01T00:00:00+00:00"
    state.completed_at = "2026-02-01T00:05:00+00:00"

    state.add_evidence(
        LogEvidenceItem(
            evidence_id="EV-LOG-001",
            reference="logs/application.log:9",
            type="error_spike",
            excerpt="TypeError: 'NoneType' object is not subscriptable",
            timestamp="2026-02-01T00:00:04+00:00",
            interpretation="Repeated 500s aligned with the request wave.",
        )
    )
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

    state.add_hypotheses(
        [
            Hypothesis(
                hypothesis_id="HYP-001",
                claim="A missing None guard causes the request handler to raise.",
                evidence_ids=["EV-LOG-001", "EV-CODE-001"],
                supporting_reasoning="The traceback points at the dereference.",
                falsification_criteria=["Handler does not dereference user.profile."],
                verification_plan=["Inspect the handler around the referenced line."],
            )
        ]
    )

    state.stages["verification"] = StageResult(
        status=StageStatus.SUCCEEDED,
        output=_verification_output(
            investigation_id,
            [
                {
                    "hypothesis_id": "HYP-001",
                    "verdict": "CONFIRMED",
                    "confidence": 0.95,
                    "reasoning": "All checks passed.",
                    "checks": [
                        {
                            "check_id": "CHK-001",
                            "description": "Handler dereferences without a guard.",
                            "result": "PASS",
                            "evidence": ["EV-CODE-001"],
                            "reference": "service/app.py:42",
                            "detail": "guard_absent=True",
                            "check_type": "code_invariant",
                            "discriminating": True,
                        }
                    ],
                }
            ],
        ),
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
                evidence_ids=["EV-CODE-001", "EV-LOG-001"],
            )
        ]
    )
    return state


def _approve(client: TestClient, inv: str, decision: str = "approved", notes: Optional[str] = None):
    payload: Dict[str, Any] = {
        "proposal_id": "FIX-001",
        "decision": decision,
        "reviewer": "alice",
    }
    if notes is not None:
        payload["notes"] = notes
    return client.post(f"/investigations/{inv}/approval", json=payload)


def _get_snapshot(client: TestClient, inv: str, proposal_id: str = "FIX-001"):
    return client.get(f"/investigations/{inv}/approval/{proposal_id}/snapshot")


def _count_snapshots(repo: PostgresRepository, inv: str) -> int:
    with repo.pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) FROM approval_snapshots WHERE investigation_id = %s;",
                (inv,),
            )
            return cur.fetchone()[0]


# ---------------------------------------------------------------------------
# 1/2/3 — snapshot creation and content
# ---------------------------------------------------------------------------

def test_approval_creates_snapshot(repo):
    inv = "snap_approve"
    repo.save(_build_state(inv))

    client = _client(repo)
    resp = _approve(client, inv, "approved")
    assert resp.status_code == 200, resp.text

    snap = _get_snapshot(client, inv)
    assert snap.status_code == 200, snap.text
    body = snap.json()

    assert body["snapshot_available"] is True
    assert body["decision"] == "approved"
    assert body["reviewer"] == "alice"
    assert body["snapshot_version"] == 1
    assert body["integrity"]["algorithm"] == "sha256"
    assert body["integrity"]["verified"] is True
    assert len(body["integrity"]["hash"]) == 64
    assert _count_snapshots(repo, inv) == 1


def test_rejection_creates_snapshot(repo):
    inv = "snap_reject"
    repo.save(_build_state(inv))

    client = _client(repo)
    resp = _approve(client, inv, "rejected", notes="Not safe to ship.")
    assert resp.status_code == 200, resp.text
    body = _get_snapshot(client, inv).json()

    assert body["snapshot_available"] is True
    assert body["decision"] == "rejected"
    assert body["notes"] == "Not safe to ship."
    assert body["snapshot"]["decision"]["decision"] == "rejected"
    assert _count_snapshots(repo, inv) == 1


def test_snapshot_content_matches_decision_time_context(repo):
    inv = "snap_content"
    repo.save(_build_state(inv))

    client = _client(repo)
    assert _approve(client, inv, "approved").status_code == 200
    body = _get_snapshot(client, inv).json()

    snapshot = body["snapshot"]
    # Decision context
    assert snapshot["investigation_id"] == inv
    assert snapshot["proposal_id"] == "FIX-001"
    assert snapshot["decision"]["reviewer"] == "alice"
    assert snapshot["decision"]["timestamp"] == body["decided_at"]

    # Proposal context
    assert snapshot["proposal"]["summary"].startswith("Add a None guard")
    assert "service/app.py" in snapshot["proposal"]["patch"]
    assert snapshot["proposal"]["risks"] == ["Callers must tolerate a None name."]
    assert snapshot["proposal"]["validation_plan"] == ["Replay the failing request."]
    assert snapshot["proposal"]["rollback_plan"] == "Revert the commit."

    # Hypothesis context, with decision-time verdict/confidence
    assert snapshot["hypothesis"]["hypothesis_id"] == "HYP-001"
    assert snapshot["hypothesis"]["verdict"] == "CONFIRMED"
    assert snapshot["hypothesis"]["confidence"] == pytest.approx(0.95)

    # Evidence context (only referenced evidence, not the whole investigation)
    ev_ids = {e["evidence_id"] for e in snapshot["evidence"]}
    assert ev_ids == {"EV-LOG-001", "EV-CODE-001"}

    # Verification context
    checks = snapshot["verification"][0]["checks"]
    assert checks[0]["check_id"] == "CHK-001"
    assert checks[0]["result"] == "PASS"
    assert checks[0]["check_type"] == "code_invariant"
    assert checks[0]["discriminating"] is True


def test_snapshot_hash_matches_recomputation(repo):
    inv = "snap_hash"
    repo.save(_build_state(inv))

    client = _client(repo)
    assert _approve(client, inv, "approved").status_code == 200
    body = _get_snapshot(client, inv).json()

    assert compute_integrity_hash(body["snapshot"]) == body["integrity"]["hash"]


# ---------------------------------------------------------------------------
# 4 — atomicity and failure isolation
# ---------------------------------------------------------------------------

def test_snapshot_and_decision_persist_atomically(repo):
    inv = "snap_atomic"
    repo.save(_build_state(inv))

    client = _client(repo)
    assert _approve(client, inv, "approved").status_code == 200

    reloaded = repo.load(inv)
    assert reloaded is not None
    assert any(a.proposal_id == "FIX-001" for a in reloaded.approvals)
    snapshot = repo.load_approval_snapshot(inv, "FIX-001")
    assert snapshot is not None
    assert snapshot.decision == "approved"


def test_failed_snapshot_insert_leaves_no_orphan_and_no_decision(repo):
    inv = "snap_fail"
    state = _build_state(inv)
    repo.save(state)

    loaded = repo.load(inv)
    assert loaded is not None
    loaded.update_approval(ApprovalRecord.from_dict(_build_approval_record("FIX-001", "approved")))

    # An integrity hash that violates the 64-char CHECK constraint forces the
    # snapshot insert to fail *inside* the transaction, after the state UPDATE.
    bad_snapshot = ApprovalSnapshot(
        investigation_id=inv,
        proposal_id="FIX-001",
        decision="approved",
        reviewer="alice",
        decided_at="2026-02-01T00:05:00+00:00",
        snapshot_version=1,
        integrity_hash="not-a-valid-sha256",
        payload={"marker": "should-not-persist"},
    )

    with pytest.raises(Exception):
        repo.save(loaded, snapshots=[bad_snapshot])

    # Neither the decision nor an orphaned snapshot may survive.
    reloaded = repo.load(inv)
    assert reloaded is not None
    assert all(a.proposal_id != "FIX-001" for a in reloaded.approvals)
    assert repo.load_approval_snapshot(inv, "FIX-001") is None
    assert _count_snapshots(repo, inv) == 0


def test_duplicate_submission_does_not_create_duplicate_snapshot(repo):
    inv = "snap_dupe"
    repo.save(_build_state(inv))

    client = _client(repo)
    assert _approve(client, inv, "approved").status_code == 200
    # A second human submission for the same proposal is rejected by the
    # existing prior-decision protection.
    second = _approve(client, inv, "rejected")
    assert second.status_code == 409

    assert _count_snapshots(repo, inv) == 1


# ---------------------------------------------------------------------------
# 6 — immutability of history
# ---------------------------------------------------------------------------

def test_later_evidence_does_not_modify_snapshot(repo):
    inv = "snap_immutable_ev"
    repo.save(_build_state(inv))

    client = _client(repo)
    assert _approve(client, inv, "approved").status_code == 200
    before = _get_snapshot(client, inv).json()

    # A new evidence item arrives after the decision.
    state = repo.load(inv)
    assert state is not None
    state.add_evidence(
        LogEvidenceItem(
            evidence_id="EV-LOG-999",
            reference="logs/later.log:1",
            type="late",
            excerpt="evidence arriving after the decision",
            timestamp="2026-02-02T00:00:00+00:00",
            interpretation="Added later.",
        )
    )
    repo.save(state)

    after = _get_snapshot(client, inv).json()

    assert after["snapshot"] == before["snapshot"]
    assert after["integrity"]["hash"] == before["integrity"]["hash"]
    assert "EV-LOG-999" not in {e["evidence_id"] for e in after["snapshot"]["evidence"]}


def test_modified_proposal_does_not_modify_snapshot(repo):
    inv = "snap_immutable_prop"
    repo.save(_build_state(inv))

    client = _client(repo)
    assert _approve(client, inv, "approved").status_code == 200
    before = _get_snapshot(client, inv).json()

    state = repo.load(inv)
    assert state is not None
    state.proposals[0].summary = "REWRITTEN SUMMARY"
    state.proposals[0].patch = "REWRITTEN PATCH"
    state.proposals[0].risks = ["totally different risk"]
    repo.save(state)

    after = _get_snapshot(client, inv).json()

    assert after["snapshot"]["proposal"]["summary"].startswith("Add a None guard")
    assert "REWRITTEN SUMMARY" not in after["snapshot"]["proposal"]["summary"]
    assert after["snapshot"]["proposal"]["risks"] == ["Callers must tolerate a None name."]
    assert after["integrity"]["hash"] == before["integrity"]["hash"]


def test_modified_hypothesis_does_not_modify_snapshot(repo):
    inv = "snap_immutable_hyp"
    repo.save(_build_state(inv))

    client = _client(repo)
    assert _approve(client, inv, "approved").status_code == 200
    before = _get_snapshot(client, inv).json()

    state = repo.load(inv)
    assert state is not None
    state.hypotheses[0].claim = "REWRITTEN CLAIM"
    repo.save(state)

    after = _get_snapshot(client, inv).json()

    assert after["snapshot"]["hypothesis"]["claim"].startswith("A missing None guard")
    assert after["integrity"]["hash"] == before["integrity"]["hash"]


# ---------------------------------------------------------------------------
# 7 — integrity and tampering
# ---------------------------------------------------------------------------

def test_integrity_verification_succeeds_on_load(repo):
    inv = "snap_integrity_ok"
    repo.save(_build_state(inv))

    client = _client(repo)
    assert _approve(client, inv, "approved").status_code == 200

    snapshot = repo.load_approval_snapshot(inv, "FIX-001")
    assert snapshot is not None
    assert snapshot.integrity_hash == compute_integrity_hash(snapshot.payload)


def test_tampered_snapshot_is_detected(repo):
    inv = "snap_tamper"
    repo.save(_build_state(inv))

    client = _client(repo)
    assert _approve(client, inv, "approved").status_code == 200

    # Directly corrupt the stored payload, simulating tampering/corruption.
    with repo.pool.connection() as conn:
        with conn.transaction():
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE approval_snapshots
                    SET snapshot_payload = jsonb_set(
                        snapshot_payload, '{proposal,summary}', '"TAMPERED"'
                    )
                    WHERE investigation_id = %s AND proposal_id = 'FIX-001';
                    """,
                    (inv,),
                )

    with pytest.raises(SnapshotIntegrityError):
        repo.load_approval_snapshot(inv, "FIX-001")

    resp = _get_snapshot(client, inv)
    assert resp.status_code == 500
    assert resp.json()["error"] == "snapshot_integrity_failed"


# ---------------------------------------------------------------------------
# 8 — legacy approvals / no substitution
# ---------------------------------------------------------------------------

def test_legacy_approval_without_snapshot_is_readable(repo):
    inv = "snap_legacy"
    state = _build_state(inv)
    # A decision recorded before snapshot capture existed: approval persisted,
    # no snapshot row written.
    state.update_approval(ApprovalRecord.from_dict(_build_approval_record("FIX-001", "rejected")))
    repo.save(state)
    assert _count_snapshots(repo, inv) == 0

    resp = _get_snapshot(_client(repo), inv)
    assert resp.status_code == 200, resp.text
    body = resp.json()

    assert body["snapshot_available"] is False
    assert body["decision"] == "rejected"
    assert body["reviewer"] == "human"
    assert body["message"]
    assert body["integrity"] is None


def test_missing_snapshot_is_not_replaced_with_current_evidence(repo):
    inv = "snap_no_substitute"
    state = _build_state(inv)
    state.update_approval(ApprovalRecord.from_dict(_build_approval_record("FIX-001", "approved")))
    repo.save(state)

    body = _get_snapshot(_client(repo), inv).json()

    # Current investigation still holds evidence/hypotheses/proposals, but none
    # of it may be presented as decision-time context.
    assert body["snapshot_available"] is False
    assert body["snapshot"] == {}


def test_snapshot_requires_an_approval_record(repo):
    inv = "snap_no_approval"
    repo.save(_build_state(inv))

    resp = _get_snapshot(_client(repo), inv)
    assert resp.status_code == 404
    assert resp.json()["error"] == "approval_not_found"


def test_snapshot_for_unknown_investigation_is_404(repo):
    resp = _get_snapshot(_client(repo), "snap_absent")
    assert resp.status_code == 404
    assert resp.json()["error"] == "investigation_not_found"


def test_invalid_proposal_id_is_rejected(repo):
    inv = "snap_bad_pid"
    repo.save(_build_state(inv))

    resp = _client(repo).get(f"/investigations/{inv}/approval/NOTAPROPOSAL/snapshot")
    assert resp.status_code == 400
    assert resp.json()["error"] == "invalid_proposal_id"


# ---------------------------------------------------------------------------
# 9 — access control
# ---------------------------------------------------------------------------

def test_unauthorised_snapshot_access_is_rejected(repo):
    inv = "snap_auth"
    repo.save(_build_state(inv))

    client = _client(repo, auth_enabled=True, api_key="s3cret", auth_protect_reads=True)
    unauth = _get_snapshot(client, inv)
    assert unauth.status_code == 401

    authorised = client.get(
        f"/investigations/{inv}/approval/FIX-001/snapshot",
        headers={"Authorization": "Bearer s3cret"},
    )
    # No approval recorded yet -> authenticated request reaches the handler.
    assert authorised.status_code == 404
    assert authorised.json()["error"] == "approval_not_found"


# ---------------------------------------------------------------------------
# Unsupported backend honesty (not PostgreSQL)
# ---------------------------------------------------------------------------

def test_unsupported_backend_reports_not_captured(tmp_path):
    from core.persistence.filesystem import FilesystemRepository

    fs_repo = FilesystemRepository(persistence_root=tmp_path)
    inv = "snap_fs_backend"
    state = _build_state(inv)
    fs_repo.save(state)

    client = TestClient(create_app(repository=fs_repo))
    assert _approve(client, inv, "approved").status_code == 200
    body = _get_snapshot(client, inv).json()

    assert body["snapshot_available"] is False
    assert body["snapshot"] == {}
    assert body["message"]
