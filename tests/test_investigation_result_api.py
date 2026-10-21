"""Focused tests for Mission 17: GET /investigations/{investigation_id}/result.

Covers:
 1. A completed investigation exposes hypotheses, evidence, verification
    checks and proposals.
 2. Returned proposal identifiers are usable with the existing approval
    endpoint.
 3. Incomplete / in-progress investigations are handled safely (empty
    sections, no fabricated results, no crash).
 4. Missing investigations and malformed IDs keep the existing error
    behaviour (404 / 400 / 503).
 5. Internal state (stage outputs, state metadata, evidence metadata, raw
    file bodies, unrelated persisted data) is not leaked.
 6. The pre-existing GET /investigations/{id} contract is unchanged.

No LLM calls, no Groq, no network.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

import pytest
from starlette.testclient import TestClient

from api.app import create_app
from api.routes import _ACTIVE_INVESTIGATIONS, _ACTIVE_LOCK
from core.domain.models import (
    CodeEvidenceItem,
    EvidenceItem,
    FileChange,
    FixProposal,
    Hypothesis,
    IncidentStatus,
    LogEvidenceItem,
    MetricEvidenceItem,
    StageStatus,
)
from core.domain.state import InvestigationState, StageResult
from core.persistence import PersistenceRepository


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

class MockRepository(PersistenceRepository):
    """In-memory repository matching the API test conventions."""

    def __init__(self, states: Optional[Dict[str, InvestigationState]] = None) -> None:
        self._states: Dict[str, InvestigationState] = states or {}
        self.save_called: List[InvestigationState] = []

    def save(self, state: InvestigationState, events: Optional[Any] = None) -> None:
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
        return False

    def list_events(
        self,
        investigation_id: str,
        limit: int = 200,
        after_cursor: Optional[Tuple[str, str]] = None,
    ) -> List[Any]:
        return []


@pytest.fixture(autouse=True)
def clean_active_registry():
    """Keep the process-local active registry clean around every test."""
    with _ACTIVE_LOCK:
        _ACTIVE_INVESTIGATIONS.clear()
    yield
    with _ACTIVE_LOCK:
        _ACTIVE_INVESTIGATIONS.clear()


def _client(repo: PersistenceRepository) -> TestClient:
    return TestClient(create_app(repository=repo))


def _verification_output(incident_id: str, results: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Build a verification stage output shaped like VerificationAgent's."""
    return {"incident_id": incident_id, "results": results}


def _build_completed_state(investigation_id: str = "inc_result_demo") -> InvestigationState:
    """A fully populated, completed investigation."""
    state = InvestigationState(
        incident_id=investigation_id,
        status=IncidentStatus.COMPLETED,
    )
    state.started_at = "2026-01-01T00:00:00+00:00"
    state.completed_at = "2026-01-01T00:05:00+00:00"

    state.add_evidence(
        LogEvidenceItem(
            evidence_id="EV-LOG-001",
            reference="logs/application.log:9",
            type="error_spike",
            excerpt="TypeError: 'NoneType' object is not subscriptable",
            timestamp="2026-01-01T00:00:04+00:00",
            interpretation="Repeated 500s aligned with the request wave.",
        )
    )
    state.add_evidence(
        MetricEvidenceItem(
            evidence_id="EV-MET-001",
            reference="metrics/metrics.csv:12",
            metric="db_query_count",
            value=1200.0,
            type="spike",
            timestamp="2026-01-01T00:00:05+00:00",
            interpretation="Query volume jumped with the same shape.",
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
    state.add_evidence(
        EvidenceItem(
            evidence_id="EV-CUSTOM-001",
            source_type="logs",
            description="Deploy marker observed at the incident start.",
            raw_snippet="deploy v1.4.2 started",
            timestamp="2026-01-01T00:00:01+00:00",
            metadata={"secret_evidence_metadata": "SECRET_EVIDENCE_METADATA"},
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
            ),
            Hypothesis(
                hypothesis_id="HYP-002",
                claim="Database connection exhaustion is the root cause.",
                evidence_ids=["EV-MET-001"],
                supporting_reasoning="Query counts rose before the errors.",
                falsification_criteria=["Connection pool never saturated."],
                verification_plan=["Check pool saturation metrics."],
            ),
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
                        },
                        {
                            "check_id": "CHK-002",
                            "description": "Falsification: handler does not dereference.",
                            "result": "PASS",
                            "evidence": ["EV-LOG-001"],
                            "reference": "logs/application.log:9",
                            "detail": None,
                        },
                    ],
                },
                {
                    "hypothesis_id": "HYP-002",
                    "verdict": "REJECTED",
                    "confidence": 0.5,
                    "reasoning": "Pool saturation check failed.",
                    "checks": [
                        {
                            "check_id": "CHK-001",
                            "description": "Connection pool saturated.",
                            "result": "FAIL",
                            "evidence": ["EV-MET-001"],
                            "reference": "metrics/metrics.csv:12",
                            "detail": "max_connections=100, observed=12",
                        }
                    ],
                },
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


# ---------------------------------------------------------------------------
# 1. Completed investigation exposes the full diagnosis
# ---------------------------------------------------------------------------

def test_result_exposes_hypotheses_evidence_verification_and_proposals():
    state = _build_completed_state()
    client = _client(MockRepository({state.incident_id: state}))

    resp = client.get(f"/investigations/{state.incident_id}/result")
    assert resp.status_code == 200
    body = resp.json()

    assert body["investigation_id"] == state.incident_id
    assert body["status"] == "COMPLETED"
    assert body["completed_at"] == "2026-01-01T00:05:00+00:00"
    assert body["result_available"] is True

    # Hypotheses, with verdicts joined from verification.
    assert [h["hypothesis_id"] for h in body["hypotheses"]] == ["HYP-001", "HYP-002"]
    assert body["hypotheses"][0]["verdict"] == "CONFIRMED"
    assert body["hypotheses"][0]["evidence_ids"] == ["EV-LOG-001", "EV-CODE-001"]
    assert body["hypotheses"][1]["verdict"] == "REJECTED"

    # Evidence, preserving traceable references.
    evidence_by_id = {e["evidence_id"]: e for e in body["evidence"]}
    assert set(evidence_by_id) == {
        "EV-LOG-001",
        "EV-MET-001",
        "EV-CODE-001",
        "EV-CUSTOM-001",
    }
    assert evidence_by_id["EV-LOG-001"]["reference"] == "logs/application.log:9"
    assert evidence_by_id["EV-MET-001"]["metric"] == "db_query_count"
    assert evidence_by_id["EV-MET-001"]["value"] == 1200.0
    assert evidence_by_id["EV-CODE-001"]["reference"] == "service/app.py:42"
    assert evidence_by_id["EV-CUSTOM-001"]["excerpt"] == "deploy v1.4.2 started"

    # Verification: overall verdict, per-check PASS/FAIL and references.
    verification = {v["hypothesis_id"]: v for v in body["verification"]}
    assert verification["HYP-001"]["verdict"] == "CONFIRMED"
    assert verification["HYP-001"]["confidence"] == 0.95
    assert [c["result"] for c in verification["HYP-001"]["checks"]] == ["PASS", "PASS"]
    assert verification["HYP-001"]["checks"][0]["reference"] == "service/app.py:42"
    assert verification["HYP-002"]["checks"][0]["result"] == "FAIL"
    assert verification["HYP-002"]["checks"][0]["evidence_ids"] == ["EV-MET-001"]

    # Proposals, including the unified diff.
    proposal = body["proposals"][0]
    assert proposal["proposal_id"] == "FIX-001"
    assert proposal["hypothesis_id"] == "HYP-001"
    assert proposal["status"] == "PROPOSED"
    assert "--- a/service/app.py" in proposal["patch"]
    assert proposal["risks"] == ["Callers must tolerate a None name."]
    assert proposal["rollback_plan"] == "Revert the commit."
    assert proposal["changes"][0]["file"] == "service/app.py"
    assert proposal["changes"][0]["start_line"] == 42
    assert proposal["human_approval_notice"].startswith("AWAITING HUMAN APPROVAL")


# ---------------------------------------------------------------------------
# 2. Proposal identifiers work with the existing approval flow
# ---------------------------------------------------------------------------

def test_returned_proposal_id_is_accepted_by_approval_endpoint():
    state = _build_completed_state()
    repo = MockRepository({state.incident_id: state})
    client = _client(repo)

    result = client.get(f"/investigations/{state.incident_id}/result")
    assert result.status_code == 200
    proposal_ids = [p["proposal_id"] for p in result.json()["proposals"]]
    assert proposal_ids == ["FIX-001"]

    approval = client.post(
        f"/investigations/{state.incident_id}/approval",
        json={
            "proposal_id": proposal_ids[0],
            "decision": "approved",
            "reviewer": "audit-reviewer",
            "notes": "Reviewed the diff.",
        },
    )
    assert approval.status_code == 200, approval.text
    approval_body = approval.json()
    assert approval_body["approval"]["proposal_id"] == "FIX-001"
    assert approval_body["approval"]["decision"] == "approved"
    assert approval_body["approval"]["approved_by"] == "audit-reviewer"

    # The decision is visible on the pre-existing GET response.
    detail = client.get(f"/investigations/{state.incident_id}").json()
    assert [a["proposal_id"] for a in detail["approvals"]] == ["FIX-001"]
    assert detail["approvals"][0]["status"] == "APPROVED"


# ---------------------------------------------------------------------------
# 3. Incomplete / in-progress investigations are handled safely
# ---------------------------------------------------------------------------

def test_in_progress_investigation_returns_empty_sections():
    state = InvestigationState(incident_id="inc_running", status=IncidentStatus.RUNNING)
    state.started_at = "2026-01-01T00:00:00+00:00"
    client = _client(MockRepository({"inc_running": state}))

    resp = client.get("/investigations/inc_running/result")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "RUNNING"
    assert body["completed_at"] is None
    assert body["result_available"] is False
    assert body["hypotheses"] == []
    assert body["evidence"] == []
    assert body["verification"] == []
    assert body["proposals"] == []


def test_partial_state_without_verification_or_proposals_is_safe():
    """Evidence + hypotheses persisted, verification stage never ran."""
    state = InvestigationState(incident_id="inc_partial", status=IncidentStatus.PARTIAL)
    state.started_at = "2026-01-01T00:00:00+00:00"
    state.add_evidence(
        LogEvidenceItem(
            evidence_id="EV-LOG-001",
            reference="logs/application.log:9",
            type="error_spike",
            excerpt="boom",
        )
    )
    state.add_hypotheses(
        [
            Hypothesis(
                hypothesis_id="HYP-001",
                claim="Unverified claim.",
                evidence_ids=["EV-LOG-001"],
                supporting_reasoning="Reasoning.",
                falsification_criteria=["Falsifier."],
                verification_plan=["Plan step."],
            )
        ]
    )
    client = _client(MockRepository({"inc_partial": state}))

    resp = client.get("/investigations/inc_partial/result")
    assert resp.status_code == 200
    body = resp.json()
    assert body["result_available"] is True
    assert body["verification"] == []
    assert body["proposals"] == []
    # No verdict is invented when verification never ran.
    assert body["hypotheses"][0]["verdict"] is None


def test_malformed_verification_stage_output_is_skipped():
    """Malformed persisted verification data yields no checks, not a 500."""
    state = InvestigationState(incident_id="inc_malformed", status=IncidentStatus.COMPLETED)
    state.started_at = "2026-01-01T00:00:00+00:00"
    state.stages["verification"] = StageResult(
        status=StageStatus.SUCCEEDED,
        output={
            "results": [
                "not-a-dict",
                {"hypothesis_id": "HYP-001"},  # missing verdict
                {"verdict": "CONFIRMED"},  # missing hypothesis_id
                {
                    "hypothesis_id": "HYP-002",
                    "verdict": "INCONCLUSIVE",
                    "checks": ["not-a-dict", {"check_id": "CHK-001"}],
                },
            ]
        },
    )
    client = _client(MockRepository({"inc_malformed": state}))

    resp = client.get("/investigations/inc_malformed/result")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert len(body["verification"]) == 1
    assert body["verification"][0]["hypothesis_id"] == "HYP-002"
    assert body["verification"][0]["checks"][0]["check_id"] == "CHK-001"
    assert body["verification"][0]["checks"][0]["result"] is None


def test_verification_stage_output_of_wrong_type_is_ignored():
    state = InvestigationState(incident_id="inc_bad_out", status=IncidentStatus.COMPLETED)
    state.started_at = "2026-01-01T00:00:00+00:00"
    state.stages["verification"] = StageResult(
        status=StageStatus.FAILED,
        output="not-a-dict",
        error="verification exploded",
    )
    client = _client(MockRepository({"inc_bad_out": state}))

    resp = client.get("/investigations/inc_bad_out/result")
    assert resp.status_code == 200
    assert resp.json()["verification"] == []


# ---------------------------------------------------------------------------
# 4. Existing error behaviour is preserved
# ---------------------------------------------------------------------------

def test_result_not_found_returns_404():
    client = _client(MockRepository())
    resp = client.get("/investigations/inc_missing/result")
    assert resp.status_code == 404
    assert resp.json()["error"] == "investigation_not_found"


def test_result_invalid_id_returns_400():
    client = _client(MockRepository())
    resp = client.get("/investigations/invalid@id!/result")
    assert resp.status_code == 400
    assert resp.json()["error"] == "invalid_investigation_id"


def test_result_repository_unavailable_returns_503():
    app = create_app(repository=MockRepository())
    app.state.repository = None
    client = TestClient(app)

    resp = client.get("/investigations/inc_any/result")
    assert resp.status_code == 503
    assert resp.json()["error"] == "persistence_unavailable"


# ---------------------------------------------------------------------------
# 5. No internal state leakage
# ---------------------------------------------------------------------------

def test_result_does_not_leak_internal_state():
    state = _build_completed_state()
    # Unrelated persisted data that must never reach the response.
    state.metadata = {"secret_state_metadata": "SECRET_STATE_METADATA"}
    state.stages["logs"] = StageResult(
        status=StageStatus.SUCCEEDED,
        output={"raw_output": "SECRET_STAGE_OUTPUT"},
    )
    client = _client(MockRepository({state.incident_id: state}))

    resp = client.get(f"/investigations/{state.incident_id}/result")
    assert resp.status_code == 200
    raw = resp.text

    for secret in (
        "SECRET_STATE_METADATA",
        "SECRET_STAGE_OUTPUT",
        "SECRET_EVIDENCE_METADATA",
        "ground_truth",
    ):
        assert secret not in raw, f"leaked internal data: {secret}"

    body = resp.json()
    # Only the four allowlisted sections (plus envelope fields) are present.
    assert set(body.keys()) == {
        "investigation_id",
        "status",
        "current_stage",
        "completed_at",
        "result_available",
        "hypotheses",
        "evidence",
        "verification",
        "proposals",
    }
    # Raw file bodies and free-form metadata are not projected.
    assert "metadata" not in body["evidence"][0]
    assert set(body["evidence"][0].keys()) == {
        "evidence_id",
        "source",
        "type",
        "reference",
        "description",
        "excerpt",
        "timestamp",
        "interpretation",
        "metric",
        "value",
    }
    changes = body["proposals"][0]["changes"][0]
    assert set(changes.keys()) == {"file", "description", "start_line", "end_line"}
    assert "before" not in changes and "after" not in changes


# ---------------------------------------------------------------------------
# 6. Pre-existing contracts remain intact
# ---------------------------------------------------------------------------

def test_existing_get_investigation_contract_is_unchanged():
    state = _build_completed_state("inc_contract")
    state.version = 3
    client = _client(MockRepository({"inc_contract": state}))

    resp = client.get("/investigations/inc_contract")
    assert resp.status_code == 200
    body = resp.json()

    assert set(body.keys()) == {
        "investigation_id",
        "status",
        "current_stage",
        "started_at",
        "completed_at",
        "error",
        "llm_call_count",
        "version",
        "lease_owner",
        "lease_expires_at",
        "stages",
        "approvals",
    }
    # Stage output stays omitted on the pre-existing endpoint.
    assert "output" not in body["stages"]["verification"]


def test_existing_list_and_health_routes_still_work():
    state = _build_completed_state("inc_other")
    client = _client(MockRepository({"inc_other": state}))

    health = client.get("/health")
    assert health.status_code == 200
    assert health.json()["status"] == "ok"

    listing = client.get("/investigations")
    assert listing.status_code == 200
    assert listing.json() == {"investigations": ["inc_other"]}
