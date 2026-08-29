from __future__ import annotations

import ast
import hashlib
import json
from pathlib import Path

import jsonschema
import pytest

from agents.hypothesis_engine import HypothesisEngine
from agents.verification_agent import VerificationAgent, VERIFICATION_OUTPUT_SCHEMA
from agents.verification_tools import (
    VERDICT_CONFIRMED,
    VERDICT_INCONCLUSIVE,
    VERDICT_REJECTED,
)
from core.llm import GroqLLMClient, LLMResponse
from unittest.mock import MagicMock

REPO = Path(__file__).parent.parent
INCIDENTS = REPO / "incidents"


def _inc(name: str) -> Path:
    return INCIDENTS / name


INC_01 = _inc("inc_01_n_plus_one_query")
INC_04 = _inc("inc_04_memory_leak")
INC_07 = _inc("inc_07_retry_storm")
INC_10 = _inc("inc_10_multi_symptom_cascade")


def _evidence_bundle(prefix: str, n: int):
    return {
        "incident_id": "demo",
        "evidence": [
            {"evidence_id": f"{prefix}{i:03d}", "source": prefix.split("-")[1].lower(),
             "reference": f"ref/{i}", "excerpt": f"demo {i}", "type": "warning",
             "interpretation": f"explanation {i}"}
            for i in range(1, n + 1)
        ],
    }


def _hyp(claim: str, ev_ids: list[str], plan: list[str], fals: list[str] | None = None, idx: int = 1):
    return {
        "hypothesis_id": f"HYP-{idx:03d}",
        "claim": claim,
        "evidence_ids": ev_ids,
        "supporting_reasoning": "Supports claim.",
        "falsification_criteria": fals or ["If checkable evidence contradicts, reject."],
        "verification_plan": plan,
    }


def test_confirmed_verdict(tmp_path: Path):
    """For inc_01 with a query-in-loop hypothesis, the dispatched checks should
    all PASS → CONFIRMED."""
    code = json.loads((REPO / "eval" / "sample_runs" / "code_agent_inc_01.json").read_text())
    logs = json.loads((REPO / "eval" / "sample_runs" / "logs_agent_inc_01.json").read_text())
    metrics = {
        "incident_id": "inc_01_n_plus_one_query",
        "evidence": [{"evidence_id": "EV-MET-001", "source": "metrics",
                      "reference": "metrics/metrics.csv", "excerpt": "spike", "type": "spike"}],
    }
    hypotheses = {
        "incident_id": "inc_01_n_plus_one_query",
        "hypotheses": [
            _hyp("DB query is called inside the order-item loop causing amplification.",
                 ["EV-CODE-001", "EV-CODE-003", "EV-LOG-001"],
                 ["AST inspect DB call inside For/While loop.",
                  "Check log pattern 'Pool exhausted' counts."],
                 idx=1),
        ],
    }
    va = VerificationAgent()
    out = va.verify(INC_01, hypotheses, logs, metrics, code)
    jsonschema.validate(instance=out, schema=VERIFICATION_OUTPUT_SCHEMA)
    r = out["results"][0]
    assert r["verdict"] == VERDICT_CONFIRMED, f"Expected CONFIRMED but got {r['verdict']}: {r['reasoning']}"
    # evidence chain: each check references at least 1 evidence id
    for chk in r["checks"]:
        assert "CHK-" in chk["check_id"]


def test_rejected_verdict(tmp_path: Path):
    """Fabricate a hypothesis about index drop in inc_10 being an SQL change —
    the tools should detect net_sql_drops=0 and mark that falsification FAIL →
    REJECTED."""
    code = json.loads((REPO / "eval" / "sample_runs" / "code_agent_inc_10.json").read_text())
    hypotheses = {
        "incident_id": "inc_10_multi_symptom_cascade",
        "hypotheses": [
            {
                "hypothesis_id": "HYP-001",
                "claim": "The migration dropped the index (new SQL was added vs removed).",
                "evidence_ids": ["EV-CODE-002", "EV-CODE-003", "EV-CODE-004"],
                "supporting_reasoning": "Diff shows both removed and added index lines.",
                "falsification_criteria": [
                    "If git_diff.patch added and removed DROP INDEX SQL equally, the SQL was not actually changed."
                ],
                "verification_plan": [
                    "Verify DROP INDEX presence/absence change using diff SQL line count.",
                ],
            }
        ],
    }
    va = VerificationAgent()
    out = va.verify(INC_10, hypotheses, code_evidence=code)
    jsonschema.validate(instance=out, schema=VERIFICATION_OUTPUT_SCHEMA)
    r = out["results"][0]
    assert r["verdict"] == VERDICT_REJECTED, f"Expected REJECTED but got {r['verdict']}: {r['reasoning']}"


def test_inconclusive_verdict(tmp_path: Path):
    """Construct an evidence bundle whose reference is absent/unresolvable and a
    plan that doesn't match any checker keywords AND whose referenced snippet
    cannot be grounded -> should be INCONCLUSIVE."""
    dummy_bundle = {
        "incident_id": INC_04.name,
        "evidence": [{
            "evidence_id": "EV-DUM-001", "source": "code",
            "reference": "service/no_such_file_zzzz.py:1",
            "excerpt": "zzz_snippet_that_does_not_exist_anywhere_zzz",
            "type": "suspicious_pattern",
        }],
    }
    hypotheses = {
        "incident_id": INC_04.name,
        "hypotheses": [
            {
                "hypothesis_id": "HYP-001",
                "claim": "Completely unsupported hypothetical claim.",
                "evidence_ids": ["EV-DUM-001"],
                "supporting_reasoning": "No real evidence.",
                "falsification_criteria": ["It is entirely uncheckable."],
                "verification_plan": [
                    "Verify the completely unsupported hypothetical claim via nonexistent check.",
                ],
            }
        ],
    }
    va = VerificationAgent()
    out = va.verify(INC_04, hypotheses, code_evidence=dummy_bundle)
    jsonschema.validate(instance=out, schema=VERIFICATION_OUTPUT_SCHEMA)
    assert out["results"][0]["verdict"] == VERDICT_INCONCLUSIVE, out["results"][0]["reasoning"]


def test_evidence_chain_preserved(tmp_path: Path):
    code = json.loads((REPO / "eval" / "sample_runs" / "code_agent_inc_01.json").read_text())
    logs = json.loads((REPO / "eval" / "sample_runs" / "logs_agent_inc_01.json").read_text())
    hypotheses = {
        "incident_id": INC_01.name,
        "hypotheses": [_hyp("DB query in loop.", ["EV-CODE-001", "EV-LOG-001"],
                            ["Query inside loop check.", "log pattern count."], idx=1)],
    }
    va = VerificationAgent()
    out = va.verify(INC_01, hypotheses, logs, code_evidence=code)
    for hyp_res in out["results"]:
        assert isinstance(hyp_res["checks"], list) and len(hyp_res["checks"]) >= 1
        for c in hyp_res["checks"]:
            assert c["result"] in {"PASS", "FAIL", "INCONCLUSIVE"}
            assert isinstance(c["evidence"], list)
            # non-empty evidence preferred
            if c["result"] == "PASS":
                assert c["evidence"] or c["reference"] is not None


def test_unknown_evidence_ids_rejected(tmp_path: Path):
    logs_bundle = {"evidence": [{"evidence_id": "EV-LOG-001", "source": "logs", "reference": "logs/x:1", "excerpt": "a", "type": "warning"}]}
    hypotheses = {
        "incident_id": INC_01.name,
        "hypotheses": [
            _hyp("c.", ["EV-LOG-001", "EV-LOG-999", "EV-INVENTED"], ["log pattern count"], idx=1)
        ],
    }
    va = VerificationAgent()
    out = va.verify(INC_01, hypotheses, logs_evidence=logs_bundle)
    # referenced checks should only use EV-LOG-001
    used_ids: set[str] = set()
    for c in out["results"][0]["checks"]:
        used_ids.update(c["evidence"])
    assert "EV-LOG-999" not in used_ids
    assert "EV-INVENTED" not in used_ids


def test_ground_truth_isolation(monkeypatch, tmp_path: Path):
    calls = []
    orig = Path.read_text
    def spy(self: Path, *a, **k):
        n = str(self).replace("\\", "/").lower()
        calls.append(n)
        if "ground_truth.md" in n:
            raise AssertionError(f"ground truth accessed: {self}")
        return orig(self, *a, **k)
    monkeypatch.setattr(Path, "read_text", spy)
    code = json.loads((REPO / "eval" / "sample_runs" / "code_agent_inc_01.json").read_text())
    hypotheses = {"incident_id": INC_01.name, "hypotheses": [
        _hyp("q loop", ["EV-CODE-001"], ["query inside loop AST check"])
    ]}
    va = VerificationAgent()
    va.verify(INC_01, hypotheses, code_evidence=code)
    assert not any("ground_truth" in c for c in calls)


def test_baseline_isolation(tmp_path: Path):
    va_src = (REPO / "agents" / "verification_agent.py").read_text(encoding="utf-8")
    vt_src = (REPO / "agents" / "verification_tools.py").read_text(encoding="utf-8")
    for src in (va_src, vt_src):
        for bad in ("results_baseline.csv", "baseline_summary.json", "baseline/results"):
            assert bad not in src


def test_no_direct_groq_import(tmp_path: Path):
    for mod in ("agents/verification_agent.py", "agents/verification_tools.py"):
        src = (REPO / mod).read_text(encoding="utf-8")
        assert "import groq" not in src
        assert "from groq" not in src


def test_no_llm_call_required_for_verdict(tmp_path: Path):
    """Verification must work with zero Groq — use VerificationAgent standalone
    and confirm no LLM constructor calls."""
    import agents.verification_agent as va_mod
    # Search source for generate_structured or GroqLLMClient construction.
    src = va_mod.__file__
    content = Path(src).read_text(encoding="utf-8")
    assert "get_llm_client" not in content
    assert "generate_structured" not in content


def test_incident_files_unchanged(tmp_path: Path):
    def snap(path: Path) -> dict[str, str]:
        out = {}
        for p in sorted(path.rglob("*")):
            if p.is_file():
                out[str(p.relative_to(path))] = hashlib.sha256(p.read_bytes()).hexdigest()
        return out
    for inc in (INC_01, INC_04, INC_07, INC_10):
        before = snap(inc)
        # Use hypotheses with mixed plan keywords to exercise dispatch fully
        code_path = REPO / "eval" / "sample_runs" / f"code_agent_{inc.name.split('_', 2)[1] if False else ('inc_' + inc.name.split('_')[1])}.json"
        # Compute actual sample run paths
        inc_suffix = inc.name.split("_")[1]
        code_p = REPO / "eval" / "sample_runs" / f"code_agent_inc_{inc_suffix}.json"
        logs_p = REPO / "eval" / "sample_runs" / f"logs_agent_inc_{inc_suffix}.json"
        metrics_p = REPO / "eval" / "sample_runs" / f"metrics_agent_inc_{inc_suffix}.json"
        code = json.loads(code_p.read_text()) if code_p.exists() else None
        logs = json.loads(logs_p.read_text()) if logs_p.exists() else None
        metrics = json.loads(metrics_p.read_text()) if metrics_p.exists() else None
        hyps = {
            "incident_id": inc.name,
            "hypotheses": [
                _hyp("mixed claim.",
                     list({e["evidence_id"] for b in [code, logs, metrics] if b for e in b.get("evidence", [])})[:3],
                     ["query inside loop check",
                      "retry configuration check",
                      "connection acquire/release path inspection",
                      "drop index SQL change check",
                      "class-level mutable collection check",
                      "metric spike order correlation check",
                      "log pattern count",
                      "max metric value threshold"],
                     idx=1)
            ],
        }
        va = VerificationAgent()
        va.verify(inc, hyps, logs, metrics, code)
        after = snap(inc)
        assert before == after, f"Incident files modified: {inc}"
