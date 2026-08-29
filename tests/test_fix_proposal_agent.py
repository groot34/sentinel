"""Unit tests for agents/fix_proposal_agent.py.

All LLM calls are mocked — no Groq API calls are made.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path
from typing import Any, Dict
from unittest.mock import MagicMock, patch

import pytest

from agents.fix_proposal_agent import FixProposalAgent
from agents.fix_tools import HUMAN_APPROVAL_NOTICE
from core.llm import GroqLLMClient, LLMJSONParseError, LLMResponse

REPO = Path(__file__).parent.parent
INC_01 = REPO / "incidents" / "inc_01_n_plus_one_query"
INC_04 = REPO / "incidents" / "inc_04_memory_leak"
INC_07 = REPO / "incidents" / "inc_07_retry_storm"
INC_10 = REPO / "incidents" / "inc_10_multi_symptom_cascade"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _mock_llm_response(proposal: Dict[str, Any]) -> LLMResponse:
    """Return a mock LLMResponse containing the given proposal JSON."""
    content = json.dumps(proposal)
    resp = MagicMock(spec=LLMResponse)
    resp.content = content
    resp.parsed_json = proposal
    resp.get_structured.return_value = proposal
    return resp


def _mock_client(proposal: Dict[str, Any]) -> GroqLLMClient:
    """Return a GroqLLMClient mock that returns the given proposal."""
    client = MagicMock(spec=GroqLLMClient)
    client.generate_structured.return_value = _mock_llm_response(proposal)
    return client


def _good_proposal_from_llm(
    proposal_id: str = "FIX-001",
    hypothesis_id: str = "HYP-001",
    incident_id: str = "inc_01_n_plus_one_query",
    ev_ids: list | None = None,
) -> Dict[str, Any]:
    return {
        "proposal_id": proposal_id,
        "hypothesis_id": hypothesis_id,
        "incident_id": incident_id,
        "status": "PROPOSED",
        "human_approval_notice": HUMAN_APPROVAL_NOTICE,
        "summary": "Replace per-item query with batch query.",
        "rationale": "The N+1 query pattern exhausts the connection pool.",
        "changes": [
            {
                "file": "service/app.py",
                "start_line": None,
                "end_line": None,
                "description": "Batch queries.",
                "before": "address = db_session.query_address_by_id(item.shipping_address_id)",
                "after": "# batch call",
            }
        ],
        "patch": "--- a/service/app.py\n+++ b/service/app.py\n@@ -1 +1 @@\n-old\n+new",
        "expected_effect": "O(1) DB queries per request.",
        "risks": ["Ordering may change."],
        "validation_plan": ["Run unit tests."],
        "rollback_plan": "Revert via git.",
        "evidence_ids": ev_ids or ["EV-LOG-001", "EV-CODE-001"],
    }


def _hypotheses(
    hyp_id: str = "HYP-001",
    ev_ids: list | None = None,
) -> Dict[str, Any]:
    return {
        "incident_id": "inc_01_n_plus_one_query",
        "hypotheses": [
            {
                "hypothesis_id": hyp_id,
                "claim": "N+1 query exhausts pool.",
                "evidence_ids": ev_ids or ["EV-LOG-001", "EV-CODE-001"],
                "supporting_reasoning": "250 queries per request.",
                "falsification_criteria": ["constant query count"],
                "verification_plan": ["check query count"],
            }
        ],
    }


def _verification(hyp_id: str = "HYP-001", verdict: str = "CONFIRMED") -> Dict[str, Any]:
    return {
        "incident_id": "inc_01_n_plus_one_query",
        "results": [
            {
                "hypothesis_id": hyp_id,
                "verdict": verdict,
                "checks": [{"check_id": "CHK-001", "description": "check", "result": "PASS",
                             "evidence": ["EV-LOG-001"], "reference": "service/app.py:22"}],
                "reasoning": "All checks passed.",
                "confidence": 0.9,
            }
        ],
    }


def _logs() -> Dict[str, Any]:
    return {
        "incident_id": "inc_01_n_plus_one_query",
        "evidence": [
            {"evidence_id": "EV-LOG-001", "source": "logs", "reference": "logs/app.log:1",
             "excerpt": "pool exhausted", "type": "error", "interpretation": "pool empty"},
            {"evidence_id": "EV-LOG-002", "source": "logs", "reference": "logs/app.log:2",
             "excerpt": "timeout", "type": "error", "interpretation": "slow"},
        ],
    }


def _code() -> Dict[str, Any]:
    return {
        "incident_id": "inc_01_n_plus_one_query",
        "evidence": [
            {"evidence_id": "EV-CODE-001", "source": "code", "reference": "service/app.py:22",
             "excerpt": "query_address_by_id", "type": "code_pattern", "interpretation": "N+1"},
            {"evidence_id": "EV-CODE-002", "source": "code", "reference": "service/app.py:25",
             "excerpt": "for item in order.items", "type": "code_pattern", "interpretation": "loop"},
        ],
    }


# ---------------------------------------------------------------------------
# Test 1: Confirmed hypothesis produces a proposal
# ---------------------------------------------------------------------------

def test_confirmed_hypothesis_produces_proposal():
    """Test 1: CONFIRMED hypothesis → proposal generated."""
    client = _mock_client(_good_proposal_from_llm())
    agent = FixProposalAgent(llm_client=client)
    result = agent.propose_fix(
        incident_dir=INC_01,
        hypotheses=_hypotheses(),
        verification_results=_verification(verdict="CONFIRMED"),
        logs_evidence=_logs(),
        code_evidence=_code(),
    )
    assert len(result["proposals"]) == 1
    assert result["proposals"][0]["proposal_id"] == "FIX-001"
    assert result["proposals"][0]["status"] == "PROPOSED"


# ---------------------------------------------------------------------------
# Test 2: Rejected hypothesis cannot produce a proposal
# ---------------------------------------------------------------------------

def test_rejected_hypothesis_produces_no_proposal():
    """Test 2: REJECTED hypothesis → no proposals."""
    client = _mock_client(_good_proposal_from_llm())
    agent = FixProposalAgent(llm_client=client)
    result = agent.propose_fix(
        incident_dir=INC_01,
        hypotheses=_hypotheses(),
        verification_results=_verification(verdict="REJECTED"),
        logs_evidence=_logs(),
        code_evidence=_code(),
    )
    assert result["proposals"] == []
    assert any("REJECTED" in s.get("verdict", "") for s in result["skipped_hypotheses"])
    client.generate_structured.assert_not_called()


# ---------------------------------------------------------------------------
# Test 3: Inconclusive hypothesis is not presented as confirmed
# ---------------------------------------------------------------------------

def test_inconclusive_hypothesis_produces_no_proposal():
    """Test 3: INCONCLUSIVE hypothesis → no proposals, skipped."""
    client = _mock_client(_good_proposal_from_llm())
    agent = FixProposalAgent(llm_client=client)
    result = agent.propose_fix(
        incident_dir=INC_01,
        hypotheses=_hypotheses(),
        verification_results=_verification(verdict="INCONCLUSIVE"),
        logs_evidence=_logs(),
        code_evidence=_code(),
    )
    assert result["proposals"] == []
    assert any("INCONCLUSIVE" in s.get("verdict", "") for s in result["skipped_hypotheses"])


# ---------------------------------------------------------------------------
# Tests 4-5: Evidence and hypothesis IDs must exist
# ---------------------------------------------------------------------------

def test_evidence_ids_used_from_hypothesis():
    """Test 4: Proposal uses evidence IDs from the confirmed hypothesis."""
    ev_ids = ["EV-LOG-001", "EV-CODE-001"]
    client = _mock_client(_good_proposal_from_llm(ev_ids=ev_ids))
    agent = FixProposalAgent(llm_client=client)
    result = agent.propose_fix(
        incident_dir=INC_01,
        hypotheses=_hypotheses(ev_ids=ev_ids),
        verification_results=_verification(verdict="CONFIRMED"),
        logs_evidence=_logs(),
        code_evidence=_code(),
    )
    proposal = result["proposals"][0]
    for eid in proposal["evidence_ids"]:
        assert eid in ("EV-LOG-001", "EV-LOG-002", "EV-CODE-001", "EV-CODE-002")


def test_hypothesis_id_preserved_in_proposal():
    """Test 5: Proposal's hypothesis_id matches the confirmed hypothesis."""
    client = _mock_client(_good_proposal_from_llm())
    agent = FixProposalAgent(llm_client=client)
    result = agent.propose_fix(
        incident_dir=INC_01,
        hypotheses=_hypotheses(),
        verification_results=_verification(verdict="CONFIRMED"),
        logs_evidence=_logs(),
        code_evidence=_code(),
    )
    assert result["proposals"][0]["hypothesis_id"] == "HYP-001"


# ---------------------------------------------------------------------------
# Test 7: Invalid file reference flagged in validation_errors
# ---------------------------------------------------------------------------

def test_invalid_file_reference_caught_in_validation():
    """Test 7: A proposal referencing a nonexistent file is flagged."""
    bad = _good_proposal_from_llm()
    bad["changes"][0]["file"] = "service/nonexistent_xyz.py"
    client = _mock_client(bad)
    agent = FixProposalAgent(llm_client=client)
    result = agent.propose_fix(
        incident_dir=INC_01,
        hypotheses=_hypotheses(),
        verification_results=_verification(verdict="CONFIRMED"),
        logs_evidence=_logs(),
        code_evidence=_code(),
    )
    # Proposal is produced but validation errors recorded
    assert "FIX-001" in result["validation_errors"]
    errors = result["validation_errors"]["FIX-001"]
    assert any("does not exist" in e for e in errors)


# ---------------------------------------------------------------------------
# Test 8: Invalid line reference flagged
# ---------------------------------------------------------------------------

def test_invalid_line_reference_caught():
    """Test 8: Proposal with start_line > end_line is flagged."""
    bad = _good_proposal_from_llm()
    bad["changes"][0]["start_line"] = 100
    bad["changes"][0]["end_line"] = 5
    client = _mock_client(bad)
    agent = FixProposalAgent(llm_client=client)
    result = agent.propose_fix(
        incident_dir=INC_01,
        hypotheses=_hypotheses(),
        verification_results=_verification(verdict="CONFIRMED"),
        logs_evidence=_logs(),
        code_evidence=_code(),
    )
    assert "FIX-001" in result["validation_errors"]


# ---------------------------------------------------------------------------
# Test 9: Malformed LLM response handled gracefully
# ---------------------------------------------------------------------------

def test_malformed_llm_response_handled():
    """Test 9: If LLM returns malformed JSON, agent still returns a proposal (normalised)."""
    client = MagicMock(spec=GroqLLMClient)
    resp = MagicMock(spec=LLMResponse)
    resp.get_structured.side_effect = LLMJSONParseError("bad json")
    client.generate_structured.return_value = resp
    agent = FixProposalAgent(llm_client=client)
    result = agent.propose_fix(
        incident_dir=INC_01,
        hypotheses=_hypotheses(),
        verification_results=_verification(verdict="CONFIRMED"),
        logs_evidence=_logs(),
        code_evidence=_code(),
    )
    # Should not raise; proposal list still exists
    assert isinstance(result["proposals"], list)


# ---------------------------------------------------------------------------
# Test 10: Exactly one Groq call per CONFIRMED hypothesis
# ---------------------------------------------------------------------------

def test_exactly_one_groq_call_per_confirmed_hypothesis():
    """Test 10: One Groq call for one confirmed hypothesis."""
    client = _mock_client(_good_proposal_from_llm())
    agent = FixProposalAgent(llm_client=client)
    agent.propose_fix(
        incident_dir=INC_01,
        hypotheses=_hypotheses(),
        verification_results=_verification(verdict="CONFIRMED"),
        logs_evidence=_logs(),
        code_evidence=_code(),
    )
    assert client.generate_structured.call_count == 1


def test_no_groq_call_when_no_confirmed():
    """Test 10b: Zero Groq calls when no hypothesis is CONFIRMED."""
    client = _mock_client(_good_proposal_from_llm())
    agent = FixProposalAgent(llm_client=client)
    agent.propose_fix(
        incident_dir=INC_01,
        hypotheses=_hypotheses(),
        verification_results=_verification(verdict="REJECTED"),
        logs_evidence=_logs(),
        code_evidence=_code(),
    )
    client.generate_structured.assert_not_called()


# ---------------------------------------------------------------------------
# Test 11: No direct Groq import in fix_proposal_agent
# ---------------------------------------------------------------------------

def test_no_direct_groq_import():
    """Test 11: fix_proposal_agent.py must not import 'groq' directly."""
    module_path = REPO / "agents" / "fix_proposal_agent.py"
    source = module_path.read_text(encoding="utf-8")
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert not alias.name.startswith("groq"), \
                    "fix_proposal_agent.py must not import 'groq' directly."
        if isinstance(node, ast.ImportFrom):
            assert node.module != "groq" and not (node.module or "").startswith("groq."), \
                "fix_proposal_agent.py must not import from 'groq' directly."


# ---------------------------------------------------------------------------
# Test 12: Uses core.llm
# ---------------------------------------------------------------------------

def test_uses_core_llm():
    """Test 12: fix_proposal_agent.py must import from core.llm."""
    module_path = REPO / "agents" / "fix_proposal_agent.py"
    source = module_path.read_text(encoding="utf-8")
    assert "from core.llm" in source or "import core.llm" in source, \
        "fix_proposal_agent.py must import from core.llm."


# ---------------------------------------------------------------------------
# Test 13: ground_truth.md never opened/read
# ---------------------------------------------------------------------------

def test_ground_truth_never_read():
    """Test 13: fix_proposal_agent.py must not open or import ground_truth.md."""
    module_path = REPO / "agents" / "fix_proposal_agent.py"
    source = module_path.read_text(encoding="utf-8")
    tree = ast.parse(source)
    # No open("...ground_truth...") call, no Path("...ground_truth..."), no read of that file
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            assert "ground_truth.md" not in node.value, \
                "fix_proposal_agent.py must not reference the path 'ground_truth.md'."


# ---------------------------------------------------------------------------
# Test 14: Baseline results never read
# ---------------------------------------------------------------------------

def test_baseline_results_never_read():
    """Test 14: fix_proposal_agent.py must not import or open baseline files."""
    module_path = REPO / "agents" / "fix_proposal_agent.py"
    source = module_path.read_text(encoding="utf-8")
    tree = ast.parse(source)
    # No import from baseline module
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            assert node.module != "baseline" and not (node.module or "").startswith("baseline."), \
                "fix_proposal_agent.py must not import from baseline module."
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert not alias.name.startswith("baseline"), \
                    "fix_proposal_agent.py must not import baseline module."
    # No string literals referencing baseline result files
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            val = node.value.lower()
            assert "results_baseline" not in val, \
                "fix_proposal_agent.py must not reference baseline result files."
            assert "baseline_summary" not in val, \
                "fix_proposal_agent.py must not reference baseline summary files."


# ---------------------------------------------------------------------------
# Test 15: Proposal status starts as PROPOSED
# ---------------------------------------------------------------------------

def test_proposal_status_starts_as_proposed():
    """Test 15: Generated proposal must have status=PROPOSED."""
    client = _mock_client(_good_proposal_from_llm())
    agent = FixProposalAgent(llm_client=client)
    result = agent.propose_fix(
        incident_dir=INC_01,
        hypotheses=_hypotheses(),
        verification_results=_verification(verdict="CONFIRMED"),
        logs_evidence=_logs(),
        code_evidence=_code(),
    )
    assert result["proposals"][0]["status"] == "PROPOSED"


# ---------------------------------------------------------------------------
# Test 16: Proposal cannot claim the fix was applied
# ---------------------------------------------------------------------------

def test_proposal_claiming_applied_flagged():
    """Test 16: A proposal claiming 'already applied' is flagged in validation_errors."""
    bad = _good_proposal_from_llm()
    bad["summary"] = "The fix has been applied to production."
    client = _mock_client(bad)
    agent = FixProposalAgent(llm_client=client)
    result = agent.propose_fix(
        incident_dir=INC_01,
        hypotheses=_hypotheses(),
        verification_results=_verification(verdict="CONFIRMED"),
        logs_evidence=_logs(),
        code_evidence=_code(),
    )
    assert "FIX-001" in result["validation_errors"]
    errors = result["validation_errors"]["FIX-001"]
    assert any("claim" in e.lower() for e in errors)


# ---------------------------------------------------------------------------
# Tests 17-18: Incident files and git remain unchanged
# ---------------------------------------------------------------------------

def test_incident_files_unchanged_after_propose():
    """Test 17: Incident source files must not be modified."""
    service_app = INC_01 / "service" / "app.py"
    before = service_app.read_text(encoding="utf-8")

    client = _mock_client(_good_proposal_from_llm())
    agent = FixProposalAgent(llm_client=client)
    agent.propose_fix(
        incident_dir=INC_01,
        hypotheses=_hypotheses(),
        verification_results=_verification(verdict="CONFIRMED"),
        logs_evidence=_logs(),
        code_evidence=_code(),
    )

    after = service_app.read_text(encoding="utf-8")
    assert before == after, "Source file was modified by FixProposalAgent!"


def test_no_git_changes_created(tmp_path: Path):
    """Test 18: No new files are created in the incident directory."""
    service_dir = INC_01 / "service"
    before_files = set(service_dir.rglob("*.py"))

    client = _mock_client(_good_proposal_from_llm())
    agent = FixProposalAgent(llm_client=client)
    agent.propose_fix(
        incident_dir=INC_01,
        hypotheses=_hypotheses(),
        verification_results=_verification(verdict="CONFIRMED"),
        logs_evidence=_logs(),
        code_evidence=_code(),
    )

    after_files = set(service_dir.rglob("*.py"))
    assert before_files == after_files, "New files appeared in incident directory!"


# ---------------------------------------------------------------------------
# Tests 19-20: No shell execution, no destructive ops
# ---------------------------------------------------------------------------

def test_no_shell_execution():
    """Test 19: Destructive patch content is flagged in validation_errors."""
    bad = _good_proposal_from_llm()
    bad["patch"] = "import subprocess; subprocess.run(['rm', '-rf', '/'])"
    client = _mock_client(bad)
    agent = FixProposalAgent(llm_client=client)
    result = agent.propose_fix(
        incident_dir=INC_01,
        hypotheses=_hypotheses(),
        verification_results=_verification(verdict="CONFIRMED"),
        logs_evidence=_logs(),
        code_evidence=_code(),
    )
    assert "FIX-001" in result["validation_errors"]
    errors = result["validation_errors"]["FIX-001"]
    assert any("destructive" in e.lower() for e in errors)


def test_no_destructive_operations():
    """Test 20: DROP TABLE in patch is rejected."""
    bad = _good_proposal_from_llm()
    bad["patch"] = "DROP TABLE users;"
    client = _mock_client(bad)
    agent = FixProposalAgent(llm_client=client)
    result = agent.propose_fix(
        incident_dir=INC_01,
        hypotheses=_hypotheses(),
        verification_results=_verification(verdict="CONFIRMED"),
        logs_evidence=_logs(),
        code_evidence=_code(),
    )
    assert "FIX-001" in result["validation_errors"]


# ---------------------------------------------------------------------------
# Test 21: No incident-specific hardcoded fix
# ---------------------------------------------------------------------------

def test_no_incident_specific_hardcoded_fix():
    """Test 21: fix_proposal_agent.py must not hardcode incident IDs."""
    module_path = REPO / "agents" / "fix_proposal_agent.py"
    source = module_path.read_text(encoding="utf-8")
    # None of the specific incident IDs should appear hardcoded outside strings
    for inc_id in ["inc_01", "inc_04", "inc_07", "inc_10"]:
        assert inc_id not in source, \
            f"fix_proposal_agent.py hardcodes incident ID {inc_id!r}."


# ---------------------------------------------------------------------------
# Test: human_approval_notice always present
# ---------------------------------------------------------------------------

def test_human_approval_notice_always_present():
    """Proposal always carries the mandatory human_approval_notice."""
    client = _mock_client(_good_proposal_from_llm())
    agent = FixProposalAgent(llm_client=client)
    result = agent.propose_fix(
        incident_dir=INC_01,
        hypotheses=_hypotheses(),
        verification_results=_verification(verdict="CONFIRMED"),
        logs_evidence=_logs(),
        code_evidence=_code(),
    )
    for proposal in result["proposals"]:
        assert proposal["human_approval_notice"] == HUMAN_APPROVAL_NOTICE


# ---------------------------------------------------------------------------
# Test: multiple confirmed hypotheses produce multiple proposals
# ---------------------------------------------------------------------------

def test_multiple_confirmed_hypotheses_produce_multiple_proposals():
    """Two confirmed hypotheses → two proposals, two Groq calls."""
    hyps = {
        "incident_id": "inc_01_n_plus_one_query",
        "hypotheses": [
            {"hypothesis_id": "HYP-001", "claim": "N+1 query",
             "evidence_ids": ["EV-LOG-001", "EV-CODE-001"],
             "supporting_reasoning": "250 queries", "falsification_criteria": ["x"],
             "verification_plan": ["check"]},
            {"hypothesis_id": "HYP-002", "claim": "Pool too small",
             "evidence_ids": ["EV-LOG-002"],
             "supporting_reasoning": "pool=20", "falsification_criteria": ["y"],
             "verification_plan": ["check"]},
        ],
    }
    ver = {
        "incident_id": "inc_01_n_plus_one_query",
        "results": [
            {"hypothesis_id": "HYP-001", "verdict": "CONFIRMED", "checks": [], "reasoning": "", "confidence": 0.9},
            {"hypothesis_id": "HYP-002", "verdict": "CONFIRMED", "checks": [], "reasoning": "", "confidence": 0.8},
        ],
    }

    call_count = [0]
    def fake_generate(*args, **kwargs) -> LLMResponse:
        call_count[0] += 1
        pid = f"FIX-{call_count[0]:03d}"
        hid = f"HYP-{call_count[0]:03d}"
        return _mock_llm_response(_good_proposal_from_llm(proposal_id=pid, hypothesis_id=hid))

    client = MagicMock(spec=GroqLLMClient)
    client.generate_structured.side_effect = fake_generate
    agent = FixProposalAgent(llm_client=client)
    result = agent.propose_fix(
        incident_dir=INC_01,
        hypotheses=hyps,
        verification_results=ver,
        logs_evidence=_logs(),
        code_evidence=_code(),
    )
    assert len(result["proposals"]) == 2
    assert client.generate_structured.call_count == 2
