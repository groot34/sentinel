"""Unit tests for deterministic metric analysis tools.

No LLM calls. Values and row references must match the original CSV.
"""

from pathlib import Path

import pytest

from agents.metric_tools import (
    calculate_summary,
    collect_candidate_evidence,
    compare_periods,
    detect_drops,
    detect_metric_correlations,
    detect_spikes,
    detect_threshold_violations,
    get_metric_window,
    list_metrics,
    load_metrics,
    pearson_correlation,
)


INCIDENTS = Path(__file__).parent.parent / "incidents"
INC01 = INCIDENTS / "inc_01_n_plus_one_query" / "metrics" / "metrics.csv"
INC03 = INCIDENTS / "inc_03_consumer_lag" / "metrics" / "metrics.csv"


def test_csv_loading():
    table = load_metrics(INC01)
    assert table.n == 9
    assert table.timestamps[0] == "2026-08-28T14:00:00Z"
    assert table.row_numbers[0] == 2
    assert "latency_p95_ms" in table.columns
    assert table.columns["active_db_connections"][0] == 2.0


def test_metric_discovery():
    names = list_metrics(load_metrics(INC01))
    assert names[0] == "latency_p95_ms"
    assert "db_queries_per_req" in names
    assert "timestamp" not in names


def test_timestamp_parsing():
    table = load_metrics(INC01)
    assert all(dt is not None for dt in table.timestamp_dts)
    assert table.timestamp_dts[0] < table.timestamp_dts[-1]


def test_metric_window_extraction():
    table = load_metrics(INC01)
    window = get_metric_window(
        table,
        "latency_p95_ms",
        start="2026-08-28T14:10:00Z",
        end="2026-08-28T14:12:00Z",
    )
    assert [p.value for p in window] == [4200.0, 9800.0, 10000.0]
    assert window[0].row_number == 7
    assert window[0].timestamp == "2026-08-28T14:10:00Z"


def test_summary_statistics():
    summary = calculate_summary([1.0, 2.0, 3.0, 4.0])
    assert summary["count"] == 4
    assert summary["min"] == 1.0
    assert summary["max"] == 4.0
    assert summary["mean"] == 2.5
    assert summary["median"] == 2.5
    assert summary["std"] == pytest.approx(1.1180339887, rel=1e-6)


def test_spike_detection():
    table = load_metrics(INC01)
    spikes = detect_spikes(table, metric="latency_p95_ms")
    assert spikes
    assert all(item.finding_type == "spike" for item in spikes)
    max_spike = max(spikes, key=lambda item: item.value)
    assert max_spike.value == 10000.0
    assert max_spike.timestamp in {"2026-08-28T14:12:00Z", "2026-08-28T14:15:00Z"}
    assert max_spike.row_number >= 2


def test_drop_detection():
    table = load_metrics(INC03)
    drops = detect_drops(table, metric="consumed_records_sec")
    assert drops
    assert all(item.finding_type == "drop" for item in drops)
    min_drop = min(drops, key=lambda item: item.value)
    assert min_drop.value == 8.0
    assert min_drop.row_number == 8


def test_threshold_detection():
    table = load_metrics(INC01)
    hits = detect_threshold_violations(table, "error_rate_pct", "ge", 50.0)
    assert hits
    assert all(item.value >= 50.0 for item in hits)
    assert hits[-1].value == 78.1


def test_period_comparison():
    table = load_metrics(INC01)
    result = compare_periods(table, "latency_p95_ms")
    assert result["baseline_n"] >= 1
    assert result["comparison_n"] >= 1
    assert result["comparison"]["mean"] > result["baseline"]["mean"]
    assert result["delta_mean"] > 0


def test_correlation_calculation():
    r = pearson_correlation([1.0, 2.0, 3.0], [2.0, 4.0, 6.0])
    assert r == pytest.approx(1.0)
    table = load_metrics(INC01)
    pairs = detect_metric_correlations(table, min_abs_r=0.8)
    assert pairs
    assert all(abs(item.value) >= 0.8 for item in pairs)
    assert all("~" in item.metric for item in pairs)


def test_deterministic_repeated_execution():
    table = load_metrics(INC01)
    first = collect_candidate_evidence(table)
    second = collect_candidate_evidence(load_metrics(INC01))
    assert first == second


def test_evidence_references_correspond_to_real_rows():
    table = load_metrics(INC01)
    lines = INC01.read_text(encoding="utf-8").splitlines()
    header = lines[0].split(",")
    for item in collect_candidate_evidence(table):
        if item["type"] == "correlation":
            assert item["reference"] == "metrics/metrics.csv"
            continue
        _, row_s = item["reference"].rsplit("row ", 1)
        row_no = int(row_s)
        cells = lines[row_no - 1].split(",")
        col = header.index(item["metric"])
        assert float(cells[col]) == item["value"]
        assert cells[0] == item["timestamp"]
