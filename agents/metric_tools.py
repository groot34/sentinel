"""Deterministic metric analysis tools for the Sentinel Metrics Agent.

These functions perform mechanical extraction only. They never call an LLM.

CSV contract observed in this repository:
- Path: metrics/metrics.csv
- Header row includes a `timestamp` column (ISO-8601, ...Z)
- Remaining columns are numeric (wide format: one timestamp, many metrics)
- Column names differ across incidents
- Series are short (typically 5–9 samples) with no missing values in the dataset

Row references use 1-indexed file line numbers including the header:
`metrics/metrics.csv:row 2` is the first data row.

Anomaly method (documented, reproducible):
- Baseline window = the earliest ceil(n / 3) samples (at least 1, and fewer than n).
- Sample mean and population standard deviation (statistics.pstdev) of the baseline.
- Spike: z >= 2.0, where z = (x - mean) / std if std > 0; if std == 0, any x > mean.
- Drop: z <= -2.0; if std == 0, any x < mean.
- Percent-like columns (`pct` or `percent` in the name): values >= 90 are threshold violations.
- Pearson correlation is reported for metric pairs with |r| >= 0.80 and n >= 3.
"""

from __future__ import annotations

import csv
import math
import statistics
from dataclasses import dataclass, field
from datetime import datetime, timezone
from math import ceil
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple, Union


DEFAULT_RELATIVE_METRICS = "metrics/metrics.csv"
SPIKE_Z = 2.0
DROP_Z = -2.0
CORR_MIN = 0.80
PCT_THRESHOLD = 90.0
MAX_CANDIDATES = 24


@dataclass
class MetricPoint:
    """One timestamped sample of a single metric."""

    row_number: int
    timestamp: str
    timestamp_dt: Optional[datetime]
    metric: str
    value: float


@dataclass
class MetricTable:
    """Loaded wide-format metrics CSV."""

    timestamps: List[str]
    timestamp_dts: List[Optional[datetime]]
    row_numbers: List[int]
    columns: Dict[str, List[float]]
    relative_path: str = DEFAULT_RELATIVE_METRICS

    @property
    def n(self) -> int:
        return len(self.timestamps)


@dataclass
class MetricFinding:
    """A deterministic finding grounded to a real CSV cell or series pair."""

    row_number: int
    timestamp: str
    metric: str
    value: float
    finding_type: str
    metadata: Dict[str, Any] = field(default_factory=dict)

    def reference(self, relative_path: str = DEFAULT_RELATIVE_METRICS) -> str:
        if self.row_number <= 0:
            return relative_path
        return f"{relative_path}:row {self.row_number}"

    def to_candidate(self, relative_path: str = DEFAULT_RELATIVE_METRICS) -> Dict[str, Any]:
        return {
            "source": "metrics",
            "reference": self.reference(relative_path),
            "timestamp": self.timestamp,
            "metric": self.metric,
            "value": self.value,
            "type": self.finding_type,
            "metadata": self.metadata,
        }


def _parse_timestamp(value: str) -> Optional[datetime]:
    text = (value or "").strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except ValueError:
        try:
            dt = datetime.strptime(value.strip(), "%Y-%m-%dT%H:%M:%SZ")
            return dt.replace(tzinfo=timezone.utc)
        except ValueError:
            return None


def load_metrics(source: Union[str, Path]) -> MetricTable:
    """Load and validate a metrics CSV. Raises ValueError on unusable files."""
    path = Path(source)
    if not path.exists() or not path.is_file():
        raise FileNotFoundError(f"Metrics CSV not found: {source}")

    with path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        if not reader.fieldnames:
            raise ValueError("Metrics CSV has no header.")
        fields = [name.strip() for name in reader.fieldnames]
        if "timestamp" not in fields:
            raise ValueError("Metrics CSV missing required 'timestamp' column.")

        metric_names = [name for name in fields if name != "timestamp"]
        timestamps: List[str] = []
        timestamp_dts: List[Optional[datetime]] = []
        row_numbers: List[int] = []
        columns: Dict[str, List[float]] = {name: [] for name in metric_names}

        for line_no, row in enumerate(reader, start=2):
            ts = (row.get("timestamp") or "").strip()
            timestamps.append(ts)
            timestamp_dts.append(_parse_timestamp(ts))
            row_numbers.append(line_no)
            for name in metric_names:
                raw = (row.get(name) or "").strip()
                if raw == "":
                    raise ValueError(f"Missing value for {name} at file row {line_no}.")
                try:
                    columns[name].append(float(raw))
                except ValueError as exc:
                    raise ValueError(
                        f"Non-numeric value for {name} at file row {line_no}: {raw}"
                    ) from exc

    return MetricTable(
        timestamps=timestamps,
        timestamp_dts=timestamp_dts,
        row_numbers=row_numbers,
        columns=columns,
        relative_path=DEFAULT_RELATIVE_METRICS,
    )


def list_metrics(table: MetricTable) -> List[str]:
    """Return available metric column names in CSV order."""
    return list(table.columns.keys())


def get_metric_window(
    table: MetricTable,
    metric: str,
    start: Optional[str] = None,
    end: Optional[str] = None,
) -> List[MetricPoint]:
    """Return samples for a metric, optionally filtered by inclusive timestamps."""
    if metric not in table.columns:
        raise KeyError(f"Unknown metric: {metric}")
    start_dt = _parse_timestamp(start) if start else None
    end_dt = _parse_timestamp(end) if end else None
    points: List[MetricPoint] = []
    for i, value in enumerate(table.columns[metric]):
        ts_dt = table.timestamp_dts[i]
        if start_dt is not None and (ts_dt is None or ts_dt < start_dt):
            continue
        if end_dt is not None and (ts_dt is None or ts_dt > end_dt):
            continue
        points.append(
            MetricPoint(
                row_number=table.row_numbers[i],
                timestamp=table.timestamps[i],
                timestamp_dt=ts_dt,
                metric=metric,
                value=value,
            )
        )
    return points


def calculate_summary(values: Sequence[float]) -> Dict[str, float]:
    """Deterministic summary statistics for a numeric series."""
    if not values:
        return {"count": 0.0}
    data = [float(v) for v in values]
    result: Dict[str, float] = {
        "count": float(len(data)),
        "min": min(data),
        "max": max(data),
        "mean": statistics.fmean(data),
        "median": float(statistics.median(data)),
    }
    result["std"] = statistics.pstdev(data) if len(data) >= 1 else 0.0
    return result


def _baseline_count(n: int) -> int:
    if n <= 1:
        return n
    count = max(1, ceil(n / 3))
    if count >= n:
        return max(1, n // 2)
    return count


def _zscore(value: float, mean: float, std: float) -> Optional[float]:
    if std > 0:
        return (value - mean) / std
    if value == mean:
        return 0.0
    return None


def detect_spikes(
    table: MetricTable,
    metric: Optional[str] = None,
    z_threshold: float = SPIKE_Z,
) -> List[MetricFinding]:
    """Detect unusually high values versus the early baseline window."""
    metrics = [metric] if metric else list_metrics(table)
    findings: List[MetricFinding] = []
    for name in metrics:
        findings.extend(_detect_direction(table, name, direction="spike", z_threshold=z_threshold))
    return findings


def detect_drops(
    table: MetricTable,
    metric: Optional[str] = None,
    z_threshold: float = abs(DROP_Z),
) -> List[MetricFinding]:
    """Detect unusually low values versus the early baseline window."""
    metrics = [metric] if metric else list_metrics(table)
    findings: List[MetricFinding] = []
    for name in metrics:
        findings.extend(_detect_direction(table, name, direction="drop", z_threshold=z_threshold))
    return findings


def _detect_direction(
    table: MetricTable,
    metric: str,
    direction: str,
    z_threshold: float,
) -> List[MetricFinding]:
    values = table.columns[metric]
    n = len(values)
    if n == 0:
        return []
    base_n = _baseline_count(n)
    baseline = values[:base_n]
    mean = statistics.fmean(baseline)
    std = statistics.pstdev(baseline)
    findings: List[MetricFinding] = []
    for i in range(base_n, n):
        value = values[i]
        if direction == "spike":
            hit = (std > 0 and (value - mean) / std >= z_threshold) or (std == 0 and value > mean)
        else:
            hit = (std > 0 and (value - mean) / std <= -z_threshold) or (std == 0 and value < mean)
        if not hit:
            continue
        z = _zscore(value, mean, std)
        findings.append(
            MetricFinding(
                row_number=table.row_numbers[i],
                timestamp=table.timestamps[i],
                metric=metric,
                value=value,
                finding_type=direction,
                metadata={
                    "baseline_mean": mean,
                    "baseline_std": std,
                    "baseline_n": base_n,
                    "z_score": z,
                    "method": "baseline_zscore",
                    "z_threshold": z_threshold if direction == "spike" else -z_threshold,
                },
            )
        )
    return findings


def detect_threshold_violations(
    table: MetricTable,
    metric: str,
    op: str,
    bound: float,
) -> List[MetricFinding]:
    """Find values outside an explicitly supplied threshold.

    op: one of 'gt', 'ge', 'lt', 'le'.
    """
    if metric not in table.columns:
        raise KeyError(f"Unknown metric: {metric}")
    ops = {
        "gt": lambda v: v > bound,
        "ge": lambda v: v >= bound,
        "lt": lambda v: v < bound,
        "le": lambda v: v <= bound,
    }
    if op not in ops:
        raise ValueError(f"Unsupported threshold op: {op}")
    predicate = ops[op]
    findings: List[MetricFinding] = []
    for i, value in enumerate(table.columns[metric]):
        if predicate(value):
            findings.append(
                MetricFinding(
                    row_number=table.row_numbers[i],
                    timestamp=table.timestamps[i],
                    metric=metric,
                    value=value,
                    finding_type="threshold",
                    metadata={"op": op, "bound": bound},
                )
            )
    return findings


def compare_periods(
    table: MetricTable,
    metric: str,
    baseline_end_index: Optional[int] = None,
) -> Dict[str, Any]:
    """Compare an early baseline period against the remaining samples.

    baseline_end_index is an exclusive index into the series. Default: ceil(n/3).
    """
    values = table.columns[metric]
    n = len(values)
    if n == 0:
        return {"metric": metric, "baseline": calculate_summary([]), "comparison": calculate_summary([])}
    end = baseline_end_index if baseline_end_index is not None else _baseline_count(n)
    end = max(1, min(end, n - 1 if n > 1 else n))
    baseline = values[:end]
    comparison = values[end:] if end < n else []
    base_stats = calculate_summary(baseline)
    cmp_stats = calculate_summary(comparison)
    delta_mean = (cmp_stats.get("mean", 0.0) - base_stats.get("mean", 0.0)) if comparison else 0.0
    return {
        "metric": metric,
        "baseline_n": len(baseline),
        "comparison_n": len(comparison),
        "baseline": base_stats,
        "comparison": cmp_stats,
        "delta_mean": delta_mean,
        "baseline_end_timestamp": table.timestamps[end - 1] if baseline else "",
        "comparison_start_timestamp": table.timestamps[end] if comparison else "",
    }


def pearson_correlation(xs: Sequence[float], ys: Sequence[float]) -> Optional[float]:
    """Pearson r. Returns None if undefined (length mismatch, n<3, or zero variance)."""
    if len(xs) != len(ys) or len(xs) < 3:
        return None
    mean_x = statistics.fmean(xs)
    mean_y = statistics.fmean(ys)
    num = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys))
    den_x = math.sqrt(sum((x - mean_x) ** 2 for x in xs))
    den_y = math.sqrt(sum((y - mean_y) ** 2 for y in ys))
    if den_x == 0 or den_y == 0:
        return None
    return num / (den_x * den_y)


def detect_metric_correlations(
    table: MetricTable,
    min_abs_r: float = CORR_MIN,
) -> List[MetricFinding]:
    """Pearson correlations between metric pairs. Series-level, not a fabricated cell."""
    names = list_metrics(table)
    findings: List[MetricFinding] = []
    if table.n < 3:
        return findings
    window = f"{table.timestamps[0]}/{table.timestamps[-1]}" if table.timestamps else ""
    for i, left in enumerate(names):
        for right in names[i + 1 :]:
            r = pearson_correlation(table.columns[left], table.columns[right])
            if r is None or abs(r) < min_abs_r:
                continue
            findings.append(
                MetricFinding(
                    row_number=0,
                    timestamp=window,
                    metric=f"{left}~{right}",
                    value=r,
                    finding_type="correlation",
                    metadata={"metric_a": left, "metric_b": right, "pearson_r": r, "n": table.n},
                )
            )
    findings.sort(key=lambda item: abs(float(item.value)), reverse=True)
    return findings


def find_metric_anomalies(table: MetricTable) -> List[MetricFinding]:
    """Union of baseline spike and drop detections."""
    return detect_spikes(table) + detect_drops(table)


def _pct_like(name: str) -> bool:
    lowered = name.lower()
    return "pct" in lowered or "percent" in lowered


def _extreme_points(findings: List[MetricFinding], limit_per_metric: int = 1) -> List[MetricFinding]:
    """Keep the most extreme findings per metric (largest |z|, else largest |delta|)."""
    grouped: Dict[Tuple[str, str], List[MetricFinding]] = {}
    for item in findings:
        grouped.setdefault((item.metric, item.finding_type), []).append(item)

    selected: List[MetricFinding] = []
    for group in grouped.values():
        def score(item: MetricFinding) -> float:
            z = item.metadata.get("z_score")
            if z is not None:
                return abs(float(z))
            mean = float(item.metadata.get("baseline_mean", 0.0))
            return abs(item.value - mean)

        group.sort(key=score, reverse=True)
        selected.extend(group[:limit_per_metric])
    return selected


def collect_candidate_evidence(
    table: MetricTable,
    relative_path: str = DEFAULT_RELATIVE_METRICS,
    max_items: int = MAX_CANDIDATES,
) -> List[Dict[str, Any]]:
    """Build a concise, de-duplicated quantitative evidence bundle."""
    if table.n == 0:
        return []

    candidates: List[MetricFinding] = []
    candidates.extend(_extreme_points(detect_spikes(table)))
    candidates.extend(_extreme_points(detect_drops(table)))

    for name in list_metrics(table):
        if not _pct_like(name):
            continue
        violations = detect_threshold_violations(table, name, "ge", PCT_THRESHOLD)
        if violations:
            candidates.append(violations[-1])

        comparison = compare_periods(table, name)
        if comparison["comparison_n"] == 0:
            continue
        base_mean = comparison["baseline"].get("mean", 0.0)
        cmp_mean = comparison["comparison"].get("mean", 0.0)
        denom = max(abs(base_mean), 1e-9)
        if abs(cmp_mean - base_mean) / denom < 0.5 and abs(cmp_mean - base_mean) < 1.0:
            continue
        # Ground period_change to the real max/min cell in the comparison window.
        values = table.columns[name]
        start = comparison["baseline_n"]
        window_vals = values[start:]
        if not window_vals:
            continue
        if cmp_mean >= base_mean:
            offset = max(range(len(window_vals)), key=lambda j: window_vals[j])
            ftype_hint = "period_change"
        else:
            offset = min(range(len(window_vals)), key=lambda j: window_vals[j])
            ftype_hint = "period_change"
        idx = start + offset
        candidates.append(
            MetricFinding(
                row_number=table.row_numbers[idx],
                timestamp=table.timestamps[idx],
                metric=name,
                value=values[idx],
                finding_type=ftype_hint,
                metadata={
                    "baseline_mean": base_mean,
                    "comparison_mean": cmp_mean,
                    "delta_mean": comparison["delta_mean"],
                },
            )
        )

    for name in list_metrics(table):
        if _pct_like(name):
            continue
        comparison = compare_periods(table, name)
        if comparison["comparison_n"] == 0:
            continue
        base_mean = comparison["baseline"].get("mean", 0.0)
        cmp_mean = comparison["comparison"].get("mean", 0.0)
        denom = max(abs(base_mean), 1e-9)
        rel = abs(cmp_mean - base_mean) / denom
        if rel < 0.5 and abs(cmp_mean - base_mean) < 1.0:
            continue
        # Skip if a spike/drop already covers this metric with the same row.
        values = table.columns[name]
        start = comparison["baseline_n"]
        window_vals = values[start:]
        if cmp_mean >= base_mean:
            offset = max(range(len(window_vals)), key=lambda j: window_vals[j])
        else:
            offset = min(range(len(window_vals)), key=lambda j: window_vals[j])
        idx = start + offset
        already = any(
            c.metric == name and c.row_number == table.row_numbers[idx] for c in candidates
        )
        if already:
            continue
        candidates.append(
            MetricFinding(
                row_number=table.row_numbers[idx],
                timestamp=table.timestamps[idx],
                metric=name,
                value=values[idx],
                finding_type="period_change",
                metadata={
                    "baseline_mean": base_mean,
                    "comparison_mean": cmp_mean,
                    "delta_mean": comparison["delta_mean"],
                },
            )
        )

    correlations = detect_metric_correlations(table)[:5]
    candidates.extend(correlations)

    # De-duplicate by (reference, metric, type)
    seen: set[Tuple[str, str, str]] = set()
    unique: List[MetricFinding] = []
    for item in candidates:
        key = (item.reference(relative_path), item.metric, item.finding_type)
        if key in seen:
            continue
        seen.add(key)
        unique.append(item)

    def rank(item: MetricFinding) -> Tuple[int, float]:
        type_rank = {
            "spike": 0,
            "drop": 1,
            "threshold": 2,
            "period_change": 3,
            "anomaly": 4,
            "correlation": 5,
        }
        z = item.metadata.get("z_score")
        magnitude = abs(float(z)) if z is not None else abs(float(item.value))
        return (type_rank.get(item.finding_type, 9), -magnitude)

    unique.sort(key=rank)
    return [item.to_candidate(relative_path) for item in unique[:max_items]]
