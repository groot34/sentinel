from __future__ import annotations

import ast
import json
from pathlib import Path
from unittest.mock import MagicMock

import jsonschema
import pytest

from agents.hypothesis_engine import HypothesisEngine, _validate_output, load_hypothesis_schema
from core.llm import GroqLLMClient, LLMJSONParseError, LLMResponse

REPO = Path(__file__).parent.parent
SCHEMA = load_hypothesis_schema()

SAMPLE_LOGS = {
    "incident_id": "inc_demo",
    "agent": "logs_agent",
    "evidence": [
        {"evidence_id": "EV-LOG-001", "source": "logs", "reference": "logs/application.log:1",
         "type": "error", "excerpt": "ERROR pool exhausted"},
        {"evidence_id": "EV-LOG-002", "source": "logs", "reference": "logs/application.log:2",
         "type": "warning", "excerpt": "WARN high latency"},
    ],
}

SAMPLE_METRICS = {
    "incident_id": "inc_demo",
    "agent": "metrics_agent",
    "evidence": [
        {"evidence_id": "EV-MET-001", "source": "metrics", "reference": "metrics/metrics.csv:row 2",
         "metric": "db_conns", "value": 100.0, "type": "spike", "excerpt": "active_db_conns 100"},
    ],
}

SAMPLE_CODE = {
    "incident_id": "inc_demo",
    "agent": "code_agent",
    "evidence": [
        {"evidence_id": "EV-CODE-001", "source": "code", "reference": "service/app.py:10",
         "type": "suspicious_pattern", "excerpt": "for item in items:\n    db.query(item.id)"},
    ],
}


def _mock_client(parsed: dict | None = None, side_effect=None) -> MagicMock:
    m = MagicMock(spec=GroqLLMClient)
    if side_effect is not None:
        m.generate_structured.side_effect = side_effect
    else:
        resp = MagicMock(spec=LLMResponse)
        resp.get_structured.return_value = parsed or {"incident_id": "x", "hypotheses": []}
        m.generate_structured.return_value = resp
    return m


def test_valid_hypothesis_generation():
    parsed = {
        "incident_id": "inc_demo",
        "hypotheses": [
            {
                "hypothesis_id": "HYP-001",
                "claim": "DB query in loop amplified connection usage.",
                "evidence_ids": ["EV-LOG-001", "EV-CODE-001", "EV-MET-001"],
                "supporting_reasoning": "Log pool exhaustion plus code loop match.",
                "falsification_criteria": ["Query count does not grow with list length."],
                "verification_plan": ["AST-check for DB call inside for loop."],
            }
        ],
    }
    eng = HypothesisEngine(llm_client=_mock_client(parsed))
    result = eng.generate_hypotheses("inc_demo", SAMPLE_LOGS, SAMPLE_METRICS, SAMPLE_CODE)
    assert result["hypotheses"][0]["hypothesis_id"] == "HYP-001"
    jsonschema.validate(instance=result, schema=SCHEMA)


def test_hypothesis_count_enforced_1_to_4():
    parsed = {
        "incident_id": "inc_demo",
        "hypotheses": [
            {
                "hypothesis_id": f"HYP-{i:03d}",
                "claim": f"Claim {i}.",
                "evidence_ids": ["EV-CODE-001"],
                "supporting_reasoning": "r.",
                "falsification_criteria": ["f."],
                "verification_plan": ["v."],
            }
            for i in range(1, 8)
        ],
    }
    eng = HypothesisEngine(llm_client=_mock_client(parsed))
    result = eng.generate_hypotheses("inc_demo", code_evidence=SAMPLE_CODE)
    assert 1 <= len(result["hypotheses"]) <= 4


def test_unique_hypothesis_ids():
    parsed = {
        "incident_id": "inc_demo",
        "hypotheses": [
            {"hypothesis_id": "HYP-XXX", "claim": "c1", "evidence_ids": ["EV-CODE-001"],
             "supporting_reasoning": "r", "falsification_criteria": ["f"], "verification_plan": ["v"]},
            {"hypothesis_id": "HYP-XXX", "claim": "c2", "evidence_ids": ["EV-LOG-001"],
             "supporting_reasoning": "r", "falsification_criteria": ["f"], "verification_plan": ["v"]},
        ],
    }
    eng = HypothesisEngine(llm_client=_mock_client(parsed))
    result = eng.generate_hypotheses("inc_demo", SAMPLE_LOGS, code_evidence=SAMPLE_CODE)
    ids = [h["hypothesis_id"] for h in result["hypotheses"]]
    assert len(ids) == len(set(ids))
    assert ids == ["HYP-001", "HYP-002"]


def test_evidence_ids_must_exist():
    parsed = {
        "incident_id": "inc_demo",
        "hypotheses": [
            {
                "hypothesis_id": "HYP-001",
                "claim": "Db query loop.",
                "evidence_ids": ["EV-CODE-001"],
                "supporting_reasoning": "r",
                "falsification_criteria": ["f"],
                "verification_plan": ["v"],
            }
        ],
    }
    eng = HypothesisEngine(llm_client=_mock_client(parsed))
    res = eng.generate_hypotheses("inc_demo", code_evidence=SAMPLE_CODE)
    assert res["hypotheses"][0]["evidence_ids"] == ["EV-CODE-001"]


def test_unknown_evidence_ids_rejected():
    parsed = {
        "incident_id": "inc_demo",
        "hypotheses": [
            {
                "hypothesis_id": "HYP-001",
                "claim": "c",
                "evidence_ids": ["EV-LOG-999", "EV-CODE-001", "EV-INVENTED-017"],
                "supporting_reasoning": "r",
                "falsification_criteria": ["f"],
                "verification_plan": ["v"],
            }
        ],
    }
    eng = HypothesisEngine(llm_client=_mock_client(parsed))
    res = eng.generate_hypotheses("inc_demo", SAMPLE_LOGS, code_evidence=SAMPLE_CODE)
    allowed = {"EV-LOG-001", "EV-LOG-002", "EV-MET-001", "EV-CODE-001"}
    for h in res["hypotheses"]:
        for eid in h["evidence_ids"]:
            assert eid in allowed


def test_ground_truth_isolated(monkeypatch):
    spy_calls = []
    orig_read_text = Path.read_text
    def _spy(self: Path, *args, **kwargs):
        name = str(self).replace("\\", "/")
        spy_calls.append(name)
        if "ground_truth.md" in name.lower():
            raise AssertionError(f"ground_truth.md accessed: {name}")
        return orig_read_text(self, *args, **kwargs)
    monkeypatch.setattr(Path, "read_text", _spy)
    eng = HypothesisEngine(llm_client=_mock_client({
        "incident_id": "inc_demo",
        "hypotheses": [{
            "hypothesis_id": "HYP-001",
            "claim": "c",
            "evidence_ids": ["EV-CODE-001"],
            "supporting_reasoning": "r",
            "falsification_criteria": ["f"],
            "verification_plan": ["v"],
        }],
    }))
    res = eng.generate_hypotheses("inc_demo", code_evidence=SAMPLE_CODE)
    assert res["hypotheses"][0]["hypothesis_id"] == "HYP-001"
    joined = "|".join(spy_calls).lower()
    assert "ground_truth" not in joined


def test_baseline_isolated(monkeypatch):
    eng = HypothesisEngine(llm_client=_mock_client({
        "incident_id": "inc_demo",
        "hypotheses": [{
            "hypothesis_id": "HYP-001",
            "claim": "c",
            "evidence_ids": ["EV-CODE-001"],
            "supporting_reasoning": "r",
            "falsification_criteria": ["f"],
            "verification_plan": ["v"],
        }],
    }))
    res = eng.generate_hypotheses("inc_demo", code_evidence=SAMPLE_CODE)
    import json as _json
    blob = _json.dumps(res)
    eng_src = (REPO / "agents" / "hypothesis_engine.py").read_text(encoding="utf-8")
    for bad in ("results_baseline.csv", "baseline_summary.json", "baseline/results"):
        assert bad not in eng_src
        assert bad not in blob


def test_no_direct_groq_import():
    src = (REPO / "agents" / "hypothesis_engine.py").read_text(encoding="utf-8")
    assert "import groq" not in src
    assert "from groq" not in src


def test_uses_core_llm():
    src = (REPO / "agents" / "hypothesis_engine.py").read_text(encoding="utf-8")
    assert "from core.llm import" in src
    assert "generate_structured" in src


def test_exactly_one_llm_call():
    eng = HypothesisEngine(llm_client=_mock_client({
        "incident_id": "inc_demo",
        "hypotheses": [{
            "hypothesis_id": "HYP-001",
            "claim": "c",
            "evidence_ids": ["EV-CODE-001"],
            "supporting_reasoning": "r",
            "falsification_criteria": ["f"],
            "verification_plan": ["v"],
        }],
    }))
    eng.generate_hypotheses("inc_demo", code_evidence=SAMPLE_CODE)
    assert eng.llm_client.generate_structured.call_count == 1
    assert eng.llm_client.generate.call_count == 0


def test_malformed_llm_response_handling():
    def raise_parse(*a, **k):
        raise LLMJSONParseError("oops")
    eng = HypothesisEngine(llm_client=_mock_client(side_effect=raise_parse))
    with pytest.raises(LLMJSONParseError):
        eng.generate_hypotheses("inc_demo", code_evidence=SAMPLE_CODE)


def test_missing_evidence_handling():
    eng = HypothesisEngine(llm_client=_mock_client())
    with pytest.raises(ValueError):
        eng.generate_hypotheses("inc_demo", logs_evidence={"evidence": []})


def test_empty_evidence_handling():
    eng = HypothesisEngine(llm_client=_mock_client())
    with pytest.raises(ValueError):
        eng.generate_hypotheses("inc_demo", logs_evidence=None, metrics_evidence=None, code_evidence=None)


def test_no_incident_specific_hardcoded_answers():
    src = (REPO / "agents" / "hypothesis_engine.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    incident_ids = [
        "inc_01_n_plus_one_query", "inc_02_short_cache_ttl", "inc_03_downstream_service_latency",
        "inc_04_unbounded_collection_memory_leak", "inc_05_blocking_op_on_event_loop",
        "inc_06_connection_pool_exhaustion", "inc_07_retry_storm",
        "inc_08_slow_external_timeout", "inc_09_missing_index",
        "inc_10_multi_symptom_cascade",
    ]
    for node in ast.walk(tree):
        if isinstance(node, (ast.If, ast.cmpop)):
            try:
                text = ast.unparse(node)
            except Exception:
                text = ""
            for iid in incident_ids:
                assert iid not in text, f"Hardcoded incident id found in: {text}"


def test_schema_validation():
    valid = {
        "incident_id": "inc_demo",
        "hypotheses": [
            {
                "hypothesis_id": "HYP-001",
                "claim": "Claim.",
                "evidence_ids": ["EV-CODE-001"],
                "supporting_reasoning": "Reason.",
                "falsification_criteria": ["fc."],
                "verification_plan": ["vp."],
            }
        ],
    }
    jsonschema.validate(instance=valid, schema=SCHEMA)
    invalid = dict(valid)
    invalid["hypotheses"][0]["hypothesis_id"] = "NOPE"
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(instance=invalid, schema=SCHEMA)


def test_validate_output_rejects_all_unknown_evidence_ids():
    bad = {
        "incident_id": "x",
        "hypotheses": [
            {"hypothesis_id": "HYP-001", "claim": "c",
             "evidence_ids": ["EV-LOG-999"], "supporting_reasoning": "r",
             "falsification_criteria": ["f"], "verification_plan": ["v"]}
        ],
    }
    with pytest.raises(LLMJSONParseError):
        _validate_output(bad, SCHEMA, {"EV-CODE-001"}, "x")
