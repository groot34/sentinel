"""Custom exceptions for Sentinel 2.0 LLM provider layer."""

from __future__ import annotations


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
