"""Abstract base provider interface for Sentinel 2.0 LLM access."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Dict, Optional

from core.llm.models import LLMResponse


class BaseLLMProvider(ABC):
    """Abstract interface for all Sentinel runtime LLM providers.

    Decouples agents and orchestration from concrete third-party SDK clients.
    """

    @property
    @abstractmethod
    def provider_name(self) -> str:
        """Identifier for the provider (e.g. 'groq')."""
        raise NotImplementedError

    @property
    @abstractmethod
    def default_model(self) -> str:
        """Default model identifier for this provider."""
        raise NotImplementedError

    @abstractmethod
    def generate(
        self,
        prompt: str,
        system_prompt: Optional[str] = None,
        temperature: float = 0.0,
        max_tokens: Optional[int] = None,
    ) -> LLMResponse:
        """Generate unstructured text response.

        Args:
            prompt: User message prompt.
            system_prompt: Optional system context prompt.
            temperature: Sampling temperature (default 0.0 for deterministic output).
            max_tokens: Maximum tokens to generate.

        Returns:
            LLMResponse containing content, latency, and token metrics.
        """
        raise NotImplementedError

    @abstractmethod
    def generate_structured(
        self,
        prompt: str,
        schema: Optional[Dict[str, Any]] = None,
        system_prompt: Optional[str] = None,
        temperature: float = 0.0,
        max_tokens: Optional[int] = None,
    ) -> LLMResponse:
        """Generate structured JSON response adhering to requested schema.

        Args:
            prompt: User message prompt detailing the incident or decision request.
            schema: Optional JSON Schema dictionary or instructions to include in prompt.
            system_prompt: Optional system prompt context.
            temperature: Sampling temperature (default 0.0).
            max_tokens: Maximum tokens to generate.

        Returns:
            LLMResponse containing raw content and parsed JSON dictionary.
        """
        raise NotImplementedError

    @abstractmethod
    def get_session_token_usage(self) -> Dict[str, int]:
        """Return cumulative token usage and call count for this provider session.

        Returns:
            Dictionary with keys: 'prompt_tokens', 'completion_tokens', 'total_tokens', 'llm_calls'.
        """
        raise NotImplementedError

    @abstractmethod
    def reset_session_token_usage(self) -> None:
        """Reset session token and call counters to zero."""
        raise NotImplementedError
