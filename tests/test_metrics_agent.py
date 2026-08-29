"""Unit tests for Sentinel Metrics Agent.

All LLM responses are mocked. Tests never call the Groq API.
"""

import ast
import json
from pathlib import Path
from unittest.mock import MagicMock

import jsonschema
import pytest

from agents.metrics_agent import MetricsAgent
from core.llm import GroqLLMClient, LLMJSONParseError, LLMResponse


INCIDENTS_DIR = Path(__file__).parent.parent / "incidents"
SAMPLE_INCIDENT_DIR = INCIDENTS_DIR / "inc_01_n_plus_one_query"
AGENT_FILE = Path(__file__).parent.parent / "agents" / "metrics_agent.py"
TOOLS_FILE = Path(__file__).parent.parent / "agents" / "metric_tools.py"


@pytest.fixture
def mock_llm_client():
    return MagicMock(spec=GroqLLMClient)


def _mock_structured(client: MagicMock, payload: dict) -> None:
    client.generate_structured.return_value = LLMResponse(
        content=json.dumps(payload),
        parsed_json=payload,
    )


def test_no_direct_groq_sdk_import_in_metrics_agent():
    content = AGENT_FILE.read_text(encoding="utf-8")
    assert "import groq" not in content
    assert "from groq import" not in content
    assert "core.llm" in content


def test_metric_tools_have_no_llm_calls():
    source = TOOLS_FILE.read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.extend(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.append(node.module.split(".")[0])
    assert "groq" not in imported
    assert "core" not in imported
    assert "generate_structured" not in source
    assert "get_llm_client" not in source
    assert "GroqLLMClient" not in source


def test_metrics_agent_uses_core_llm():
    content = AGENT_FILE.read_text(encoding="utf-8")
    assert "from core.llm import" in content
    assert "generate_structured" in content


def test_ground_truth_is_never_read(mock_llm_client, monkeypatch):
    agent = MetricsAgent(llm_client=mock_llm_client)
    _mock_structured(
        mock_llm_client,
        {
            "incident_id": "inc_01_n_plus_one_query",
            "agent": "metrics_agent",
            "summary": "Latency and connection metrics rose.",
            "evidence": [],
        },
    )
    read_names: list[str] = []
    original = Path.read_text

    def spy(self, *args, **kwargs):
        read_names.append(self.name)
        return original(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", spy)
    result = agent.extract_evidence(SAMPLE_INCIDENT_DIR)
    assert "ground_truth.md" not in read_names
    prompt = mock_llm_client.generate_structured.call_args.kwargs.get("prompt")
    if prompt is None:
        prompt = mock_llm_client.generate_structured.call_args.args[0]
    assert "ground_truth.md" not in prompt
    assert "Underlying Root Cause" not in prompt
    assert result["incident_id"] == "inc_01_n_plus_one_query"


def test_exactly_one_llm_summarisation_call(mock_llm_client):
    agent = MetricsAgent(llm_client=mock_llm_client)
    _mock_structured(
        mock_llm_client,
        {
            "incident_id": "inc_01_n_plus_one_query",
            "agent": "metrics_agent",
            "summary": "ok",
            "evidence": [],
        },
    )
    agent.extract_evidence(SAMPLE_INCIDENT_DIR)
    assert mock_llm_client.generate_structured.call_count == 1
    assert mock_llm_client.generate.call_count == 0


def test_mocked_groq_response_is_parsed(mock_llm_client):
    from agents.metric_tools import collect_candidate_evidence, load_metrics

    table = load_metrics(SAMPLE_INCIDENT_DIR / "metrics" / "metrics.csv")
    candidates = collect_candidate_evidence(table)
    sample = next(item for item in candidates if item["type"] != "correlation")
    agent = MetricsAgent(llm_client=mock_llm_client)
    _mock_structured(
        mock_llm_client,
        {
            "incident_id": "wrong-id",
            "agent": "metrics_agent",
            "summary": "Latency increased versus the early window.",
            "evidence": [
                {
                    "evidence_id": "EV-MET-009",
                    "source": "metrics",
                    "reference": sample["reference"],
                    "timestamp": sample["timestamp"],
                    "metric": sample["metric"],
                    "value": sample["value"],
                    "type": sample["type"],
                    "interpretation": "Observed elevated sample versus baseline.",
                }
            ],
        },
    )
    result = agent.extract_evidence(SAMPLE_INCIDENT_DIR)
    jsonschema.validate(instance=result, schema=agent.schema)
    assert result["incident_id"] == "inc_01_n_plus_one_query"
    assert result["agent"] == "metrics_agent"
    assert result["evidence"][0]["evidence_id"] == "EV-MET-001"
    assert result["evidence"][0]["value"] == sample["value"]
    assert result["evidence"][0]["interpretation"] == "Observed elevated sample versus baseline."


def test_malformed_llm_response_is_handled(mock_llm_client):
    agent = MetricsAgent(llm_client=mock_llm_client)
    mock_llm_client.generate_structured.side_effect = LLMJSONParseError("not json")
    result = agent.extract_evidence(SAMPLE_INCIDENT_DIR)
    assert result["incident_id"] == "inc_01_n_plus_one_query"
    assert result["evidence"]
    ids = [item["evidence_id"] for item in result["evidence"]]
    assert ids == [f"EV-MET-{i:03d}" for i in range(1, len(ids) + 1)]
    jsonschema.validate(instance=result, schema=agent.schema)


def test_empty_metrics_are_handled(mock_llm_client, tmp_path: Path):
    empty_dir = tmp_path / "inc_empty_metrics"
    (empty_dir / "metrics").mkdir(parents=True)
    (empty_dir / "metrics" / "metrics.csv").write_text("timestamp,latency_ms\n", encoding="utf-8")
    agent = MetricsAgent(llm_client=mock_llm_client)
    result = agent.extract_evidence(empty_dir)
    assert result["incident_id"] == "inc_empty_metrics"
    assert result["evidence"] == []
    assert mock_llm_client.generate_structured.call_count == 0


def test_missing_metrics_csv_is_handled(mock_llm_client, tmp_path: Path):
    missing_dir = tmp_path / "inc_missing_metrics"
    missing_dir.mkdir()
    agent = MetricsAgent(llm_client=mock_llm_client)
    result = agent.extract_evidence(missing_dir)
    assert result["incident_id"] == "inc_missing_metrics"
    assert result["evidence"] == []
    assert "No metrics file" in result["summary"]
    assert mock_llm_client.generate_structured.call_count == 0


def test_evidence_ids_unique_and_references_real(mock_llm_client):
    agent = MetricsAgent(llm_client=mock_llm_client)
    mock_llm_client.generate_structured.side_effect = LLMJSONParseError("fallback")
    result = agent.extract_evidence(SAMPLE_INCIDENT_DIR)
    ids = [item["evidence_id"] for item in result["evidence"]]
    assert len(ids) == len(set(ids))
    csv_path = SAMPLE_INCIDENT_DIR / "metrics" / "metrics.csv"
    lines = csv_path.read_text(encoding="utf-8").splitlines()
    header = lines[0].split(",")
    for item in result["evidence"]:
        if item["type"] == "correlation":
            assert item["reference"] == "metrics/metrics.csv"
            continue
        row_no = int(item["reference"].rsplit("row ", 1)[1])
        cells = lines[row_no - 1].split(",")
        assert float(cells[header.index(item["metric"])]) == item["value"]


def test_incident_files_are_never_modified(mock_llm_client):
    agent = MetricsAgent(llm_client=mock_llm_client)
    _mock_structured(
        mock_llm_client,
        {
            "incident_id": "inc_01_n_plus_one_query",
            "agent": "metrics_agent",
            "summary": "ok",
            "evidence": [],
        },
    )
    snapshot = {
        path: path.read_bytes()
        for path in SAMPLE_INCIDENT_DIR.rglob("*")
        if path.is_file()
    }
    agent.extract_evidence(SAMPLE_INCIDENT_DIR)
    after = {
        path: path.read_bytes()
        for path in SAMPLE_INCIDENT_DIR.rglob("*")
        if path.is_file()
    }
    assert snapshot == after
