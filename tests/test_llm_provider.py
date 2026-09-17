"""Unit tests for Sentinel 2.0 LLM Provider Abstraction Layer (Mission 07).

Verifies:
1. BaseLLMProvider abstract interface enforcement.
2. GroqLLMClient subclass and GroqProvider alias relationship.
3. Thread-safe provider factory (get_llm_provider, get_llm_client, force_new, unsupported provider).
4. Exact token-accounting invariants (success, missing usage, retries, API failure, JSON failure).
5. Temperature pass-through transparency.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch
import pytest

from core.llm import (
    BaseLLMProvider,
    GroqLLMClient,
    GroqProvider,
    LLMAPIError,
    LLMJSONParseError,
    LLMResponse,
    get_llm_client,
    get_llm_provider,
)


def test_base_provider_is_abstract():
    """Verify that BaseLLMProvider cannot be instantiated directly."""
    with pytest.raises(TypeError, match="Can't instantiate abstract class"):
        BaseLLMProvider()  # type: ignore


def test_groq_client_implements_base_provider():
    """Verify class hierarchy and provider aliases."""
    assert issubclass(GroqLLMClient, BaseLLMProvider)
    assert GroqProvider is GroqLLMClient
    assert issubclass(GroqProvider, BaseLLMProvider)


def test_factory_returns_singleton_groq_client(monkeypatch):
    """Verify factory returns same instance on repeated calls and GroqLLMClient type."""
    monkeypatch.setenv("GROQ_API_KEY", "gsk_test_key_12345678901234567890")
    monkeypatch.setenv("GROQ_MODEL", "openai/gpt-oss-120b")

    provider1 = get_llm_provider("groq", force_new=True)
    provider2 = get_llm_provider("groq")

    assert provider1 is provider2
    assert isinstance(provider1, GroqLLMClient)
    assert isinstance(provider1, BaseLLMProvider)
    assert provider1.provider_name == "groq"
    assert provider1.default_model == "openai/gpt-oss-120b"

    # get_llm_client compatibility
    client = get_llm_client()
    assert client is provider1


def test_factory_force_new_creates_distinct_instance(monkeypatch):
    """Verify force_new=True instantiates a fresh instance."""
    monkeypatch.setenv("GROQ_API_KEY", "gsk_test_key_12345678901234567890")
    monkeypatch.setenv("GROQ_MODEL", "openai/gpt-oss-120b")

    inst1 = get_llm_provider("groq", force_new=True)
    inst2 = get_llm_provider("groq", force_new=True)

    assert inst1 is not inst2
    assert isinstance(inst1, GroqLLMClient)
    assert isinstance(inst2, GroqLLMClient)


def test_factory_unsupported_provider_raises_value_error():
    """Verify requesting an unregistered/unsupported provider raises clean ValueError."""
    with pytest.raises(ValueError, match="Unsupported LLM provider: 'unsupported_engine'"):
        get_llm_provider("unsupported_engine")


def test_temperature_passthrough():
    """Verify temperature is passed through directly without rejection or alteration."""
    mock_choice = MagicMock()
    mock_choice.message.content = "response"
    mock_completion = MagicMock()
    mock_completion.choices = [mock_choice]
    mock_completion.usage = None

    mock_raw = MagicMock()
    mock_raw.chat.completions.create.return_value = mock_completion

    client = GroqLLMClient(
        api_key="gsk_mock_key_12345678901234567890",
        client_instance=mock_raw,
    )

    # Pass non-zero temperature (e.g. 0.7)
    client.generate("Test prompt", temperature=0.7)
    _, kwargs = mock_raw.chat.completions.create.call_args
    assert kwargs["temperature"] == 0.7


# ============================================================================
# Token-Accounting Behaviour Tests (The 5 Rules)
# ============================================================================

def test_token_accounting_rule1_successful_response():
    """Rule 1: Successful response increments calls and accumulates usage tokens."""
    mock_choice = MagicMock()
    mock_choice.message.content = "output text"

    mock_usage = MagicMock()
    mock_usage.prompt_tokens = 50
    mock_usage.completion_tokens = 20
    mock_usage.total_tokens = 70

    mock_completion = MagicMock()
    mock_completion.choices = [mock_choice]
    mock_completion.usage = mock_usage

    mock_raw = MagicMock()
    mock_raw.chat.completions.create.return_value = mock_completion

    client = GroqLLMClient(
        api_key="gsk_mock_key_12345678901234567890",
        client_instance=mock_raw,
    )

    resp = client.generate("Hello world")
    assert resp.prompt_tokens == 50
    assert resp.completion_tokens == 20
    assert resp.total_tokens == 70

    usage = client.get_session_token_usage()
    assert usage["prompt_tokens"] == 50
    assert usage["completion_tokens"] == 20
    assert usage["total_tokens"] == 70
    assert usage["llm_calls"] == 1

    client.reset_session_token_usage()
    reset_usage = client.get_session_token_usage()
    assert reset_usage == {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0, "llm_calls": 0}


def test_token_accounting_rule2_missing_usage_fields():
    """Rule 2: Missing usage leaves tokens unchanged but increments llm_calls."""
    mock_choice = MagicMock()
    mock_choice.message.content = "no usage output"

    mock_completion = MagicMock()
    mock_completion.choices = [mock_choice]
    mock_completion.usage = None

    mock_raw = MagicMock()
    mock_raw.chat.completions.create.return_value = mock_completion

    client = GroqLLMClient(
        api_key="gsk_mock_key_12345678901234567890",
        client_instance=mock_raw,
    )

    resp = client.generate("Hello world")
    assert resp.prompt_tokens is None
    assert resp.completion_tokens is None
    assert resp.total_tokens is None

    usage = client.get_session_token_usage()
    assert usage["prompt_tokens"] == 0
    assert usage["completion_tokens"] == 0
    assert usage["total_tokens"] == 0
    assert usage["llm_calls"] == 1


def test_token_accounting_rule3_retries():
    """Rule 3: Failed retry attempts do not record tokens or calls; success records once."""
    mock_choice = MagicMock()
    mock_choice.message.content = "recovered"

    mock_usage = MagicMock()
    mock_usage.prompt_tokens = 30
    mock_usage.completion_tokens = 10
    mock_usage.total_tokens = 40

    mock_completion = MagicMock()
    mock_completion.choices = [mock_choice]
    mock_completion.usage = mock_usage

    mock_raw = MagicMock()
    # 1st attempt fails with transient connection error, 2nd attempt succeeds
    mock_raw.chat.completions.create.side_effect = [
        RuntimeError("connection reset by peer"),
        mock_completion,
    ]

    client = GroqLLMClient(
        api_key="gsk_mock_key_12345678901234567890",
        client_instance=mock_raw,
        max_retries=1,
    )

    with patch("time.sleep"):
        resp = client.generate("Hello world")

    assert resp.content == "recovered"
    usage = client.get_session_token_usage()
    # Only 1 successful call and tokens from that attempt are recorded
    assert usage["llm_calls"] == 1
    assert usage["prompt_tokens"] == 30
    assert usage["total_tokens"] == 40


def test_token_accounting_rule4_api_failure():
    """Rule 4: When all attempts fail, zero tokens and zero calls are recorded."""
    mock_raw = MagicMock()
    mock_raw.chat.completions.create.side_effect = RuntimeError("Fatal 500 error")

    client = GroqLLMClient(
        api_key="gsk_mock_key_12345678901234567890",
        client_instance=mock_raw,
        max_retries=1,
    )

    with patch("time.sleep"):
        with pytest.raises(LLMAPIError):
            client.generate("Hello world")

    usage = client.get_session_token_usage()
    assert usage["llm_calls"] == 0
    assert usage["total_tokens"] == 0


def test_token_accounting_rule5_json_parsing_failure():
    """Rule 5: When model returns invalid JSON, tokens and llm_calls are recorded before error."""
    mock_choice = MagicMock()
    mock_choice.message.content = "Malformed JSON string {not: valid}"

    mock_usage = MagicMock()
    mock_usage.prompt_tokens = 45
    mock_usage.completion_tokens = 15
    mock_usage.total_tokens = 60

    mock_completion = MagicMock()
    mock_completion.choices = [mock_choice]
    mock_completion.usage = mock_usage

    mock_raw = MagicMock()
    mock_raw.chat.completions.create.return_value = mock_completion

    client = GroqLLMClient(
        api_key="gsk_mock_key_12345678901234567890",
        client_instance=mock_raw,
    )

    with pytest.raises(LLMJSONParseError):
        client.generate_structured("Extract JSON", schema={"type": "object"})

    usage = client.get_session_token_usage()
    # Telemetry must accurately reflect that the API call happened and consumed tokens
    assert usage["llm_calls"] == 1
    assert usage["prompt_tokens"] == 45
    assert usage["total_tokens"] == 60
