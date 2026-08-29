"""Unit tests for deterministic log analysis tools.

No LLM calls. Line references must match original file contents.
"""

from pathlib import Path

from agents.log_tools import (
    collect_candidate_evidence,
    count_pattern,
    extract_context,
    extract_request_ids,
    extract_time_window,
    find_bursts,
    find_error_lines,
    find_warning_lines,
    load_log_lines,
    search_log,
)


SAMPLE_LOG = """2026-08-28T14:00:00Z INFO [order-service] Application started
2026-08-28T14:05:00Z WARN [order-service] Query pool latency exceeding threshold
2026-08-28T14:10:05Z ERROR [order-service] GET /api/v1/orders/bulk 504 GATEWAY_TIMEOUT
2026-08-28T14:10:30Z ERROR [order-service] Pool exhausted: 20/20 active connections
2026-08-28T14:10:31Z ERROR [order-service] TimeoutAcquiringConnection: pool empty request_id=req-abc-123
2026-08-28T14:11:00Z WARN [order-service] TCP retransmissions observed - distractor
2026-08-28T14:12:00Z INFO [order-service] Healthcheck still serving
"""


def test_search_finds_expected_lines():
    matches = search_log(SAMPLE_LOG, "Pool exhausted")
    assert len(matches) == 1
    assert matches[0].line_number == 4
    assert "Pool exhausted" in matches[0].text


def test_search_returns_no_false_line_references():
    matches = search_log(SAMPLE_LOG, "Pool exhausted")
    lines = SAMPLE_LOG.splitlines()
    for match in matches:
        assert 1 <= match.line_number <= len(lines)
        assert lines[match.line_number - 1] == match.text
        assert "Pool exhausted" in match.text

    missing = search_log(SAMPLE_LOG, "this-pattern-does-not-exist-xyz")
    assert missing == []


def test_error_extraction_works():
    errors = find_error_lines(SAMPLE_LOG)
    assert len(errors) == 3
    assert all(m.match_type == "error" for m in errors)
    assert errors[0].line_number == 3
    assert "504 GATEWAY_TIMEOUT" in errors[0].text


def test_warning_extraction_works():
    warnings = find_warning_lines(SAMPLE_LOG)
    assert len(warnings) == 2
    assert all(m.match_type == "warning" for m in warnings)
    assert warnings[0].line_number == 2
    assert "latency" in warnings[0].text


def test_pattern_counting_works():
    assert count_pattern(SAMPLE_LOG, r"ERROR") == 3
    assert count_pattern(SAMPLE_LOG, r"pool", ignore_case=True) == 3
    assert count_pattern(SAMPLE_LOG, r"does-not-exist") == 0


def test_context_extraction_works():
    context = extract_context(SAMPLE_LOG, line_number=4, before=1, after=1)
    assert [m.line_number for m in context] == [3, 4, 5]
    lines = SAMPLE_LOG.splitlines()
    assert context[1].text == lines[3]


def test_timestamp_filtering_works():
    window = extract_time_window(
        SAMPLE_LOG,
        start="2026-08-28T14:10:00Z",
        end="2026-08-28T14:10:31Z",
    )
    assert [m.line_number for m in window] == [3, 4, 5]
    assert all(m.timestamp is not None for m in window)


def test_request_id_extraction_works():
    matches = extract_request_ids(SAMPLE_LOG)
    assert len(matches) == 1
    assert matches[0].line_number == 5
    assert matches[0].metadata["request_ids"] == ["req-abc-123"]


def test_find_bursts_detects_dense_errors():
    bursts = find_bursts(SAMPLE_LOG, window_seconds=30, min_events=3)
    assert len(bursts) == 1
    assert bursts[0].match_type == "burst"
    assert bursts[0].metadata["event_count"] >= 3
    assert 3 in bursts[0].metadata["line_numbers"]


def test_evidence_references_point_to_real_lines(tmp_path: Path):
    log_path = tmp_path / "application.log"
    log_path.write_text(SAMPLE_LOG, encoding="utf-8")
    loaded = load_log_lines(log_path)
    candidates = collect_candidate_evidence(loaded, relative_path="logs/application.log")
    original = SAMPLE_LOG.splitlines()
    assert candidates
    for item in candidates:
        assert item["source"] == "logs"
        _, line_s = item["reference"].rsplit(":", 1)
        line_no = int(line_s)
        assert original[line_no - 1] == item["excerpt"]


def test_empty_log_handled():
    assert search_log("", "ERROR") == []
    assert find_error_lines("") == []
    assert collect_candidate_evidence("") == []
