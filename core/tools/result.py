"""Tool execution result models for Sentinel 2.0.

Defines the standard execution envelope returned by all tools.
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Optional
from pydantic import BaseModel, ConfigDict, Field


class ToolStatus(str, Enum):
    """Execution status for a tool invocation."""

    SUCCESS = "SUCCESS"  # Executed successfully, including legitimate empty results
    FAILED = "FAILED"    # Expected operational failure (e.g. file missing, empty table)
    ERROR = "ERROR"      # Unexpected tool crash, defect, or safety boundary violation


class ToolResult(BaseModel):
    """Standardized execution envelope returned by all Sentinel tools."""

    tool_name: str
    status: ToolStatus
    success: bool
    data: Optional[Any] = None
    error: Optional[str] = None
    execution_time_ms: float = Field(default=0.0, ge=0.0)
    metadata: dict[str, Any] = Field(default_factory=dict)

    model_config = ConfigDict(
        frozen=True,
        use_enum_values=True,
        extra="forbid",
    )

    @property
    def is_success(self) -> bool:
        return self.status == ToolStatus.SUCCESS or self.status == ToolStatus.SUCCESS.value

    @property
    def is_failed(self) -> bool:
        return self.status == ToolStatus.FAILED or self.status == ToolStatus.FAILED.value

    @property
    def is_error(self) -> bool:
        return self.status == ToolStatus.ERROR or self.status == ToolStatus.ERROR.value

    @classmethod
    def ok(
        cls,
        tool_name: str,
        data: Any,
        execution_time_ms: float = 0.0,
        **metadata: Any,
    ) -> ToolResult:
        """Construct a successful result envelope.

        Legitimate empty results (e.g. search found zero matches) must use this method.
        """
        return cls(
            tool_name=tool_name,
            status=ToolStatus.SUCCESS,
            success=True,
            data=data,
            error=None,
            execution_time_ms=max(0.0, float(execution_time_ms)),
            metadata=metadata,
        )

    @classmethod
    def fail(
        cls,
        tool_name: str,
        error: str,
        execution_time_ms: float = 0.0,
        **metadata: Any,
    ) -> ToolResult:
        """Construct an expected operational failure envelope (e.g. file missing)."""
        return cls(
            tool_name=tool_name,
            status=ToolStatus.FAILED,
            success=False,
            data=None,
            error=str(error),
            execution_time_ms=max(0.0, float(execution_time_ms)),
            metadata=metadata,
        )

    @classmethod
    def err(
        cls,
        tool_name: str,
        error: str,
        execution_time_ms: float = 0.0,
        **metadata: Any,
    ) -> ToolResult:
        """Construct a tool defect or safety boundary violation envelope."""
        return cls(
            tool_name=tool_name,
            status=ToolStatus.ERROR,
            success=False,
            data=None,
            error=str(error),
            execution_time_ms=max(0.0, float(execution_time_ms)),
            metadata=metadata,
        )


# Bind ToolResult.error as classmethod to ensure ToolResult.error(...) works despite field shadowing
setattr(ToolResult, "error", classmethod(ToolResult.err.__func__))
