"""Comprehensive unit tests for Sentinel's centralized Groq LLM client.

All tests utilize mocked client interfaces to run fast, deterministically,
and without requiring real API keys or external network calls.
"""

import json
from unittest.mock import MagicMock, patch
import pytest

from core.llm import (
    GroqLLMClient,
    LLMConfigurationError,
    LLMAPIError,
    LLMRateLimitError,
    LLMJSONParseError,
    LLMResponse,
    _sanitize_message,
)


def test_missing_groq_api_key_raises_configuration_error(monkeypatch):
    """Test that missing or empty GROQ_API_KEY raises a clean LLMConfigurationError."""
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    with pytest.raises(LLMConfigurationError) as exc_info:
        GroqLLMClient(api_key=None)
    assert "Missing required GROQ_API_KEY" in str(exc_info.value)


def test_missing_groq_model_raises_configuration_error(monkeypatch):
    """Test that missing GROQ_MODEL without default raises LLMConfigurationError."""
    monkeypatch.setenv("GROQ_API_KEY", "dummy_mock_key")
    monkeypatch.setenv("GROQ_MODEL", "")
    with pytest.raises(LLMConfigurationError) as exc_info:
        GroqLLMClient(api_key="dummy_mock_key", model="")
    assert "Missing required GROQ_MODEL" in str(exc_info.value)


def test_successful_text_generation_with_mock():
    """Test standard text generation with a mocked Groq client."""
    mock_choice = MagicMock()
    mock_choice.message.content = "Diagnosed root cause: N+1 query pattern in serializer loop."
    mock_choice.finish_reason = "stop"

    mock_usage = MagicMock()
    mock_usage.prompt_tokens = 120
    mock_usage.completion_tokens = 25
    mock_usage.total_tokens = 145

    mock_completion = MagicMock()
    mock_completion.choices = [mock_choice]
    mock_completion.usage = mock_usage

    mock_raw_client = MagicMock()
    mock_raw_client.chat.completions.create.return_value = mock_completion

    client = GroqLLMClient(
        api_key="gsk_test_mock_secret_key_12345",
        model="llama-3.3-70b-versatile",
        client_instance=mock_raw_client,
    )

    response = client.generate("Analyze this incident bundle")

    assert response.content == "Diagnosed root cause: N+1 query pattern in serializer loop."
    assert response.model == "llama-3.3-70b-versatile"
    assert response.prompt_tokens == 120
    assert response.completion_tokens == 25
    assert response.total_tokens == 145
    assert response.latency_ms >= 0.0
    mock_raw_client.chat.completions.create.assert_called_once()


def test_successful_structured_json_generation_with_mock():
    """Test structured JSON response extraction and parsing."""
    structured_payload = {
        "incident_id": "INC-001",
        "root_cause": "N+1 query loop",
        "confidence": 0.95,
        "evidence_ids": ["EV-LOG-001", "EV-CODE-002"]
    }

    mock_choice = MagicMock()
    mock_choice.message.content = json.dumps(structured_payload)
    mock_choice.finish_reason = "stop"

    mock_completion = MagicMock()
    mock_completion.choices = [mock_choice]
    mock_completion.usage = None

    mock_raw_client = MagicMock()
    mock_raw_client.chat.completions.create.return_value = mock_completion

    client = GroqLLMClient(
        api_key="gsk_test_mock_secret_key_12345",
        model="llama-3.3-70b-versatile",
        client_instance=mock_raw_client,
    )

    response = client.generate_structured(
        prompt="Extract root cause",
        schema={"type": "object", "properties": {"root_cause": {"type": "string"}}}
    )

    assert response.parsed_json == structured_payload
    assert response.get_structured()["incident_id"] == "INC-001"
    assert response.get_structured()["confidence"] == 0.95


def test_structured_json_with_markdown_codeblock_stripping():
    """Test that markdown code blocks (```json ... ```) are cleanly stripped and parsed."""
    structured_payload = {"status": "CONFIRMED", "evidence_count": 2}
    raw_markdown = f"```json\n{json.dumps(structured_payload)}\n```"

    mock_choice = MagicMock()
    mock_choice.message.content = raw_markdown
    mock_choice.finish_reason = "stop"

    mock_completion = MagicMock()
    mock_completion.choices = [mock_choice]
    mock_completion.usage = None

    mock_raw_client = MagicMock()
    mock_raw_client.chat.completions.create.return_value = mock_completion

    client = GroqLLMClient(
        api_key="gsk_test_mock_secret_key_12345",
        client_instance=mock_raw_client,
    )

    response = client.generate_structured(prompt="Verify hypothesis")
    assert response.parsed_json == structured_payload


def test_malformed_json_raises_llm_json_parse_error():
    """Test that completely invalid JSON raises LLMJSONParseError."""
    mock_choice = MagicMock()
    mock_choice.message.content = "This is plain unstructured text, not valid JSON {bad-format:"
    mock_choice.finish_reason = "stop"

    mock_completion = MagicMock()
    mock_completion.choices = [mock_choice]
    mock_completion.usage = None

    mock_raw_client = MagicMock()
    mock_raw_client.chat.completions.create.return_value = mock_completion

    client = GroqLLMClient(
        api_key="gsk_test_mock_secret_key_12345",
        client_instance=mock_raw_client,
    )

    with pytest.raises(LLMJSONParseError) as exc_info:
        client.generate_structured(prompt="Extract data")
    assert "Failed to parse LLM structured response as JSON" in str(exc_info.value)


def test_api_failure_raises_llm_api_error():
    """Test that upstream generic API errors are wrapped cleanly in LLMAPIError."""
    mock_raw_client = MagicMock()
    mock_raw_client.chat.completions.create.side_effect = RuntimeError("Internal server connection error")

    client = GroqLLMClient(
        api_key="gsk_test_mock_secret_key_12345",
        client_instance=mock_raw_client,
        max_retries=1,
    )

    with pytest.raises(LLMAPIError) as exc_info:
        client.generate("Test prompt")
    assert "Groq API call failed" in str(exc_info.value)


def test_rate_limit_error_and_retry_exhaustion():
    """Test that 429 rate limit errors trigger backoff retries and raise LLMRateLimitError upon exhaustion."""
    mock_raw_client = MagicMock()
    mock_raw_client.chat.completions.create.side_effect = Exception("Rate limit reached: 429 Too Many Requests")

    client = GroqLLMClient(
        api_key="gsk_test_mock_secret_key_12345",
        client_instance=mock_raw_client,
        max_retries=1,  # 1 retry = 2 total attempts
    )

    with patch("time.sleep") as mock_sleep:
        with pytest.raises(LLMRateLimitError) as exc_info:
            client.generate("Test prompt")
        assert "rate limit exceeded" in str(exc_info.value).lower()
        assert mock_raw_client.chat.completions.create.call_count == 2
        mock_sleep.assert_called_once()


def test_secret_is_never_exposed_in_error_output():
    """Test that secret keys are scrubbed and never appear in error strings or exceptions."""
    secret_key = "gsk_real_secret_production_key_abcdef123456789"
    
    mock_raw_client = MagicMock()
    # Simulate an error message where the library reflects the API key
    mock_raw_client.chat.completions.create.side_effect = Exception(
        f"Unauthorized request using key {secret_key}: invalid permissions"
    )

    client = GroqLLMClient(
        api_key=secret_key,
        client_instance=mock_raw_client,
        max_retries=0,
    )

    with pytest.raises(LLMAPIError) as exc_info:
        client.generate("Test prompt")
    
    error_message = str(exc_info.value)
    assert secret_key not in error_message, "Secret API key leaked in exception message!"
    assert "***REDACTED" in error_message


def test_sanitize_message_utility():
    """Test helper sanitization function across different key patterns."""
    raw = "Failed on gsk_abcdef1234567890123456 with sk-12345678901234567890"
    sanitized = _sanitize_message(raw)
    assert "gsk_abcdef" not in sanitized
    assert "sk-12345" not in sanitized
    assert "***REDACTED" in sanitized
