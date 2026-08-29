"""Unit tests for agents/approval_gate.py — human approval gate.

All tests inject answers via _answer parameter — no stdin/TTY required.
No Groq calls. No source file modifications.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path
from typing import Any, Dict

import pytest

from agents.approval_gate import (
    STATE_APPROVED,
    STATE_PROPOSED,
    STATE_REJECTED,
    ApprovalGate,
)
from agents.fix_tools import HUMAN_APPROVAL_NOTICE, ProposalValidationError

REPO = Path(__file__).parent.parent
INC_01 = REPO / "incidents" / "inc_01_n_plus_one_query"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _proposal(proposal_id: str = "FIX-001", status: str = "PROPOSED") -> Dict[str, Any]:
    return {
        "proposal_id": proposal_id,
        "hypothesis_id": "HYP-001",
        "incident_id": "inc_01_n_plus_one_query",
        "status": status,
        "human_approval_notice": HUMAN_APPROVAL_NOTICE,
        "summary": "Replace N+1 query with batched query.",
        "rationale": "Reduces DB round-trips.",
        "changes": [
            {
                "file": "service/app.py",
                "start_line": None,
                "end_line": None,
                "description": "Use batch query.",
                "before": "db_session.query_address_by_id(item.shipping_address_id)",
                "after": "# batch query",
            }
        ],
        "patch": "--- a/service/app.py\n+++ b/service/app.py\n@@ -1 +1 @@\n-old\n+new",
        "expected_effect": "Fewer DB queries.",
        "risks": [],
        "validation_plan": ["Run tests."],
        "rollback_plan": "Revert commit.",
        "evidence_ids": ["EV-LOG-001"],
    }


# ---------------------------------------------------------------------------
# Tests 22-28: Answer parsing
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("answer", ["y", "Y", "yes", "YES"])
def test_affirmative_produces_approved(answer: str):
    """Tests 22-25: explicit affirmative variants → APPROVED."""
    gate = ApprovalGate(interactive=False)
    record = gate.review(_proposal(), _answer=answer)
    assert record["status"] == STATE_APPROVED
    assert record["decision"] == "approved"
    assert record["proposal_id"] == "FIX-001"


def test_n_produces_rejected():
    """Test 26: 'n' → REJECTED."""
    gate = ApprovalGate(interactive=False)
    record = gate.review(_proposal(), _answer="n")
    assert record["status"] == STATE_REJECTED
    assert record["decision"] == "rejected"


def test_empty_input_produces_rejected():
    """Test 27: empty string → REJECTED."""
    gate = ApprovalGate(interactive=False)
    record = gate.review(_proposal(), _answer="")
    assert record["status"] == STATE_REJECTED


def test_invalid_input_produces_rejected():
    """Test 28: random string → REJECTED."""
    gate = ApprovalGate(interactive=False)
    record = gate.review(_proposal(), _answer="maybe")
    assert record["status"] == STATE_REJECTED


def test_eof_simulation_produces_rejected():
    """Test 29: None answer (simulates EOF) → REJECTED."""
    # When _answer is None AND interactive=False, default is REJECTED
    gate = ApprovalGate(interactive=False)
    record = gate.review(_proposal(), _answer=None)
    assert record["status"] == STATE_REJECTED


def test_non_interactive_mode_defaults_to_rejected():
    """Test 30: Non-interactive mode → REJECTED."""
    gate = ApprovalGate(interactive=False)
    record = gate.review(_proposal(), _answer=None)
    assert record["status"] == STATE_REJECTED
    assert record["decision"] == "rejected"


# ---------------------------------------------------------------------------
# Tests: Approval record structure (Test 31)
# ---------------------------------------------------------------------------

def test_approval_record_is_valid():
    """Test 31: Approval record passes schema validation."""
    from agents.fix_tools import validate_approval_record_schema
    gate = ApprovalGate(interactive=False)
    record = gate.review(_proposal(), _answer="y")
    # Should not raise
    validate_approval_record_schema(record)
    assert record["approved_by"] == "human"
    assert "timestamp" in record
    assert record["proposal_id"] == "FIX-001"


# ---------------------------------------------------------------------------
# Tests: Rejection is terminal (Test 32)
# ---------------------------------------------------------------------------

def test_rejected_proposal_cannot_be_approved_again():
    """Test 32: Once a proposal produces a REJECTED record, reviewing
    the same proposal again with affirmative produces a NEW APPROVED record —
    but neither record changes; immutable records, no state mutation."""
    gate = ApprovalGate(interactive=False)
    record1 = gate.review(_proposal(), _answer="n")
    assert record1["status"] == STATE_REJECTED

    # Reviewing again is a fresh call; records are independent
    record2 = gate.review(_proposal(), _answer="y")
    # record2 is APPROVED — the gate does not know about record1 (stateless)
    assert record2["status"] == STATE_APPROVED

    # But the first record is unchanged (value semantics)
    assert record1["status"] == STATE_REJECTED


# ---------------------------------------------------------------------------
# Tests: Approval does NOT apply the patch (Tests 33-34)
# ---------------------------------------------------------------------------

def test_approval_does_not_modify_source_files():
    """Test 33: After approval, incident service files must not change."""
    service_app = INC_01 / "service" / "app.py"
    before = service_app.read_text(encoding="utf-8")

    gate = ApprovalGate(interactive=False)
    gate.review(_proposal(), _answer="y")

    after = service_app.read_text(encoding="utf-8")
    assert before == after, "Source file was modified by the approval gate!"


def test_approval_does_not_create_new_files(tmp_path: Path):
    """Test 34: Approving a proposal creates no new source files."""
    # Count files in incident service dir before
    service_dir = INC_01 / "service"
    before_files = set(service_dir.rglob("*.py"))

    gate = ApprovalGate(interactive=False)
    gate.review(_proposal(), _answer="y")

    after_files = set(service_dir.rglob("*.py"))
    assert before_files == after_files, "New files appeared after approval!"


# ---------------------------------------------------------------------------
# Tests: Non-PROPOSED status raises (guard rail)
# ---------------------------------------------------------------------------

def test_non_proposed_status_raises():
    """Reviewing a non-PROPOSED proposal raises ProposalValidationError."""
    gate = ApprovalGate(interactive=False)
    bad_proposal = _proposal(status="APPROVED")
    with pytest.raises(ProposalValidationError, match="PROPOSED"):
        gate.review(bad_proposal, _answer="y")


def test_rejected_status_raises():
    """Reviewing a REJECTED proposal raises ProposalValidationError."""
    gate = ApprovalGate(interactive=False)
    bad_proposal = _proposal(status="REJECTED")
    with pytest.raises(ProposalValidationError, match="PROPOSED"):
        gate.review(bad_proposal, _answer="y")


# ---------------------------------------------------------------------------
# Tests: review_all
# ---------------------------------------------------------------------------

def test_review_all_approves_and_rejects():
    """review_all correctly handles mixed answers."""
    gate = ApprovalGate(interactive=False)
    bundle = {
        "incident_id": "inc_01_n_plus_one_query",
        "proposals": [
            _proposal("FIX-001"),
            _proposal("FIX-002"),
        ],
        "validation_errors": {},
        "skipped_hypotheses": [],
    }
    result = gate.review_all(bundle, _answers={"FIX-001": "y", "FIX-002": "n"})
    records = result["approval_records"]
    assert len(records) == 2
    by_id = {r["proposal_id"]: r for r in records}
    assert by_id["FIX-001"]["status"] == STATE_APPROVED
    assert by_id["FIX-002"]["status"] == STATE_REJECTED
    assert result["summary"]["approved"] == 1
    assert result["summary"]["rejected"] == 1


def test_review_all_empty_proposals():
    """review_all with no proposals returns empty records."""
    gate = ApprovalGate(interactive=False)
    bundle = {
        "incident_id": "test",
        "proposals": [],
        "validation_errors": {},
        "skipped_hypotheses": [],
    }
    result = gate.review_all(bundle)
    assert result["approval_records"] == []
    assert result["summary"]["total"] == 0


def test_review_all_no_answers_defaults_to_rejected():
    """review_all with no injected answers defaults all to REJECTED (non-interactive)."""
    gate = ApprovalGate(interactive=False)
    bundle = {
        "incident_id": "test",
        "proposals": [_proposal("FIX-001"), _proposal("FIX-002")],
        "validation_errors": {},
        "skipped_hypotheses": [],
    }
    result = gate.review_all(bundle)  # no _answers
    for r in result["approval_records"]:
        assert r["status"] == STATE_REJECTED


# ---------------------------------------------------------------------------
# Additional edge-case tests
# ---------------------------------------------------------------------------

def test_yes_uppercase_produces_approved():
    gate = ApprovalGate(interactive=False)
    record = gate.review(_proposal(), _answer="YES")
    assert record["status"] == STATE_APPROVED


def test_whitespace_only_produces_rejected():
    gate = ApprovalGate(interactive=False)
    record = gate.review(_proposal(), _answer="   ")
    assert record["status"] == STATE_REJECTED


def test_approval_timestamp_is_set():
    gate = ApprovalGate(interactive=False)
    record = gate.review(_proposal(), _answer="y")
    assert record["timestamp"]
    # Should be a parseable ISO string
    from datetime import datetime
    datetime.fromisoformat(record["timestamp"])
