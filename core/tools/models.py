"""Structured observation models for Sentinel 2.0 tools.

Observations represent objective, deterministic tool inspection outputs.
They are strictly distinct from domain EvidenceItem models.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional
from pydantic import BaseModel, ConfigDict, Field


class BaseObservation(BaseModel):
    """Base model for all tool observation payloads."""

    model_config = ConfigDict(
        frozen=True,
        extra="forbid",
    )


# ============================================================================
# Log Observations
# ============================================================================

class LogLineObservation(BaseObservation):
    """Observation of a single original log line with parsed metadata."""

    line_number: int
    text: str
    timestamp: Optional[str] = None
    level: Optional[str] = None
    service: Optional[str] = None
    metadata: Dict[str, Any] = Field(default_factory=dict)


class LogBurstObservation(BaseObservation):
    """Observation of a high-density cluster of timestamped log events."""

    start_line: int
    event_count: int
    window_seconds: float
    start_timestamp: Optional[str] = None
    end_timestamp: Optional[str] = None
    sample_lines: List[LogLineObservation] = Field(default_factory=list)


# ============================================================================
# Metric Observations
# ============================================================================

class MetricSampleObservation(BaseObservation):
    """Observation of a single metric sample row."""

    timestamp: Optional[str] = None
    value: float
    row_number: int


class MetricSeriesObservation(BaseObservation):
    """Observation of a full metric time-series column with summary statistics."""

    metric_name: str
    sample_count: int
    min_value: float
    max_value: float
    mean_value: float
    samples: List[MetricSampleObservation] = Field(default_factory=list)


class MetricAnomalyObservation(BaseObservation):
    """Observation of an anomalous change (spike, drop, period change)."""

    metric_name: str
    anomaly_type: str  # "spike", "drop", "period_change", etc.
    row_number: int
    timestamp: Optional[str] = None
    value: float
    baseline_mean: float
    ratio: float
    description: str = ""


# ============================================================================
# Source Code & Git Observations
# ============================================================================

class DiffHunkObservation(BaseObservation):
    """Observation of a single hunk within a unified git diff."""

    old_start: int
    old_lines: int
    new_start: int
    new_lines: int
    header: str = ""
    lines: List[str] = Field(default_factory=list)


class DiffFileObservation(BaseObservation):
    """Observation of file-level changes in a git diff patch."""

    file_path: str
    change_type: str  # "modified", "added", "deleted"
    added_lines: int
    removed_lines: int
    hunks: List[DiffHunkObservation] = Field(default_factory=list)


class SourceMatchObservation(BaseObservation):
    """Observation of a pattern or symbol match in source code."""

    file_path: str
    line_number: int
    line_text: str
    pattern: str
