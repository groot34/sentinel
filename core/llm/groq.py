"""Groq LLM provider implementation for Sentinel 2.0.

Provides centralized Groq client integration with rate limit backoff,
structured JSON response extraction, token telemetry, and secret sanitization.
"""

from __future__ import annotations

import json
import os
import re
import time
from typing import Any, Dict, List, Optional
from dotenv import load_dotenv

from core.llm.base import BaseLLMProvider
from core.llm.errors import (
    LLMAPIError,
    LLMConfigurationError,
    LLMJSONParseError,
    LLMRateLimitError,
)
from core.llm.models import LLMResponse

# Load local environment if available
load_dotenv()


# ---------------------------------------------------------------------------
# Helper: Secret Masking
# ---------------------------------------------------------------------------
def _sanitize_message(message: str, secret: Optional[str] = None) -> str:
    """Remove sensitive API key patterns or the provided secret from error strings."""
    if not message:
        return ""
    sanitized = message
    if secret and len(secret) > 4:
        sanitized = sanitized.replace(secret, "***REDACTED_API_KEY***")
    # Common Groq/OpenAI key patterns (gsk_... or sk-...)
    sanitized = re.sub(r"gsk_[a-zA-Z0-9]{20,}", "***REDACTED_GROQ_KEY***", sanitized)
    sanitized = re.sub(r"sk-[a-zA-Z0-9]{20,}", "***REDACTED_KEY***", sanitized)
    return sanitized


# ---------------------------------------------------------------------------
# Client / Provider Implementation
# ---------------------------------------------------------------------------
class GroqLLMClient(BaseLLMProvider):
    """Unified Groq client and provider for Sentinel incident investigation agents."""

    DEFAULT_MODEL = "openai/gpt-oss-120b"
    DEFAULT_TIMEOUT_SECONDS = 30.0
    DEFAULT_MAX_RETRIES = 2

    provider_name: str = "groq"
    default_model: str = DEFAULT_MODEL

    def __init__(
        self,
        api_key: Optional[str] = None,
        model: Optional[str] = None,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        max_retries: int = DEFAULT_MAX_RETRIES,
        client_instance: Optional[Any] = None,
    ) -> None:
        self.api_key = api_key if api_key is not None else os.getenv("GROQ_API_KEY")
        if model is not None:
            self.model = model
        else:
            self.model = os.getenv("GROQ_MODEL") or self.DEFAULT_MODEL
        self.timeout = timeout
        self._last_response: Optional[LLMResponse] = None
        self.max_retries = max(0, max_retries)

        # Session token tracking
        self._session_prompt_tokens: int = 0
        self._session_completion_tokens: int = 0
        self._session_total_tokens: int = 0
        self._session_llm_calls: int = 0

        # Allow dependency injection of a mocked/pre-configured client instance for unit testing
        self._raw_client = client_instance

        self._validate_configuration()

    def get_session_token_usage(self) -> Dict[str, int]:
        """Return cumulative token usage and call count for this client session."""
        return {
            "prompt_tokens": self._session_prompt_tokens,
            "completion_tokens": self._session_completion_tokens,
            "total_tokens": self._session_total_tokens,
            "llm_calls": self._session_llm_calls,
        }

    def reset_session_token_usage(self) -> None:
        """Reset session token and call counters."""
        self._session_prompt_tokens = 0
        self._session_completion_tokens = 0
        self._session_total_tokens = 0
        self._session_llm_calls = 0

    def _validate_configuration(self) -> None:
        """Validate API key and model presence without exposing secrets."""
        if not self.api_key or not self.api_key.strip():
            raise LLMConfigurationError(
                "Missing required GROQ_API_KEY environment variable. "
                "Please configure GROQ_API_KEY in your environment or .env file."
            )
        if not self.model or not self.model.strip():
            raise LLMConfigurationError(
                "Missing required GROQ_MODEL environment variable. "
                "Please set GROQ_MODEL (e.g., 'llama-3.3-70b-versatile')."
            )

    def _get_client(self) -> Any:
        """Lazily initialize the official Groq client if not injected."""
        if self._raw_client is not None:
            return self._raw_client
        try:
            from groq import Groq
            self._raw_client = Groq(api_key=self.api_key, timeout=self.timeout)
            return self._raw_client
        except ImportError:
            raise LLMConfigurationError("The 'groq' package is not installed. Please run: pip install groq")
        except Exception as e:
            sanitized_err = _sanitize_message(str(e), self.api_key)
            raise LLMConfigurationError(f"Failed to initialize Groq client: {sanitized_err}")

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
            LLMResponse with content, metadata, and token usage.
        """
        messages = self._build_messages(prompt, system_prompt)
        return self._execute_call(messages, temperature=temperature, max_tokens=max_tokens, response_format=None)

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
            LLMResponse containing both raw string content and parsed JSON dictionary.
        """
        augmented_system = system_prompt or "You are an expert production incident investigator."
        if schema:
            augmented_system += f"\n\nYou MUST respond ONLY with valid JSON conforming to this schema:\n{json.dumps(schema)}"
        else:
            augmented_system += "\n\nYou MUST respond ONLY with valid JSON."

        messages = self._build_messages(prompt, augmented_system)

        # Groq supports json_object response format
        response_format = {"type": "json_object"}
        response = self._execute_call(
            messages,
            temperature=temperature,
            max_tokens=max_tokens,
            response_format=response_format,
        )

        # Parse and sanitize JSON output
        parsed = self._extract_json(response.content)
        response.parsed_json = parsed
        return response

    def _build_messages(self, prompt: str, system_prompt: Optional[str]) -> List[Dict[str, str]]:
        """Construct standard chat completions message payload."""
        messages: List[Dict[str, str]] = []
        if system_prompt and system_prompt.strip():
            messages.append({"role": "system", "content": system_prompt.strip()})
        messages.append({"role": "user", "content": prompt})
        return messages

    def _execute_call(
        self,
        messages: List[Dict[str, str]],
        temperature: float = 0.0,
        max_tokens: Optional[int] = None,
        response_format: Optional[Dict[str, str]] = None,
    ) -> LLMResponse:
        """Execute chat completion with conservative exponential backoff retries."""
        client = self._get_client()
        attempts = 0
        last_exception: Optional[Exception] = None

        while attempts <= self.max_retries:
            attempts += 1
            start_time = time.perf_counter()
            try:
                kwargs: Dict[str, Any] = {
                    "model": self.model,
                    "messages": messages,
                    "temperature": temperature,
                }
                if max_tokens is not None:
                    kwargs["max_tokens"] = max_tokens
                if response_format is not None:
                    kwargs["response_format"] = response_format

                chat_completion = client.chat.completions.create(**kwargs)
                latency_ms = round((time.perf_counter() - start_time) * 1000.0, 2)

                # Extract choices and content
                choice = chat_completion.choices[0]
                content = choice.message.content or ""
                finish_reason = getattr(choice, "finish_reason", None)

                # Extract token usage if provided by API
                prompt_tokens = None
                completion_tokens = None
                total_tokens = None
                if hasattr(chat_completion, "usage") and chat_completion.usage is not None:
                    prompt_tokens = getattr(chat_completion.usage, "prompt_tokens", None)
                    completion_tokens = getattr(chat_completion.usage, "completion_tokens", None)
                    total_tokens = getattr(chat_completion.usage, "total_tokens", None)

                if prompt_tokens is not None:
                    self._session_prompt_tokens += prompt_tokens
                if completion_tokens is not None:
                    self._session_completion_tokens += completion_tokens
                if total_tokens is not None:
                    self._session_total_tokens += total_tokens
                self._session_llm_calls += 1

                resp = LLMResponse(
                    content=content,
                    model=self.model,
                    latency_ms=latency_ms,
                    prompt_tokens=prompt_tokens,
                    completion_tokens=completion_tokens,
                    total_tokens=total_tokens,
                    finish_reason=finish_reason,
                )
                self._last_response = resp
                return resp

            except Exception as e:
                last_exception = e
                err_str = str(e).lower()
                is_rate_limit = "rate limit" in err_str or "429" in err_str
                is_timeout = "timeout" in err_str or "timed out" in err_str

                # If retryable and attempts remaining, apply backoff
                if (is_rate_limit or is_timeout or "connection" in err_str) and attempts <= self.max_retries:
                    backoff_delay = 1.0 * (2 ** (attempts - 1))
                    time.sleep(backoff_delay)
                    continue

                sanitized_error = _sanitize_message(str(e), self.api_key)
                if is_rate_limit:
                    raise LLMRateLimitError(
                        f"Groq API rate limit exceeded after {attempts} attempts: {sanitized_error}"
                    ) from None
                raise LLMAPIError(
                    f"Groq API call failed: {sanitized_error}"
                ) from None

        sanitized_error = _sanitize_message(str(last_exception), self.api_key)
        raise LLMAPIError(f"Groq API call failed after {self.max_retries} retries: {sanitized_error}")

    def _extract_json(self, raw_text: str) -> Dict[str, Any]:
        """Extract and parse JSON safely, stripping any markdown wrappers if present."""
        text = raw_text.strip()
        if not text:
            raise LLMJSONParseError("Received empty response from LLM; cannot parse JSON.")

        # Strip markdown ```json ... ``` code blocks
        if text.startswith("```"):
            text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
            text = re.sub(r"\s*```$", "", text)
            text = text.strip()

        try:
            return json.loads(text)
        except json.JSONDecodeError as err:
            # Attempt to locate JSON object substring within text
            match = re.search(r"(\{.*\})", text, flags=re.DOTALL)
            if match:
                try:
                    return json.loads(match.group(1))
                except Exception:
                    pass
            raise LLMJSONParseError(
                f"Failed to parse LLM structured response as JSON: {err.msg}. Raw excerpt: {text[:200]}"
            )


# Compatibility alias
GroqProvider = GroqLLMClient
