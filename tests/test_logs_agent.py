"""Unit tests for Sentinel Logs Agent.

All LLM responses are mocked. Tests never call the Groq API.
"""

import ast
import json
from pathlib import Path
from unittest.mock import MagicMock

import jsonschema
import pytest

from agents.logs_agent import LogsAgent
from core.llm import GroqLLMClient, LLMJSONParseError, LLMResponse


INCIDENTS_DIR = Path(__file__).parent.parent / "incidents"
SAMPLE_INCIDENT_DIR = INCIDENTS_DIR / "inc_01_n_plus_one_query"
LOGS_AGENT_FILE = Path(__file__).parent.parent / "agents" / "logs_agent.py"
LOG_TOOLS_FILE = Path(__file__).parent.parent / "agents" / "log_tools.py"


@pytest.fixture
def mock_llm_client():
    return MagicMock(spec=GroqLLMClient)


def _mock_structured(client: MagicMock, payload: dict) -> None:
    client.generate_structured.return_value = LLMResponse(
        content=json.dumps(payload),
        parsed_json=payload,
    )


def test_no_direct_groq_sdk_import_in_logs_agent():
    content = LOGS_AGENT_FILE.read_text(encoding="utf-8")
    assert "import groq" not in content
    assert "from groq import" not in content
    assert "core.llm" in content


def test_log_tools_have_no_llm_calls():
    source = LOG_TOOLS_FILE.read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.extend(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.append(node.module.split(".")[0])
    assert "groq" not in imported
    assert "core" not in imported
    assert "llm" not in source.lower() or "llm" not in imported
    assert "generate_structured" not in source
    assert "get_llm_client" not in source


def test_logs_agent_uses_core_llm():
    content = LOGS_AGENT_FILE.read_text(encoding="utf-8")
    assert "from core.llm import" in content
    assert "generate_structured" in content


def test_ground_truth_is_never_read(mock_llm_client, monkeypatch):
    agent = LogsAgent(llm_client=mock_llm_client)
    _mock_structured(
        mock_llm_client,
        {
            "incident_id": "inc_01_n_plus_one_query",
            "agent": "logs_agent",
            "summary": "Errors and pool exhaustion appear in logs.",
            "evidence": [],
        },
    )

    read_paths: list[str] = []
    original_read_text = Path.read_text

    def spy_read_text(self, *args, **kwargs):
        read_paths.append(self.name)
        return original_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", spy_read_text)
    result = agent.extract_evidence(SAMPLE_INCIDENT_DIR)

    assert "ground_truth.md" not in read_paths
    prompt = mock_llm_client.generate_structured.call_args.kwargs.get("prompt")
    if prompt is None:
        prompt = mock_llm_client.generate_structured.call_args.args[0]
    assert "ground_truth.md" not in prompt
    assert "Underlying Root Cause" not in prompt
    assert result["incident_id"] == "inc_01_n_plus_one_query"


def test_exactly_one_llm_summarisation_call(mock_llm_client):
    agent = LogsAgent(llm_client=mock_llm_client)
    log_lines = (SAMPLE_INCIDENT_DIR / "logs" / "application.log").read_text(encoding="utf-8").splitlines()
    first_error = next(i for i, line in enumerate(log_lines, start=1) if "ERROR" in line)
    excerpt = log_lines[first_error - 1]
    _mock_structured(
        mock_llm_client,
        {
            "incident_id": "inc_01_n_plus_one_query",
            "agent": "logs_agent",
            "summary": "Timeout and pool errors are present.",
            "evidence": [
                {
                    "evidence_id": "EV-LOG-001",
                    "source": "logs",
                    "reference": f"logs/application.log:{first_error}",
                    "timestamp": excerpt.split(" ")[0],
                    "type": "error",
                    "excerpt": excerpt,
                    "interpretation": "Request timed out while querying orders.",
                }
            ],
        },
    )

    agent.extract_evidence(SAMPLE_INCIDENT_DIR)
    assert mock_llm_client.generate_structured.call_count == 1
    assert mock_llm_client.generate.call_count == 0


def test_mocked_groq_response_is_parsed(mock_llm_client):
    agent = LogsAgent(llm_client=mock_llm_client)
    log_lines = (SAMPLE_INCIDENT_DIR / "logs" / "application.log").read_text(encoding="utf-8").splitlines()
    error_line_no = next(i for i, line in enumerate(log_lines, start=1) if "ERROR" in line)
    excerpt = log_lines[error_line_no - 1]
    _mock_structured(
        mock_llm_client,
        {
            "incident_id": "wrong-id-from-model",
            "agent": "logs_agent",
            "summary": "Pool errors observed after bulk queries.",
            "evidence": [
                {
                    "evidence_id": "EV-LOG-009",
                    "source": "logs",
                    "reference": f"logs/application.log:{error_line_no}",
                    "timestamp": excerpt.split(" ")[0],
                    "type": "error",
                    "excerpt": excerpt,
                    "interpretation": "Gateway timeout on bulk order fetch.",
                }
            ],
        },
    )

    result = agent.extract_evidence(SAMPLE_INCIDENT_DIR)
    jsonschema.validate(instance=result, schema=agent.schema)
    assert result["incident_id"] == "inc_01_n_plus_one_query"
    assert result["agent"] == "logs_agent"
    assert result["evidence"][0]["evidence_id"] == "EV-LOG-001"
    assert result["evidence"][0]["excerpt"] == excerpt
    assert result["evidence"][0]["interpretation"] == "Gateway timeout on bulk order fetch."


def test_malformed_llm_response_is_handled(mock_llm_client):
    agent = LogsAgent(llm_client=mock_llm_client)
    mock_llm_client.generate_structured.side_effect = LLMJSONParseError("not json")
    result = agent.extract_evidence(SAMPLE_INCIDENT_DIR)
    assert result["incident_id"] == "inc_01_n_plus_one_query"
    assert result["agent"] == "logs_agent"
    assert result["evidence"], "Deterministic fallback should still return extracted lines"
    jsonschema.validate(instance=result, schema=agent.schema)


def test_empty_and_noisy_logs_are_handled(mock_llm_client, tmp_path: Path):
    empty_dir = tmp_path / "inc_empty_logs"
    (empty_dir / "logs").mkdir(parents=True)
    (empty_dir / "logs" / "application.log").write_text("", encoding="utf-8")

    noisy_dir = tmp_path / "inc_noisy_logs"
    (noisy_dir / "logs").mkdir(parents=True)
    (noisy_dir / "logs" / "application.log").write_text(
        "hello world\nnot a structured log\njust chatter\n",
        encoding="utf-8",
    )

    agent = LogsAgent(llm_client=mock_llm_client)
    empty_result = agent.extract_evidence(empty_dir)
    noisy_result = agent.extract_evidence(noisy_dir)

    assert empty_result["incident_id"] == "inc_empty_logs"
    assert empty_result["evidence"] == []
    assert noisy_result["incident_id"] == "inc_noisy_logs"
    assert noisy_result["evidence"] == []
    assert mock_llm_client.generate_structured.call_count == 0


def test_incident_id_preserved_and_evidence_ids_unique(mock_llm_client):
    agent = LogsAgent(llm_client=mock_llm_client)
    log_lines = (SAMPLE_INCIDENT_DIR / "logs" / "application.log").read_text(encoding="utf-8").splitlines()
    error_indices = [i for i, line in enumerate(log_lines, start=1) if "ERROR" in line][:2]
    payload_evidence = []
    for idx in error_indices:
        payload_evidence.append(
            {
                "evidence_id": "EV-LOG-001",
                "source": "logs",
                "reference": f"logs/application.log:{idx}",
                "timestamp": log_lines[idx - 1].split(" ")[0],
                "type": "error",
                "excerpt": log_lines[idx - 1],
                "interpretation": "Error line",
            }
        )
    _mock_structured(
        mock_llm_client,
        {
            "incident_id": "ignored",
            "agent": "logs_agent",
            "summary": "Multiple errors",
            "evidence": payload_evidence,
        },
    )
    result = agent.extract_evidence(SAMPLE_INCIDENT_DIR)
    ids = [item["evidence_id"] for item in result["evidence"]]
    assert result["incident_id"] == "inc_01_n_plus_one_query"
    assert ids == [f"EV-LOG-{i:03d}" for i in range(1, len(ids) + 1)]
    assert len(ids) == len(set(ids))


def test_evidence_references_are_real_lines(mock_llm_client):
    agent = LogsAgent(llm_client=mock_llm_client)
    log_lines = (SAMPLE_INCIDENT_DIR / "logs" / "application.log").read_text(encoding="utf-8").splitlines()
    _mock_structured(
        mock_llm_client,
        {
            "incident_id": "inc_01_n_plus_one_query",
            "agent": "logs_agent",
            "summary": "ok",
            "evidence": [
                {
                    "evidence_id": "EV-LOG-001",
                    "source": "logs",
                    "reference": "logs/application.log:9999",
                    "timestamp": "",
                    "type": "error",
                    "excerpt": "this excerpt does not exist in the file",
                    "interpretation": "hallucinated",
                }
            ],
        },
    )
    result = agent.extract_evidence(SAMPLE_INCIDENT_DIR)
    for item in result["evidence"]:
        _, line_s = item["reference"].rsplit(":", 1)
        line_no = int(line_s)
        assert log_lines[line_no - 1] == item["excerpt"]


def test_incident_files_are_never_modified(mock_llm_client):
    agent = LogsAgent(llm_client=mock_llm_client)
    _mock_structured(
        mock_llm_client,
        {
            "incident_id": "inc_01_n_plus_one_query",
            "agent": "logs_agent",
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
    assert set(snapshot) == set(after)
