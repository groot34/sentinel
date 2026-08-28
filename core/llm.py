"""Centralized Groq Runtime LLM Client for Sentinel.

This module provides the single unified interface for all Sentinel runtime agents.
Individual agents must never instantiate provider SDKs directly.

Features:
- Configurable via environment variables (GROQ_API_KEY, GROQ_MODEL).
- Free-tier rate limit protection with conservative exponential retries.
- Support for unstructured text and structured JSON responses.
- Robust secret masking to prevent credentials leaking into logs, traces, or errors.
- Latency and token usage tracking without fabricated metrics.
"""

import json
import os
import re
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional
from dotenv import load_dotenv

# Load local environment if available
load_dotenv()


# ---------------------------------------------------------------------------
# Custom Exceptions
# ---------------------------------------------------------------------------
class LLMError(Exception):
    """Base exception for all Sentinel LLM errors."""
    pass


class LLMConfigurationError(LLMError):
    """Raised when required environment variables or configuration values are missing."""
    pass


class LLMAPIError(LLMError):
    """Raised when an upstream API call fails after retries."""
    pass


class LLMRateLimitError(LLMError):
    """Raised when rate limits are hit and retry attempts are exhausted."""
    pass


class LLMJSONParseError(LLMError):
    """Raised when a structured JSON response cannot be parsed."""
    pass


# ---------------------------------------------------------------------------
# Response Model
# ---------------------------------------------------------------------------
@dataclass
class LLMResponse:
    """Standardized response payload from LLM generation."""
    content: str
    parsed_json: Optional[Dict[str, Any]] = None
    model: str = ""
    latency_ms: float = 0.0
    prompt_tokens: Optional[int] = None
    completion_tokens: Optional[int] = None
    total_tokens: Optional[int] = None
    finish_reason: Optional[str] = None

    def get_structured(self) -> Dict[str, Any]:
        """Convenience helper to retrieve parsed JSON data."""
        if self.parsed_json is not None:
            return self.parsed_json
        if not self.content:
            raise LLMJSONParseError("Response content is empty.")
        try:
            return json.loads(self.content)
        except Exception as e:
            raise LLMJSONParseError(f"Failed to parse content as JSON: {e}")


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
# Client Implementation
# ---------------------------------------------------------------------------
class GroqLLMClient:
    """Unified Groq client for all Sentinel incident investigation agents."""

    DEFAULT_MODEL = "openai/gpt-oss-120b"
    DEFAULT_TIMEOUT_SECONDS = 30.0
    DEFAULT_MAX_RETRIES = 2


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

        # Allow dependency injection of a mocked/pre-configured client instance for unit testing
        self._raw_client = client_instance

        self._validate_configuration()


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


# ---------------------------------------------------------------------------
# Global Factory Helper
# ---------------------------------------------------------------------------
_CLIENT_INSTANCE: Optional[GroqLLMClient] = None


def get_llm_client(force_new: bool = False) -> GroqLLMClient:
    """Retrieve or initialize the singleton Groq LLM client.

    Args:
        force_new: If True, instantiates a fresh client from environment.

    Returns:
        Configured GroqLLMClient instance.
    """
    global _CLIENT_INSTANCE
    if _CLIENT_INSTANCE is None or force_new:
        _CLIENT_INSTANCE = GroqLLMClient()
    return _CLIENT_INSTANCE
