from __future__ import annotations

import ast
import hashlib
import os
import tempfile
from pathlib import Path

import pytest

from agents.verification_tools import (
    CHECK_FAIL,
    CHECK_INCONCLUSIVE,
    CHECK_PASS,
    CheckResult,
    contains_db_call_inside_loop,
    find_acquire_without_release,
    find_backoff_zero,
    find_drop_index_actual_change,
    find_retry_constants,
    find_unbounded_mutable_class_level,
    iter_service_py_files,
    max_metric_value,
    metric_spike_order,
    read_application_log,
    read_metrics_rows,
    run_dispatch_check,
)

REPO = Path(__file__).parent.parent
INCIDENTS = REPO / "incidents"


def _inc(name: str) -> Path:
    return INCIDENTS / name


INC_01 = _inc("inc_01_n_plus_one_query")
INC_04 = _inc("inc_04_memory_leak")
INC_07 = _inc("inc_07_retry_storm")
INC_10 = _inc("inc_10_multi_symptom_cascade")


def test_deterministic_behaviour_same_input(tmp_path: Path):
    for inc in (INC_01, INC_04, INC_07, INC_10):
        logs = read_application_log(inc)
        headers, rows = read_metrics_rows(inc)
        assert isinstance(logs, list)
        assert isinstance(headers, list)
        assert isinstance(rows, list)
        for sf in iter_service_py_files(inc):
            src = "\n".join(sf.read_text(encoding="utf-8").splitlines())
            # run detectors twice
            a = contains_db_call_inside_loop(src)
            b = contains_db_call_inside_loop(src)
            assert a == b


def test_same_input_same_result(tmp_path: Path):
    # dispatch with identical params
    logs_bundle = {"evidence": []}
    metrics_bundle = {"evidence": []}
    code_bundle = {"evidence": []}
    ref_ev: list[dict] = []
    r1 = run_dispatch_check(
        "CHK-001", INC_01, "query inside loop", "check loop query",
        {"logs": logs_bundle, "metrics": metrics_bundle, "code": code_bundle},
        ref_ev,
    )
    r2 = run_dispatch_check(
        "CHK-001", INC_01, "query inside loop", "check loop query",
        {"logs": logs_bundle, "metrics": metrics_bundle, "code": code_bundle},
        ref_ev,
    )
    assert r1.result == r2.result
    assert r1.description == r2.description


def test_readonly_behaviour(tmp_path: Path):
    """Execute all public detectors and dispatch checks across incidents; then
    compare SHA256 of all incident files to ensure zero mutation."""
    def snapshot(base: Path) -> dict[str, str]:
        out = {}
        for p in sorted(base.rglob("*")):
            if p.is_file():
                h = hashlib.sha256()
                with open(p, "rb") as f:
                    for chunk in iter(lambda: f.read(65536), b""):
                        h.update(chunk)
                out[str(p.relative_to(base))] = h.hexdigest()
        return out

    for inc in (INC_01, INC_04, INC_07, INC_10):
        before = snapshot(inc)
        # exercise tooling
        read_application_log(inc)
        read_metrics_rows(inc)
        metric_spike_order(read_metrics_rows(inc)[1], ["active_db_conns", "error_rate_pct"])
        for sf in iter_service_py_files(inc):
            src = sf.read_text(encoding="utf-8")
            contains_db_call_inside_loop(src)
            find_retry_constants(src)
            find_backoff_zero(src)
            find_unbounded_mutable_class_level(src)
            find_acquire_without_release(src)
        find_drop_index_actual_change(inc)
        run_dispatch_check(
            "CHK-001", inc, "query", "query inside loop",
            {"logs": {}, "metrics": {}, "code": {}}, [],
        )
        after = snapshot(inc)
        assert before == after, f"Files modified during verification tools execution in {inc}"


def test_real_source_references(tmp_path: Path):
    # For incidents with known checkable sources, ensure returned reference points to existing file
    result = run_dispatch_check(
        "CHK-001", INC_01, "DB query inside loop",
        "Verify query inside loop (N+1) via AST.",
        {"logs": {}, "metrics": {}, "code": {}}, [],
    )
    assert result.result == CHECK_PASS
    assert result.reference
    # reference should point to an existing service file
    first = result.reference.split(";")[0].strip()
    file_part = first.rsplit(":", 1)[0]
    assert (INC_01 / file_part).is_file()


def test_real_metric_references(tmp_path: Path):
    result = run_dispatch_check(
        "CHK-001", INC_10, "metric spike correlation",
        "Order metric spikes (correlation, before/after).",
        {"logs": {}, "metrics": {}, "code": {}}, [],
    )
    assert result.result in (CHECK_PASS, CHECK_INCONCLUSIVE)
    if result.reference:
        assert result.reference.startswith("metrics/metrics.csv")


def test_real_log_references(tmp_path: Path):
    result = run_dispatch_check(
        "CHK-001", INC_04, "count log errors matching",
        "Log pattern count for timeout/ERROR.",
        {"logs": {}, "metrics": {}, "code": {}}, [],
    )
    assert result.result in (CHECK_PASS, CHECK_INCONCLUSIVE)
    if result.reference:
        assert result.reference.startswith("logs/application.log")


def test_safe_handling_unsupported(tmp_path: Path):
    r = run_dispatch_check(
        "CHK-001", INC_01, "completely bogus claim",
        "unmappable-plan-wording-nothing-supported: flobbenator flobbenator 3000.",
        {"logs": {}, "metrics": {}, "code": {}}, [],
    )
    assert r.result in (CHECK_PASS, CHECK_INCONCLUSIVE)


def test_no_shell_execution_from_llm(tmp_path: Path):
    src = (REPO / "agents" / "verification_tools.py").read_text(encoding="utf-8")
    # No shell execution imports allowed
    for bad in ("import subprocess", "from subprocess", "os.system(", "popen(", "exec(", "eval("):
        # allow 'eval' only if not actually used with shell; strict check:
        assert bad not in src, f"Potential shell execution found: {bad}"


def test_no_incident_specific_hardcoded():
    src = (REPO / "agents" / "verification_tools.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    incident_ids = [
        "inc_01_n_plus_one_query", "inc_02_short_cache_ttl", "inc_03_downstream_service_latency",
        "inc_04_unbounded_collection_memory_leak", "inc_05_blocking_op_on_event_loop",
        "inc_06_connection_pool_exhaustion", "inc_07_retry_storm",
        "inc_08_slow_external_timeout", "inc_09_missing_index",
        "inc_10_multi_symptom_cascade",
    ]
    for node in ast.walk(tree):
        if isinstance(node, (ast.If, ast.cmpop)):
            try:
                text = ast.unparse(node)
            except Exception:
                text = ""
            for iid in incident_ids:
                assert iid not in text, f"Hardcoded incident id in: {text}"


# --- individual tool unit checks ---

def test_drop_index_inc_10_unchanged_sql(tmp_path: Path):
    r = find_drop_index_actual_change(INC_10)
    assert r is not None
    # net SQL drops unchanged: removed 1, added 1
    assert r["net_sql_drops"] == 0


def test_db_loop_inc_01(tmp_path: Path):
    for sf in iter_service_py_files(INC_01):
        src = sf.read_text(encoding="utf-8")
        res = contains_db_call_inside_loop(src)
        if res and res.get("found"):
            assert len(res["hits"]) >= 1
            return
    pytest.fail("Expected query-in-loop pattern in inc_01")


def test_retry_inc_07(tmp_path: Path):
    for sf in iter_service_py_files(INC_07):
        src = sf.read_text(encoding="utf-8")
        r1 = find_retry_constants(src)
        r2 = find_backoff_zero(src)
        if r1 and r1.get("hits"):
            # assert high MAX_RETRIES present
            highs = [h for h in r1["hits"] if h["value"] >= 5]
            if highs:
                assert True
                return
        if r2 and r2.get("hits"):
            return
    pytest.fail("Expected retry storm indicators in inc_07")


def test_unbounded_collection_inc_04(tmp_path: Path):
    for sf in iter_service_py_files(INC_04):
        src = sf.read_text(encoding="utf-8")
        r = find_unbounded_mutable_class_level(src)
        if r and r.get("hits"):
            return
    pytest.fail("Expected class-level mutable collection in inc_04")


def test_acquire_without_release_inc_10(tmp_path: Path):
    for sf in iter_service_py_files(INC_10):
        src = sf.read_text(encoding="utf-8")
        r = find_acquire_without_release(src)
        if r and r.get("has_pattern"):
            return
    pytest.fail("Expected acquire without release in inc_10 service/app.py")
