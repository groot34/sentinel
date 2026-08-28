"""Unit test suite for Sentinel Baseline Agent.

Tests cover:
1. Valid diagnosis response formatting and schema conformance.
2. Correct incident ID mapping.
3. Strict exclusion of ground_truth.md from LLM prompt.
4. Malformed LLM response handling.
5. Missing required output fields handling and normalization.
6. LLM/API failure handling.
7. Evidence citations preservation.
8. Verifying exactly one primary LLM generation call per diagnosis.
9. Verification that baseline_agent.py never directly imports the Groq SDK.
"""

import json
from pathlib import Path
from unittest.mock import MagicMock
import pytest
import jsonschema

from baseline.baseline_agent import BaselineAgent
from core.llm import GroqLLMClient, LLMResponse, LLMAPIError, LLMJSONParseError


INCIDENTS_DIR = Path(__file__).parent.parent / "incidents"
SAMPLE_INCIDENT_DIR = INCIDENTS_DIR / "inc_01_n_plus_one_query"


@pytest.fixture
def mock_llm_client():
    """Mock GroqLLMClient returning structured JSON."""
    client = MagicMock(spec=GroqLLMClient)
    return client


def test_no_direct_groq_sdk_import_in_baseline():
    """Verify that baseline_agent.py never imports groq directly."""
    baseline_file = Path(__file__).parent.parent / "baseline" / "baseline_agent.py"
    content = baseline_file.read_text(encoding="utf-8")
    
    assert "import groq" not in content, "Direct 'import groq' found in baseline_agent.py!"
    assert "from groq import" not in content, "Direct 'from groq import' found in baseline_agent.py!"
    assert "core.llm" in content, "baseline_agent.py must use centralized core.llm abstraction."


def test_ground_truth_strictly_excluded_from_prompt(mock_llm_client):
    """Verify that ground_truth.md content is never loaded into the baseline prompt."""
    agent = BaselineAgent(llm_client=mock_llm_client)
    
    mock_llm_client.generate_structured.return_value = LLMResponse(
        content=json.dumps({
            "incident_id": "inc_01_n_plus_one_query",
            "root_cause_guess": "N+1 query loop",
            "reasoning": "Detected multiple sequential address queries in serializer loop.",
            "confidence": 0.9,
            "evidence": []
        }),
        parsed_json={
            "incident_id": "inc_01_n_plus_one_query",
            "root_cause_guess": "N+1 query loop",
            "reasoning": "Detected multiple sequential address queries in serializer loop.",
            "confidence": 0.9,
            "evidence": []
        }
    )

    agent.diagnose(SAMPLE_INCIDENT_DIR)

    # Inspect the prompt passed to generate_structured
    call_args = mock_llm_client.generate_structured.call_args
    prompt_passed = call_args.kwargs.get("prompt") or call_args.args[0]
    
    assert "ground_truth.md" not in prompt_passed
    assert "Ground Truth:" not in prompt_passed
    assert "Underlying Root Cause" not in prompt_passed


def test_valid_diagnosis_conforms_to_schema(mock_llm_client):
    """Test successful single-shot diagnosis conforming strictly to baseline_schema.json."""
    agent = BaselineAgent(llm_client=mock_llm_client)

    sample_output = {
        "incident_id": "inc_01_n_plus_one_query",
        "root_cause_guess": "Database connection pool exhausted due to N+1 query loop in OrderSerializer.",
        "reasoning": "Commit c84a1f added an item address lookup inside a for loop.",
        "confidence": 0.88,
        "evidence": [
            {
                "source": "logs",
                "reference": "application.log:L7",
                "excerpt": "GET /api/v1/orders/bulk?page=1 200 OK - 145ms (queries: 51)"
            },
            {
                "source": "git_diff",
                "reference": "order_serializer.py:L14",
                "excerpt": "address = db_session.query_address_by_id(item.shipping_address_id)"
            }
        ],
        "suggested_mitigation": "Batch query addresses using query_addresses_batch."
    }

    mock_llm_client.generate_structured.return_value = LLMResponse(
        content=json.dumps(sample_output),
        parsed_json=sample_output
    )

    result = agent.diagnose(SAMPLE_INCIDENT_DIR)

    assert result["incident_id"] == "inc_01_n_plus_one_query"
    assert result["root_cause_guess"] == sample_output["root_cause_guess"]
    assert result["confidence"] == 0.88
    assert len(result["evidence"]) == 2
    # Verify strict JSON schema compliance
    jsonschema.validate(instance=result, schema=agent.schema)


def test_baseline_makes_exactly_one_primary_llm_call(mock_llm_client):
    """Verify that the baseline never executes more than one LLM call per diagnosis."""
    agent = BaselineAgent(llm_client=mock_llm_client)

    mock_llm_client.generate_structured.return_value = LLMResponse(
        content=json.dumps({
            "incident_id": "inc_01_n_plus_one_query",
            "root_cause_guess": "Pool exhaustion",
            "reasoning": "Logs show 20/20 connections held.",
            "confidence": 0.8,
            "evidence": []
        }),
        parsed_json={
            "incident_id": "inc_01_n_plus_one_query",
            "root_cause_guess": "Pool exhaustion",
            "reasoning": "Logs show 20/20 connections held.",
            "confidence": 0.8,
            "evidence": []
        }
    )

    agent.diagnose(SAMPLE_INCIDENT_DIR)

    assert mock_llm_client.generate_structured.call_count == 1
    assert mock_llm_client.generate.call_count == 0


def test_malformed_llm_json_response_raises_error(mock_llm_client):
    """Test that unrecoverable malformed JSON response propagates cleanly."""
    agent = BaselineAgent(llm_client=mock_llm_client)

    mock_llm_client.generate_structured.side_effect = LLMJSONParseError("Malformed JSON output from LLM")

    with pytest.raises(LLMJSONParseError):
        agent.diagnose(SAMPLE_INCIDENT_DIR)


def test_api_failure_raises_llm_api_error(mock_llm_client):
    """Test that upstream LLM API errors propagate without crashing unexpectedly."""
    agent = BaselineAgent(llm_client=mock_llm_client)

    mock_llm_client.generate_structured.side_effect = LLMAPIError("Groq 500 API error")

    with pytest.raises(LLMAPIError):
        agent.diagnose(SAMPLE_INCIDENT_DIR)


def test_missing_optional_fields_are_normalized(mock_llm_client):
    """Test that responses missing optional fields or using aliases are normalized."""
    agent = BaselineAgent(llm_client=mock_llm_client)

    # LLM returned 'root_cause' alias instead of 'root_cause_guess' and omitted 'evidence'
    raw_response = {
        "root_cause": "Cache key TTL set to 5s.",
        "reasoning": "High cache misses observed.",
        "confidence": "0.75"
    }

    mock_llm_client.generate_structured.return_value = LLMResponse(
        content=json.dumps(raw_response),
        parsed_json=raw_response
    )

    result = agent.diagnose(SAMPLE_INCIDENT_DIR)

    assert result["incident_id"] == "inc_01_n_plus_one_query"
    assert result["root_cause_guess"] == "Cache key TTL set to 5s."
    assert result["confidence"] == 0.75
    assert result["evidence"] == []
    jsonschema.validate(instance=result, schema=agent.schema)
