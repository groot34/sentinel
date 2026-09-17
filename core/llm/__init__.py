"""Sentinel 2.0 LLM Provider Abstraction Layer.

Decouples agent reasoning and orchestrator telemetry from concrete LLM SDKs:
- BaseLLMProvider: Abstract interface defining generation, JSON schemas, and token tracking.
- GroqLLMClient / GroqProvider: Concrete Groq provider with rate limit backoff and secret masking.
- get_llm_provider / get_llm_client: Synchronized provider factory and backwards-compatible singleton getter.
"""

from __future__ import annotations

from core.llm.base import BaseLLMProvider
from core.llm.errors import (
    LLMAPIError,
    LLMConfigurationError,
    LLMError,
    LLMJSONParseError,
    LLMRateLimitError,
)
from core.llm.factory import get_llm_client, get_llm_provider
from core.llm.groq import GroqLLMClient, GroqProvider, _sanitize_message
from core.llm.models import LLMResponse

__all__ = [
    # Base abstractions
    "BaseLLMProvider",
    # Providers
    "GroqLLMClient",
    "GroqProvider",
    # Models
    "LLMResponse",
    # Errors
    "LLMError",
    "LLMConfigurationError",
    "LLMAPIError",
    "LLMRateLimitError",
    "LLMJSONParseError",
    # Factory & Utilities
    "get_llm_provider",
    "get_llm_client",
    "_sanitize_message",
]
