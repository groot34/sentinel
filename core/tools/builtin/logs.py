"""Built-in log inspection tools for Sentinel 2.0.

Provides deterministic search, error extraction, and burst detection over log files.
"""

from __future__ import annotations

from pathlib import Path
from typing import List, Optional
from pydantic import BaseModel, Field

from agents.log_tools import (
    find_bursts as _find_bursts,
    find_error_lines as _find_error_lines,
    load_log_lines as _load_log_lines,
    search_log as _search_log,
)
from core.tools.base import BaseTool
from core.tools.context import ToolExecutionContext
from core.tools.models import LogBurstObservation, LogLineObservation
from core.tools.result import ToolResult


# ============================================================================
# 1. LogSearchTool
# ============================================================================

class LogSearchInput(BaseModel):
    """Input parameters for searching log files."""

    pattern: str = Field(..., min_length=1, description="Regex or text pattern to search for")
    relative_path: str = Field(default="logs/application.log", description="Relative path to log file within incident")
    ignore_case: bool = Field(default=False, description="Whether search should be case-insensitive")


class LogSearchTool(BaseTool[LogSearchInput]):
    """Deterministic log search tool matching patterns line by line."""

    name = "search_logs"
    description = "Search an application log file for string or regex pattern matches."
    input_schema = LogSearchInput

    def run(self, params: LogSearchInput, context: ToolExecutionContext) -> ToolResult:
        log_path = context.resolve_path(params.relative_path)
        if not log_path.is_file():
            return ToolResult.fail(
                self.name,
                f"Log file not found at '{params.relative_path}'",
            )

        matches = _search_log(log_path, pattern=params.pattern, ignore_case=params.ignore_case)
        observations = [
            LogLineObservation(
                line_number=m.line_number,
                text=m.text,
                timestamp=m.timestamp,
                level=m.level,
                service=m.service,
                metadata=m.metadata,
            )
            for m in matches
        ]
        # Legitimate empty matches MUST be SUCCESS with data=[]
        return ToolResult.ok(self.name, data=observations)


# ============================================================================
# 2. LogErrorExtractTool
# ============================================================================

class LogErrorExtractInput(BaseModel):
    """Input parameters for extracting error-level log lines."""

    relative_path: str = Field(default="logs/application.log", description="Relative path to log file within incident")


class LogErrorExtractTool(BaseTool[LogErrorExtractInput]):
    """Extracts lines containing ERROR, FATAL, CRITICAL, or unhandled tracebacks."""

    name = "extract_log_errors"
    description = "Extract error-level and exception log lines from an application log."
    input_schema = LogErrorExtractInput

    def run(self, params: LogErrorExtractInput, context: ToolExecutionContext) -> ToolResult:
        log_path = context.resolve_path(params.relative_path)
        if not log_path.is_file():
            return ToolResult.fail(
                self.name,
                f"Log file not found at '{params.relative_path}'",
            )

        matches = _find_error_lines(log_path)
        observations = [
            LogLineObservation(
                line_number=m.line_number,
                text=m.text,
                timestamp=m.timestamp,
                level=m.level,
                service=m.service,
                metadata=m.metadata,
            )
            for m in matches
        ]
        return ToolResult.ok(self.name, data=observations)


# ============================================================================
# 3. LogBurstExtractTool
# ============================================================================

class LogBurstExtractInput(BaseModel):
    """Input parameters for detecting high-density event bursts in logs."""

    relative_path: str = Field(default="logs/application.log", description="Relative path to log file within incident")
    window_seconds: int = Field(default=30, ge=1, description="Sliding time window in seconds")
    min_events: int = Field(default=3, ge=2, description="Minimum number of events to constitute a burst")


class LogBurstExtractTool(BaseTool[LogBurstExtractInput]):
    """Detects clusters of events occurring within a concentrated time window."""

    name = "detect_log_bursts"
    description = "Detect unusually dense clusters of timestamped errors or warnings in logs."
    input_schema = LogBurstExtractInput

    def run(self, params: LogBurstExtractInput, context: ToolExecutionContext) -> ToolResult:
        log_path = context.resolve_path(params.relative_path)
        if not log_path.is_file():
            return ToolResult.fail(
                self.name,
                f"Log file not found at '{params.relative_path}'",
            )

        burst_matches = _find_bursts(
            log_path,
            window_seconds=params.window_seconds,
            min_events=params.min_events,
        )

        observations: List[LogBurstObservation] = []
        for b in burst_matches:
            cluster_meta = b.metadata
            obs = LogBurstObservation(
                start_line=b.line_number,
                event_count=cluster_meta.get("count", 1),
                window_seconds=float(params.window_seconds),
                start_timestamp=b.timestamp,
                end_timestamp=cluster_meta.get("end_ts"),
                sample_lines=[
                    LogLineObservation(
                        line_number=b.line_number,
                        text=b.text,
                        timestamp=b.timestamp,
                        level=b.level,
                        service=b.service,
                    )
                ],
            )
            observations.append(obs)

        return ToolResult.ok(self.name, data=observations)
