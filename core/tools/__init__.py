"""Sentinel 2.0 Tool Abstraction Layer.

Defines the contract for first-class tools:
- BaseTool: generic abstract base class with Pydantic input validation
- ToolExecutionContext: trusted runtime context enforcing incident boundary
- ToolResult & ToolStatus: deterministic execution outcomes (SUCCESS, FAILED, ERROR)
- ToolRegistry: lifecycle manager with schema generation and invocation safety
- Typed observation models for structured findings without leaking domain evidence
"""

from __future__ import annotations

from core.tools.base import BaseTool
from core.tools.context import ToolExecutionContext
from core.tools.errors import (
    ToolError,
    ToolNotFoundError,
    ToolRegistrationError,
    ToolSafetyError,
    ToolValidationError,
)
from core.tools.models import (
    BaseObservation,
    DiffFileObservation,
    DiffHunkObservation,
    LogBurstObservation,
    LogLineObservation,
    MetricAnomalyObservation,
    MetricSampleObservation,
    MetricSeriesObservation,
    SourceMatchObservation,
)
from core.tools.registry import ToolRegistry
from core.tools.result import ToolResult, ToolStatus

__all__ = [
    # Base abstraction
    "BaseTool",
    "ToolExecutionContext",
    "ToolResult",
    "ToolStatus",
    "ToolRegistry",
    # Errors
    "ToolError",
    "ToolNotFoundError",
    "ToolRegistrationError",
    "ToolSafetyError",
    "ToolValidationError",
    # Observation models
    "BaseObservation",
    "LogLineObservation",
    "LogBurstObservation",
    "MetricSampleObservation",
    "MetricSeriesObservation",
    "MetricAnomalyObservation",
    "DiffHunkObservation",
    "DiffFileObservation",
    "SourceMatchObservation",
]
