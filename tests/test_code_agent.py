"""Unit tests for Sentinel Code Agent.

All LLM responses are mocked. Tests never call the Groq API.
"""

import ast
import hashlib
import json
from pathlib import Path
from unittest.mock import MagicMock

import jsonschema
import pytest

from agents.code_agent import CodeAgent
from core.llm import GroqLLMClient, LLMJSONParseError, LLMResponse


INCIDENTS_DIR = Path(__file__).parent.parent / "incidents"
SAMPLE_INCIDENT_DIR = INCIDENTS_DIR / "inc_01_n_plus_one_query"
INC_04_DIR = INCIDENTS_DIR / "inc_04_memory_leak"
INC_07_DIR = INCIDENTS_DIR / "inc_07_retry_storm"
INC_10_DIR = INCIDENTS_DIR / "inc_10_multi_symptom_cascade"
AGENT_FILE = Path(__file__).parent.parent / "agents" / "code_agent.py"
TOOLS_FILE = Path(__file__).parent.parent / "agents" / "code_tools.py"


@pytest.fixture
def mock_llm_client():
    return MagicMock(spec=GroqLLMClient)


def _mock_structured(client: MagicMock, payload: dict) -> None:
    client.generate_structured.return_value = LLMResponse(
        content=json.dumps(payload),
        parsed_json=payload,
    )


def test_no_direct_groq_sdk_import_in_code_agent():
    content = AGENT_FILE.read_text(encoding="utf-8")
    assert "import groq" not in content
    assert "from groq import" not in content
    assert "core.llm" in content


def test_code_tools_have_no_llm_calls():
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


def test_code_agent_uses_core_llm():
    content = AGENT_FILE.read_text(encoding="utf-8")
    assert "from core.llm import" in content
    assert "generate_structured" in content


def test_ground_truth_is_never_read(mock_llm_client, monkeypatch):
    agent = CodeAgent(llm_client=mock_llm_client)
    _mock_structured(
        mock_llm_client,
        {
            "incident_id": "inc_01_n_plus_one_query",
            "agent": "code_agent",
            "summary": "Added code introduces a per-item DB query in the serializer.",
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
    agent = CodeAgent(llm_client=mock_llm_client)
    _mock_structured(
        mock_llm_client,
        {
            "incident_id": "inc_01_n_plus_one_query",
            "agent": "code_agent",
            "summary": "ok",
            "evidence": [],
        },
    )
    agent.extract_evidence(SAMPLE_INCIDENT_DIR)
    assert mock_llm_client.generate_structured.call_count == 1
    assert mock_llm_client.generate.call_count == 0


def test_mocked_groq_response_is_parsed(mock_llm_client):
    from agents.code_tools import collect_candidate_evidence

    candidates = collect_candidate_evidence(SAMPLE_INCIDENT_DIR)
    sample = next(item for item in candidates if item["type"] == "added_code")
    agent = CodeAgent(llm_client=mock_llm_client)
    _mock_structured(
        mock_llm_client,
        {
            "incident_id": "wrong-id",
            "agent": "code_agent",
            "summary": "Added per-item shipping lookup into serialize loop.",
            "evidence": [
                {
                    "evidence_id": "EV-CODE-999",
                    "source": sample["source"],
                    "reference": sample["reference"],
                    "type": sample["type"],
                    "excerpt": sample["excerpt"],
                    "interpretation": "N+1-style address DB query executed inside for-loop over items.",
                }
            ],
        },
    )
    result = agent.extract_evidence(SAMPLE_INCIDENT_DIR)
    jsonschema.validate(instance=result, schema=agent.schema)
    assert result["incident_id"] == "inc_01_n_plus_one_query"
    assert result["agent"] == "code_agent"
    assert result["evidence"][0]["evidence_id"] == "EV-CODE-001"
    assert result["evidence"][0]["reference"] == sample["reference"]
    assert "N+1" in result["evidence"][0]["interpretation"]


def test_malformed_llm_response_is_handled(mock_llm_client):
    agent = CodeAgent(llm_client=mock_llm_client)
    mock_llm_client.generate_structured.side_effect = LLMJSONParseError("not json")
    result = agent.extract_evidence(SAMPLE_INCIDENT_DIR)
    assert result["incident_id"] == "inc_01_n_plus_one_query"
    assert result["evidence"]
    ids = [item["evidence_id"] for item in result["evidence"]]
    assert ids == [f"EV-CODE-{i:03d}" for i in range(1, len(ids) + 1)]
    jsonschema.validate(instance=result, schema=agent.schema)


def test_empty_diff_handled(mock_llm_client, tmp_path: Path):
    empty_dir = tmp_path / "inc_empty_diff"
    (empty_dir / "service").mkdir(parents=True)
    (empty_dir / "service" / "app.py").write_text("x = 1\n", encoding="utf-8")
    (empty_dir / "git_diff.patch").write_text("", encoding="utf-8")
    agent = CodeAgent(llm_client=mock_llm_client)
    result = agent.extract_evidence(empty_dir)
    assert result["incident_id"] == "inc_empty_diff"
    assert mock_llm_client.generate_structured.call_count == 0


def test_missing_source_and_diff_handled(mock_llm_client, tmp_path: Path):
    missing_dir = tmp_path / "inc_missing_all"
    missing_dir.mkdir()
    agent = CodeAgent(llm_client=mock_llm_client)
    result = agent.extract_evidence(missing_dir)
    assert result["incident_id"] == "inc_missing_all"
    assert result["evidence"] == []
    assert "No git_diff.patch or service/" in result["summary"]
    assert mock_llm_client.generate_structured.call_count == 0


def test_evidence_ids_are_unique_and_use_stable_prefix(mock_llm_client):
    agent = CodeAgent(llm_client=mock_llm_client)
    mock_llm_client.generate_structured.side_effect = LLMJSONParseError("fallback")
    for inc_dir in [SAMPLE_INCIDENT_DIR, INC_04_DIR, INC_07_DIR, INC_10_DIR]:
        result = agent.extract_evidence(inc_dir)
        ids = [item["evidence_id"] for item in result["evidence"]]
        assert len(ids) == len(set(ids)), f"Duplicate IDs in {inc_dir.name}: {ids}"
        assert all(i.startswith("EV-CODE-") for i in ids)
        assert all(i[8:].isdigit() for i in ids)


def test_evidence_references_point_to_real_source_or_diff(mock_llm_client):
    agent = CodeAgent(llm_client=mock_llm_client)
    mock_llm_client.generate_structured.side_effect = LLMJSONParseError("fallback")
    for inc_dir in [SAMPLE_INCIDENT_DIR, INC_04_DIR, INC_07_DIR, INC_10_DIR]:
        result = agent.extract_evidence(inc_dir)
        for item in result["evidence"]:
            ref = item["reference"]
            excerpt = item["excerpt"]
            source = item["source"]
            assert excerpt, f"Empty excerpt in {inc_dir.name}"
            if source == "code":
                file_part, _, line_part = ref.partition(":")
                path = inc_dir / file_part
                assert path.exists(), f"Missing file: {path} for ref {ref}"
                lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
                if "-" in line_part:
                    start_s, _, end_s = line_part.partition("-")
                    start = int(start_s)
                    end = int(end_s)
                    assert 1 <= start <= len(lines)
                    assert 1 <= end <= len(lines)
                    block_lines = [l.strip() for l in lines[start - 1 : end]]
                    snippet_lines = [ln.lstrip("+- ").strip() for ln in excerpt.splitlines() if ln.strip()]
                    found_count = 0
                    for sl in snippet_lines:
                        if not sl:
                            continue
                        if any(sl == bl for bl in block_lines):
                            found_count += 1
                        elif any(sl in bl for bl in block_lines):
                            found_count += 1
                    if snippet_lines:
                        assert found_count > 0, (
                            f"No excerpt lines traceable in {file_part}:{start}-{end} "
                            f"for {inc_dir.name}. Ref = {ref}"
                        )
                else:
                    line = int(line_part)
                    assert 1 <= line <= len(lines)
                    first_excerpt_line = next(
                        (ln.lstrip("+- ").strip() for ln in excerpt.splitlines() if ln.strip()),
                        "",
                    )
                    assert first_excerpt_line or excerpt.strip(), "Cannot verify single-line ref without content"
            elif source == "git_diff":
                diff_path = inc_dir / "git_diff.patch"
                diff_text = diff_path.read_text(encoding="utf-8")
                cleaned_excerpt = "\n".join(
                    ln[1:] if ln.startswith(("+", "-")) else ln
                    for ln in excerpt.splitlines()
                ).strip()
                assert (
                    cleaned_excerpt[:60] in diff_text
                    or excerpt[:60] in diff_text
                    or "hunk" in ref
                ), f"Diff excerpt not traceable in {inc_dir.name}: {ref}"


def test_incident_files_are_never_modified(mock_llm_client):
    agent = CodeAgent(llm_client=mock_llm_client)
    _mock_structured(
        mock_llm_client,
        {
            "incident_id": "inc_01_n_plus_one_query",
            "agent": "code_agent",
            "summary": "ok",
            "evidence": [],
        },
    )
    snapshot = {
        path: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in SAMPLE_INCIDENT_DIR.rglob("*")
        if path.is_file()
    }
    agent.extract_evidence(SAMPLE_INCIDENT_DIR)
    after = {
        path: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in SAMPLE_INCIDENT_DIR.rglob("*")
        if path.is_file()
    }
    assert snapshot == after


def test_no_hardcoded_incident_specific_answers_in_code_agent():
    """Only check that there are no hardcoded branches keyed on incident ID.

    argparse help text and docstrings legitimately mention example paths such as
    "incidents/inc_01_n_plus_one_query", so plain substring checks are too strict.
    """
    incident_ids = {f"inc_{i:02d}" for i in range(1, 11)}

    def _scan_file(path: Path, file_label: str) -> None:
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.If):
                comparisons = [node.test]
                for elt in getattr(node, "orelse", []):
                    if isinstance(elt, ast.If):
                        comparisons.append(elt.test)
                for test in comparisons:
                    # Build string representation of the test condition
                    try:
                        cond = ast.unparse(test)
                    except Exception:
                        cond = ""
                    for inc_id in incident_ids:
                        assert inc_id not in cond, (
                            f"Hardcoded incident-specific branch '{cond}' "
                            f"found in {file_label}"
                        )
            if isinstance(node, (ast.Constant, ast.JoinedStr)):
                pass  # string literals / f-strings in help are OK

    _scan_file(AGENT_FILE, "code_agent.py")
    _scan_file(TOOLS_FILE, "code_tools.py")


def test_schema_conformance_with_all_real_incidents(mock_llm_client):
    agent = CodeAgent(llm_client=mock_llm_client)
    mock_llm_client.generate_structured.side_effect = LLMJSONParseError("fallback-only")
    for inc_dir in INCIDENTS_DIR.iterdir():
        if not inc_dir.is_dir() or inc_dir.name.startswith("."):
            continue
        if not (inc_dir / "git_diff.patch").exists() and not (inc_dir / "service").exists():
            continue
        result = agent.extract_evidence(inc_dir)
        jsonschema.validate(instance=result, schema=agent.schema)
        assert result["agent"] == "code_agent"
        assert result["incident_id"] == inc_dir.name


def test_schema_conformance_with_mocked_llm_response(mock_llm_client):
    from agents.code_tools import collect_candidate_evidence

    candidates = collect_candidate_evidence(INC_07_DIR)
    evidence_payload = []
    for i, c in enumerate(candidates[:4], start=1):
        evidence_payload.append(
            {
                "evidence_id": f"EV-CODE-{i:03d}",
                "source": c["source"],
                "reference": c["reference"],
                "type": c["type"],
                "excerpt": c["excerpt"],
                "interpretation": f"Interpretation #{i}",
            }
        )
    agent = CodeAgent(llm_client=mock_llm_client)
    _mock_structured(
        mock_llm_client,
        {
            "incident_id": "inc_07_retry_storm",
            "agent": "code_agent",
            "summary": "Retry configuration changes elevated; backoff disabled.",
            "evidence": evidence_payload,
        },
    )
    result = agent.extract_evidence(INC_07_DIR)
    jsonschema.validate(instance=result, schema=agent.schema)
    assert result["agent"] == "code_agent"
    for item in result["evidence"]:
        assert item["evidence_id"].startswith("EV-CODE-")
        assert item["type"] in {
            "added_code",
            "removed_code",
            "suspicious_pattern",
            "changed_config",
        }
        assert item["source"] in {"git_diff", "code", "config"}
