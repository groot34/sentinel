"""Unit tests for agents/fix_tools.py — deterministic safety validator.

All checks are pure-Python with no Groq calls.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Set

import pytest

from agents.fix_tools import (
    HUMAN_APPROVAL_NOTICE,
    ProposalValidationError,
    check_approval_notice_present,
    check_evidence_ids_exist,
    check_hypothesis_eligible,
    check_hypothesis_exists,
    check_patch_not_destructive,
    check_patch_targets_allowed_files,
    check_proposal_not_claiming_applied,
    check_referenced_files_exist,
    check_source_locations_valid,
    check_status_is_proposed,
    collect_all_evidence_ids,
    filter_confirmed_hypotheses,
    validate_proposal,
)

REPO = Path(__file__).parent.parent
INC_01 = REPO / "incidents" / "inc_01_n_plus_one_query"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _minimal_proposal(overrides: Dict[str, Any] | None = None) -> Dict[str, Any]:
    p: Dict[str, Any] = {
        "proposal_id": "FIX-001",
        "hypothesis_id": "HYP-001",
        "incident_id": "inc_01_n_plus_one_query",
        "status": "PROPOSED",
        "human_approval_notice": HUMAN_APPROVAL_NOTICE,
        "summary": "Replace per-item DB query with a single batched query.",
        "rationale": "The N+1 query pattern exhausts the connection pool.",
        "changes": [
            {
                "file": "service/app.py",
                "start_line": None,
                "end_line": None,
                "description": "Replace serialize loop with batch query.",
                "before": "address = db_session.query_address_by_id(item.shipping_address_id)",
                "after": "# use batch query instead",
            }
        ],
        "patch": "--- a/service/app.py\n+++ b/service/app.py\n@@ -1 +1 @@\n-old\n+new",
        "expected_effect": "Reduces DB queries from O(N) to O(1) per request.",
        "risks": ["Potential change in address ordering."],
        "validation_plan": ["Run existing unit tests."],
        "rollback_plan": "Revert to the previous commit.",
        "evidence_ids": ["EV-LOG-001", "EV-CODE-001"],
    }
    if overrides:
        p.update(overrides)
    return p


def _hypotheses_bundle(hyp_id: str = "HYP-001") -> Dict[str, Any]:
    return {
        "incident_id": "inc_01_n_plus_one_query",
        "hypotheses": [
            {
                "hypothesis_id": hyp_id,
                "claim": "N+1 query pattern causes pool exhaustion.",
                "evidence_ids": ["EV-LOG-001", "EV-CODE-001"],
                "supporting_reasoning": "Logs show 250 queries per request.",
                "falsification_criteria": ["If query count is constant, reject."],
                "verification_plan": ["Check query count per request."],
            }
        ],
    }


def _verification_results(hyp_id: str = "HYP-001", verdict: str = "CONFIRMED") -> Dict[str, Any]:
    return {
        "incident_id": "inc_01_n_plus_one_query",
        "results": [
            {
                "hypothesis_id": hyp_id,
                "verdict": verdict,
                "checks": [],
                "reasoning": "All checks passed.",
                "confidence": 0.9,
            }
        ],
    }


def _evidence_ids() -> Set[str]:
    return {"EV-LOG-001", "EV-LOG-002", "EV-CODE-001", "EV-CODE-002"}


# ---------------------------------------------------------------------------
# Tests: check_approval_notice_present
# ---------------------------------------------------------------------------

def test_correct_approval_notice_passes():
    p = _minimal_proposal()
    check_approval_notice_present(p)  # no exception


def test_wrong_approval_notice_raises():
    p = _minimal_proposal({"human_approval_notice": "approved"})
    with pytest.raises(ProposalValidationError, match="human_approval_notice"):
        check_approval_notice_present(p)


def test_missing_approval_notice_raises():
    p = _minimal_proposal()
    del p["human_approval_notice"]
    with pytest.raises(ProposalValidationError):
        check_approval_notice_present(p)


# ---------------------------------------------------------------------------
# Tests: check_status_is_proposed
# ---------------------------------------------------------------------------

def test_status_proposed_passes():
    check_status_is_proposed(_minimal_proposal())


def test_status_approved_raises():
    with pytest.raises(ProposalValidationError, match="PROPOSED"):
        check_status_is_proposed(_minimal_proposal({"status": "APPROVED"}))


def test_status_missing_raises():
    p = _minimal_proposal()
    del p["status"]
    with pytest.raises(ProposalValidationError):
        check_status_is_proposed(p)


# ---------------------------------------------------------------------------
# Tests: check_hypothesis_exists
# ---------------------------------------------------------------------------

def test_existing_hypothesis_passes():
    check_hypothesis_exists(_minimal_proposal(), _hypotheses_bundle())


def test_unknown_hypothesis_raises():
    p = _minimal_proposal({"hypothesis_id": "HYP-999"})
    with pytest.raises(ProposalValidationError, match="HYP-999"):
        check_hypothesis_exists(p, _hypotheses_bundle())


# ---------------------------------------------------------------------------
# Tests: check_hypothesis_eligible
# ---------------------------------------------------------------------------

def test_confirmed_hypothesis_eligible():
    check_hypothesis_eligible(_minimal_proposal(), _verification_results(verdict="CONFIRMED"))


def test_rejected_hypothesis_raises():
    p = _minimal_proposal()
    with pytest.raises(ProposalValidationError, match="REJECTED"):
        check_hypothesis_eligible(p, _verification_results(verdict="REJECTED"))


def test_inconclusive_hypothesis_raises():
    p = _minimal_proposal()
    with pytest.raises(ProposalValidationError, match="INCONCLUSIVE"):
        check_hypothesis_eligible(p, _verification_results(verdict="INCONCLUSIVE"))


def test_missing_verification_result_raises():
    p = _minimal_proposal()
    with pytest.raises(ProposalValidationError, match="no verification result"):
        check_hypothesis_eligible(p, {"incident_id": "x", "results": []})


# ---------------------------------------------------------------------------
# Tests: check_evidence_ids_exist
# ---------------------------------------------------------------------------

def test_valid_evidence_ids_pass():
    check_evidence_ids_exist(_minimal_proposal(), _evidence_ids())


def test_unknown_evidence_id_raises():
    p = _minimal_proposal({"evidence_ids": ["EV-LOG-001", "EV-FAKE-999"]})
    with pytest.raises(ProposalValidationError, match="EV-FAKE-999"):
        check_evidence_ids_exist(p, _evidence_ids())


def test_empty_evidence_ids_raises():
    p = _minimal_proposal({"evidence_ids": []})
    with pytest.raises(ProposalValidationError, match="at least one"):
        check_evidence_ids_exist(p, _evidence_ids())


# ---------------------------------------------------------------------------
# Tests: check_referenced_files_exist
# ---------------------------------------------------------------------------

def test_existing_service_file_passes():
    p = _minimal_proposal()
    check_referenced_files_exist(p, INC_01)


def test_nonexistent_file_raises():
    p = _minimal_proposal()
    p["changes"][0]["file"] = "service/nonexistent_file_xyz.py"
    with pytest.raises(ProposalValidationError, match="does not exist"):
        check_referenced_files_exist(p, INC_01)


# ---------------------------------------------------------------------------
# Tests: check_source_locations_valid
# ---------------------------------------------------------------------------

def test_null_line_numbers_pass():
    p = _minimal_proposal()
    check_source_locations_valid(p, INC_01)  # start_line/end_line are None


def test_valid_line_numbers_pass():
    p = _minimal_proposal()
    p["changes"][0]["start_line"] = 1
    p["changes"][0]["end_line"] = 5
    check_source_locations_valid(p, INC_01)


def test_end_line_exceeds_file_raises():
    p = _minimal_proposal()
    p["changes"][0]["start_line"] = 1
    p["changes"][0]["end_line"] = 99999
    with pytest.raises(ProposalValidationError, match="exceeds file length"):
        check_source_locations_valid(p, INC_01)


def test_start_gt_end_raises():
    p = _minimal_proposal()
    p["changes"][0]["start_line"] = 10
    p["changes"][0]["end_line"] = 5
    with pytest.raises(ProposalValidationError, match="start_line"):
        check_source_locations_valid(p, INC_01)


def test_zero_start_line_raises():
    p = _minimal_proposal()
    p["changes"][0]["start_line"] = 0
    p["changes"][0]["end_line"] = 5
    with pytest.raises(ProposalValidationError, match="less than 1"):
        check_source_locations_valid(p, INC_01)


# ---------------------------------------------------------------------------
# Tests: check_patch_targets_allowed_files
# ---------------------------------------------------------------------------

def test_service_prefix_allowed():
    p = _minimal_proposal()
    check_patch_targets_allowed_files(p)  # service/app.py → OK


def test_ground_truth_file_rejected():
    p = _minimal_proposal()
    p["changes"][0]["file"] = "ground_truth.md"
    with pytest.raises(ProposalValidationError, match="outside the allowed"):
        check_patch_targets_allowed_files(p)


def test_eval_dir_file_rejected():
    p = _minimal_proposal()
    p["changes"][0]["file"] = "eval/results/something.csv"
    with pytest.raises(ProposalValidationError, match="outside the allowed"):
        check_patch_targets_allowed_files(p)


def test_tests_dir_file_rejected():
    p = _minimal_proposal()
    p["changes"][0]["file"] = "tests/test_something.py"
    with pytest.raises(ProposalValidationError, match="outside the allowed"):
        check_patch_targets_allowed_files(p)


# ---------------------------------------------------------------------------
# Tests: check_patch_not_destructive
# ---------------------------------------------------------------------------

def test_clean_patch_passes():
    check_patch_not_destructive(_minimal_proposal())


@pytest.mark.parametrize("bad_content,field", [
    ("import subprocess; subprocess.run(['rm', '-rf', '/'])", "patch"),
    ("os.system('rm -rf /')", "patch"),
    ("DROP TABLE users", "patch"),
    ("eval('__import__(\"os\").system(\"ls\")')", "patch"),
])
def test_destructive_content_raises(bad_content: str, field: str):
    p = _minimal_proposal({field: bad_content})
    with pytest.raises(ProposalValidationError, match="destructive"):
        check_patch_not_destructive(p)


def test_destructive_content_in_after_raises():
    p = _minimal_proposal()
    p["changes"][0]["after"] = "subprocess.call(['rm', '-rf', '/'])"
    with pytest.raises(ProposalValidationError, match="destructive"):
        check_patch_not_destructive(p)


# ---------------------------------------------------------------------------
# Tests: check_proposal_not_claiming_applied
# ---------------------------------------------------------------------------

def test_non_applied_claim_passes():
    check_proposal_not_claiming_applied(_minimal_proposal())


@pytest.mark.parametrize("bad_text", [
    "The fix has been applied.",
    "patch applied to production",
    "changes committed to main",
    "already applied to the codebase",
])
def test_applied_claim_raises(bad_text: str):
    p = _minimal_proposal({"summary": bad_text})
    with pytest.raises(ProposalValidationError, match="claim"):
        check_proposal_not_claiming_applied(p)


# ---------------------------------------------------------------------------
# Tests: validate_proposal (master validator)
# ---------------------------------------------------------------------------

def test_full_valid_proposal_passes():
    p = _minimal_proposal()
    is_valid, errors = validate_proposal(
        proposal=p,
        incident_dir=INC_01,
        available_hypotheses=_hypotheses_bundle(),
        verification_results=_verification_results(),
        all_evidence_ids=_evidence_ids(),
    )
    assert is_valid, f"Expected valid but got errors: {errors}"
    assert errors == []


def test_rejected_hypothesis_fails_master_validation():
    p = _minimal_proposal()
    is_valid, errors = validate_proposal(
        proposal=p,
        incident_dir=INC_01,
        available_hypotheses=_hypotheses_bundle(),
        verification_results=_verification_results(verdict="REJECTED"),
        all_evidence_ids=_evidence_ids(),
    )
    assert not is_valid
    assert any("REJECTED" in e for e in errors)


def test_invalid_file_ref_fails_master_validation():
    p = _minimal_proposal()
    p["changes"][0]["file"] = "service/does_not_exist.py"
    is_valid, errors = validate_proposal(
        proposal=p,
        incident_dir=INC_01,
        available_hypotheses=_hypotheses_bundle(),
        verification_results=_verification_results(),
        all_evidence_ids=_evidence_ids(),
    )
    assert not is_valid
    assert any("does not exist" in e for e in errors)


# ---------------------------------------------------------------------------
# Tests: collect_all_evidence_ids
# ---------------------------------------------------------------------------

def test_collect_ids_from_three_bundles():
    logs = {"evidence": [{"evidence_id": "EV-LOG-001"}, {"evidence_id": "EV-LOG-002"}]}
    metrics = {"evidence": [{"evidence_id": "EV-MET-001"}]}
    code = {"evidence": [{"evidence_id": "EV-CODE-001"}]}
    ids = collect_all_evidence_ids(logs, metrics, code)
    assert ids == {"EV-LOG-001", "EV-LOG-002", "EV-MET-001", "EV-CODE-001"}


def test_collect_ids_handles_none():
    ids = collect_all_evidence_ids(None, None, None)
    assert ids == set()


# ---------------------------------------------------------------------------
# Tests: filter_confirmed_hypotheses
# ---------------------------------------------------------------------------

def test_filter_returns_only_confirmed():
    hyps = {
        "hypotheses": [
            {"hypothesis_id": "HYP-001", "claim": "A"},
            {"hypothesis_id": "HYP-002", "claim": "B"},
            {"hypothesis_id": "HYP-003", "claim": "C"},
        ]
    }
    ver = {
        "results": [
            {"hypothesis_id": "HYP-001", "verdict": "CONFIRMED"},
            {"hypothesis_id": "HYP-002", "verdict": "REJECTED"},
            {"hypothesis_id": "HYP-003", "verdict": "INCONCLUSIVE"},
        ]
    }
    confirmed = filter_confirmed_hypotheses(hyps, ver)
    assert len(confirmed) == 1
    assert confirmed[0]["hypothesis_id"] == "HYP-001"


def test_filter_returns_empty_when_none_confirmed():
    hyps = {"hypotheses": [{"hypothesis_id": "HYP-001", "claim": "A"}]}
    ver = {"results": [{"hypothesis_id": "HYP-001", "verdict": "REJECTED"}]}
    assert filter_confirmed_hypotheses(hyps, ver) == []
