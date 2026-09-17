"""Response models for Sentinel 2.0 LLM provider layer."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Dict, Optional

from core.llm.errors import LLMJSONParseError


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
