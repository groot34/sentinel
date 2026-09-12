"""Built-in metrics inspection tools for Sentinel 2.0.

Provides deterministic querying and anomaly detection over CSV metrics data.
"""

from __future__ import annotations

from pathlib import Path
from typing import List, Optional
from pydantic import BaseModel, Field

from agents.metric_tools import (
    calculate_summary as _calculate_summary,
    detect_drops as _detect_drops,
    detect_spikes as _detect_spikes,
    list_metrics as _list_metrics,
    load_metrics as _load_metrics,
)
from core.tools.base import BaseTool
from core.tools.context import ToolExecutionContext
from core.tools.models import (
    MetricAnomalyObservation,
    MetricSampleObservation,
    MetricSeriesObservation,
)
from core.tools.result import ToolResult


# ============================================================================
# 1. MetricSeriesQueryTool
# ============================================================================

class MetricSeriesQueryInput(BaseModel):
    """Input parameters for querying a metric time series."""

    metric_name: str = Field(..., min_length=1, description="Name of the metric column in CSV")
    relative_path: str = Field(default="metrics/metrics.csv", description="Relative path to CSV metrics file")


class MetricSeriesQueryTool(BaseTool[MetricSeriesQueryInput]):
    """Query a single metric series and compute summary statistics."""

    name = "query_metric_series"
    description = "Extract time-series samples and summary statistics for a specific metric."
    input_schema = MetricSeriesQueryInput

    def run(self, params: MetricSeriesQueryInput, context: ToolExecutionContext) -> ToolResult:
        metrics_path = context.resolve_path(params.relative_path)
        if not metrics_path.is_file():
            return ToolResult.fail(
                self.name,
                f"Metrics file not found at '{params.relative_path}'",
            )

        try:
            table = _load_metrics(metrics_path)
        except Exception as exc:
            return ToolResult.fail(
                self.name,
                f"Failed to parse metrics CSV at '{params.relative_path}': {exc}",
            )

        available_metrics = _list_metrics(table)
        if params.metric_name not in table.columns:
            return ToolResult.fail(
                self.name,
                f"Metric '{params.metric_name}' not found. Available: {available_metrics}",
            )

        values = table.columns[params.metric_name]
        summary = _calculate_summary(values)

        samples = [
            MetricSampleObservation(
                timestamp=table.timestamps[i] if i < len(table.timestamps) else None,
                value=val,
                row_number=table.row_numbers[i] if i < len(table.row_numbers) else i + 2,
            )
            for i, val in enumerate(values)
        ]

        obs = MetricSeriesObservation(
            metric_name=params.metric_name,
            sample_count=int(summary.get("count", len(values))),
            min_value=float(summary.get("min", 0.0)),
            max_value=float(summary.get("max", 0.0)),
            mean_value=float(summary.get("mean", 0.0)),
            samples=samples,
        )

        return ToolResult.ok(self.name, data=obs)


# ============================================================================
# 2. MetricAnomalyDetectTool
# ============================================================================

class MetricAnomalyDetectInput(BaseModel):
    """Input parameters for detecting anomalies across metrics."""

    metric_name: Optional[str] = Field(default=None, description="Optional metric to filter analysis to")
    relative_path: str = Field(default="metrics/metrics.csv", description="Relative path to CSV metrics file")
    z_threshold: float = Field(default=2.0, ge=0.5, description="Z-score threshold for spike/drop detection")


class MetricAnomalyDetectTool(BaseTool[MetricAnomalyDetectInput]):
    """Detect spikes and drops in metric time series using z-score against baseline."""

    name = "detect_metric_anomalies"
    description = "Detect statistically significant spikes and drops in metric values against baseline."
    input_schema = MetricAnomalyDetectInput

    def run(self, params: MetricAnomalyDetectInput, context: ToolExecutionContext) -> ToolResult:
        metrics_path = context.resolve_path(params.relative_path)
        if not metrics_path.is_file():
            return ToolResult.fail(
                self.name,
                f"Metrics file not found at '{params.relative_path}'",
            )

        try:
            table = _load_metrics(metrics_path)
        except Exception as exc:
            return ToolResult.fail(
                self.name,
                f"Failed to parse metrics CSV at '{params.relative_path}': {exc}",
            )

        available_metrics = _list_metrics(table)
        if params.metric_name and params.metric_name not in table.columns:
            return ToolResult.fail(
                self.name,
                f"Metric '{params.metric_name}' not found. Available: {available_metrics}",
            )

        findings = []
        findings.extend(_detect_spikes(table, metric=params.metric_name, z_threshold=params.z_threshold))
        findings.extend(_detect_drops(table, metric=params.metric_name, z_threshold=params.z_threshold))

        observations: List[MetricAnomalyObservation] = []
        for f in findings:
            baseline_mean = float(f.metadata.get("baseline_mean", 0.0))
            ratio = (f.value / baseline_mean) if baseline_mean != 0 else 0.0
            obs = MetricAnomalyObservation(
                metric_name=f.metric,
                anomaly_type=f.finding_type,
                row_number=f.row_number,
                timestamp=f.timestamp,
                value=f.value,
                baseline_mean=baseline_mean,
                ratio=ratio,
                description=f"{f.finding_type} detected: {f.metric}={f.value} vs baseline_mean={baseline_mean:.2f}",
            )
            observations.append(obs)

        # Empty matches must return SUCCESS with data=[]
        return ToolResult.ok(self.name, data=observations)
