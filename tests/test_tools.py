"""Tests for Sentinel 2.0 Tool Abstraction Layer (Mission 06).

Verifies:
1. ToolRegistry lifecycle (register, get, has, list, get_schemas, execute, duplicate protection).
2. Pydantic input validation (valid params, dict params, invalid types, missing fields).
3. ToolExecutionContext safety boundary:
   - caller cannot modify incident_root (frozen)
   - ../../ground_truth.md cannot be accessed
   - absolute paths cannot be accessed
   - symlink escapes cannot be accessed
   - benchmark files cannot be accessed
4. ToolResult invariants:
   - zero matches return SUCCESS with empty data []
   - missing files return FAILED
   - unexpected exceptions return ERROR
   - safety violations return ERROR
5. Built-in tools (logs, metrics, code) against synthetic & real incident fixtures.
6. Observation boundary (observations are BaseObservation, not domain EvidenceItem).
"""

from __future__ import annotations

import os
from dataclasses import FrozenInstanceError
from pathlib import Path
from typing import Any, Dict, List
from unittest.mock import patch
import pytest
from pydantic import BaseModel, Field

from core.tools.base import BaseTool
from core.tools.builtin.code import (
    GitDiffParseInput,
    GitDiffParseTool,
    SourceFileSearchInput,
    SourceFileSearchTool,
)
from core.tools.builtin.logs import (
    LogBurstExtractInput,
    LogBurstExtractTool,
    LogErrorExtractInput,
    LogErrorExtractTool,
    LogSearchInput,
    LogSearchTool,
)
from core.tools.builtin.metrics import (
    MetricAnomalyDetectInput,
    MetricAnomalyDetectTool,
    MetricSeriesQueryInput,
    MetricSeriesQueryTool,
)
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


# ============================================================================
# Dummy Tools for Unit Testing
# ============================================================================

class DummyInput(BaseModel):
    query: str = Field(..., min_length=2)
    limit: int = Field(default=10, ge=1)


class DummyTool(BaseTool[DummyInput]):
    name = "dummy_tool"
    description = "A dummy tool for unit testing."
    input_schema = DummyInput

    def run(self, params: DummyInput, context: ToolExecutionContext) -> ToolResult:
        if params.query == "throw_unexpected":
            raise RuntimeError("Simulated crash")
        if params.query == "empty":
            return ToolResult.ok(self.name, data=[])
        return ToolResult.ok(self.name, data={"echo": params.query, "limit": params.limit})


# ============================================================================
# 1. Registry Lifecycle & Operations
# ============================================================================

def test_registry_registration_and_discovery():
    registry = ToolRegistry()
    tool = DummyTool()
    registry.register(tool)

    assert registry.has("dummy_tool") is True
    assert registry.has("nonexistent") is False
    assert registry.get("dummy_tool") is tool
    assert registry.list() == ["dummy_tool"]


def test_registry_duplicate_registration_rejected():
    registry = ToolRegistry()
    tool = DummyTool()
    registry.register(tool)

    with pytest.raises(ToolRegistrationError, match="already registered"):
        registry.register(DummyTool())

    # Override permitted with override=True
    registry.register(DummyTool(), override=True)
    assert registry.has("dummy_tool") is True


def test_registry_invalid_tool_types():
    registry = ToolRegistry()
    with pytest.raises(ToolRegistrationError, match="Expected BaseTool instance"):
        registry.register("not_a_tool")  # type: ignore

    class UnnamedTool(BaseTool[DummyInput]):
        name = ""
        description = "No name"
        input_schema = DummyInput
        def run(self, params: DummyInput, context: ToolExecutionContext) -> ToolResult:
            return ToolResult.ok(self.name, data=None)

    with pytest.raises(ToolRegistrationError, match="Tool name cannot be empty"):
        registry.register(UnnamedTool())


def test_registry_get_unknown_raises():
    registry = ToolRegistry()
    with pytest.raises(ToolNotFoundError, match="not registered"):
        registry.get("unknown_tool")


def test_registry_schemas_deterministic():
    registry = ToolRegistry()
    registry.register(DummyTool())
    registry.register(LogSearchTool())

    schemas = registry.get_schemas()
    assert len(schemas) == 2
    # Alphabetical order: dummy_tool, search_logs
    assert schemas[0]["function"]["name"] == "dummy_tool"
    assert schemas[1]["function"]["name"] == "search_logs"
    assert "parameters" in schemas[0]["function"]


# ============================================================================
# 2. Input Validation Tests
# ============================================================================

def test_tool_input_validation(tmp_path: Path):
    context = ToolExecutionContext(incident_id="test_inc", incident_root=tmp_path)
    tool = DummyTool()

    # Valid Pydantic model
    res1 = tool.validate_input(DummyInput(query="hello", limit=5))
    assert res1.query == "hello"

    # Valid dict
    res2 = tool.validate_input({"query": "world", "limit": 2})
    assert res2.query == "world"
    assert res2.limit == 2

    # Invalid input (query too short)
    with pytest.raises(ToolValidationError):
        tool.validate_input({"query": "x"})

    # Invalid input (wrong type)
    with pytest.raises(ToolValidationError):
        tool.validate_input(["not", "a", "dict"])


def test_registry_execute_input_validation_failure(tmp_path: Path):
    registry = ToolRegistry()
    registry.register(DummyTool())
    context = ToolExecutionContext(incident_id="test_inc", incident_root=tmp_path)

    # Missing required query field -> FAILED
    result = registry.execute("dummy_tool", {"limit": 5}, context)
    assert result.status == ToolStatus.FAILED
    assert result.is_failed is True
    assert "validation" in result.error.lower() or "field required" in result.error.lower()


# ============================================================================
# 3. Context & Path Safety Boundary Tests
# ============================================================================

def test_context_validation_rules(tmp_path: Path):
    # Empty incident_id rejected
    with pytest.raises(ToolSafetyError, match="incident_id must be a non-empty string"):
        ToolExecutionContext(incident_id="", incident_root=tmp_path)

    # Nonexistent incident_root rejected
    with pytest.raises(ToolSafetyError, match="does not exist"):
        ToolExecutionContext(incident_id="test", incident_root=tmp_path / "nonexistent")

    # File as root rejected
    file_path = tmp_path / "file.txt"
    file_path.write_text("not a dir", encoding="utf-8")
    with pytest.raises(ToolSafetyError, match="not a directory"):
        ToolExecutionContext(incident_id="test", incident_root=file_path)


def test_tool_caller_cannot_modify_incident_root(tmp_path: Path):
    context = ToolExecutionContext(incident_id="test_inc", incident_root=tmp_path)
    other_path = tmp_path / "other"
    other_path.mkdir()

    with pytest.raises((FrozenInstanceError, AttributeError)):
        context.incident_root = other_path  # type: ignore


def test_path_safety_traversal_rejected(tmp_path: Path):
    context = ToolExecutionContext(incident_id="test_inc", incident_root=tmp_path)

    # Path traversal with ../../ground_truth.md
    with pytest.raises(ToolSafetyError, match="Path traversal detected"):
        context.resolve_path("../../ground_truth.md")

    with pytest.raises(ToolSafetyError, match="Path traversal detected"):
        context.resolve_path("logs/../../etc/passwd")


def test_path_safety_absolute_paths_rejected(tmp_path: Path):
    context = ToolExecutionContext(incident_id="test_inc", incident_root=tmp_path)

    # POSIX root
    with pytest.raises(ToolSafetyError, match="Absolute path rejected"):
        context.resolve_path("/etc/passwd")

    # Windows drive
    with pytest.raises(ToolSafetyError, match="Absolute path rejected"):
        context.resolve_path("C:\\Windows\\System32\\config")

    # UNC path
    with pytest.raises(ToolSafetyError, match="Absolute path rejected"):
        context.resolve_path("\\\\server\\share\\data")


def test_path_safety_benchmark_files_rejected(tmp_path: Path):
    context = ToolExecutionContext(incident_id="test_inc", incident_root=tmp_path)

    # Direct benchmark files
    forbidden = ["ground_truth.md", "results_baseline.csv", "baseline_summary.json"]
    for fn in forbidden:
        with pytest.raises(ToolSafetyError, match="forbidden benchmark file"):
            context.resolve_path(fn)

        with pytest.raises(ToolSafetyError, match="forbidden benchmark file"):
            context.resolve_path(f"sub/{fn}")


def test_path_safety_symlink_escape_rejected(tmp_path: Path):
    context = ToolExecutionContext(incident_id="test_inc", incident_root=tmp_path)

    # Simulated resolution that escapes incident_root
    outside = Path("C:/outside/target.txt")
    with patch.object(Path, "resolve", return_value=outside):
        with pytest.raises(ToolSafetyError, match="Path escapes trusted incident boundary"):
            context.resolve_path("symlink_to_outside.txt")


def test_path_safety_empty_and_whitespace(tmp_path: Path):
    context = ToolExecutionContext(incident_id="test_inc", incident_root=tmp_path)
    with pytest.raises(ToolSafetyError):
        context.resolve_path("")
    with pytest.raises(ToolSafetyError):
        context.resolve_path("   ")


# ============================================================================
# 4. Result Status Invariants (SUCCESS, FAILED, ERROR)
# ============================================================================

def test_invariant_empty_matches_is_success(tmp_path: Path):
    registry = ToolRegistry()
    registry.register(DummyTool())
    context = ToolExecutionContext(incident_id="test_inc", incident_root=tmp_path)

    res = registry.execute("dummy_tool", {"query": "empty"}, context)
    assert res.status == ToolStatus.SUCCESS
    assert res.is_success is True
    assert res.data == []
    assert res.error is None


def test_invariant_missing_file_is_failed(tmp_path: Path):
    registry = ToolRegistry()
    registry.register(LogSearchTool())
    context = ToolExecutionContext(incident_id="test_inc", incident_root=tmp_path)

    # Missing log file returns FAILED
    res = registry.execute(
        "search_logs",
        {"pattern": "ERROR", "relative_path": "logs/missing.log"},
        context,
    )
    assert res.status == ToolStatus.FAILED
    assert res.is_failed is True
    assert "not found" in res.error.lower()
    assert res.data is None


def test_invariant_unexpected_exception_is_error(tmp_path: Path):
    registry = ToolRegistry()
    registry.register(DummyTool())
    context = ToolExecutionContext(incident_id="test_inc", incident_root=tmp_path)

    res = registry.execute("dummy_tool", {"query": "throw_unexpected"}, context)
    assert res.status == ToolStatus.ERROR
    assert res.is_error is True
    assert "Simulated crash" in res.error


def test_invariant_safety_violation_is_error(tmp_path: Path):
    registry = ToolRegistry()
    registry.register(LogSearchTool())
    context = ToolExecutionContext(incident_id="test_inc", incident_root=tmp_path)

    # Trying to search forbidden benchmark file
    res = registry.execute(
        "search_logs",
        {"pattern": "root_cause", "relative_path": "ground_truth.md"},
        context,
    )
    assert res.status == ToolStatus.ERROR
    assert res.is_error is True
    assert "forbidden" in res.error.lower()


def test_invariant_unknown_tool_is_error(tmp_path: Path):
    registry = ToolRegistry()
    context = ToolExecutionContext(incident_id="test_inc", incident_root=tmp_path)

    res = registry.execute("unknown_tool", {}, context)
    assert res.status == ToolStatus.ERROR
    assert res.is_error is True
    assert "not found" in res.error.lower()


# ============================================================================
# 5. Built-in Tools: Logs, Metrics, Code
# ============================================================================

@pytest.fixture
def synthetic_incident(tmp_path: Path) -> ToolExecutionContext:
    """Fixture providing a minimal, well-formed synthetic incident directory."""
    # 1. Logs
    logs_dir = tmp_path / "logs"
    logs_dir.mkdir()
    app_log = logs_dir / "application.log"
    app_log.write_text(
        "2026-03-01T10:00:00Z INFO [app] Service booting up\n"
        "2026-03-01T10:00:01Z ERROR [db] Connection refused to pgsql:5432\n"
        "2026-03-01T10:00:02Z ERROR [db] Connection timeout retry 1\n"
        "2026-03-01T10:00:03Z ERROR [db] Connection timeout retry 2\n"
        "2026-03-01T10:00:04Z INFO [app] Healthcheck OK\n",
        encoding="utf-8",
    )

    # 2. Metrics
    metrics_dir = tmp_path / "metrics"
    metrics_dir.mkdir()
    metrics_csv = metrics_dir / "metrics.csv"
    metrics_csv.write_text(
        "timestamp,cpu_pct,db_connections\n"
        "2026-03-01T10:00:00Z,10.0,4\n"
        "2026-03-01T10:00:01Z,12.0,6\n"
        "2026-03-01T10:00:02Z,11.0,5\n"
        "2026-03-01T10:00:03Z,95.0,99\n"
        "2026-03-01T10:00:04Z,98.0,100\n",
        encoding="utf-8",
    )

    # 3. Code & Git Diff
    service_dir = tmp_path / "service"
    service_dir.mkdir()
    app_py = service_dir / "app.py"
    app_py.write_text(
        "import db\n\n"
        "def fetch_data():\n"
        "    for item_id in items:\n"
        "        db.query(f'SELECT * FROM items WHERE id={item_id}')\n",
        encoding="utf-8",
    )

    git_diff = tmp_path / "git_diff.patch"
    git_diff.write_text(
        "diff --git a/service/app.py b/service/app.py\n"
        "--- a/service/app.py\n"
        "+++ b/service/app.py\n"
        "@@ -1,3 +1,4 @@\n"
        " import db\n"
        "+# Added comment\n"
        " def fetch_data():\n",
        encoding="utf-8",
    )

    return ToolExecutionContext(incident_id="synthetic_01", incident_root=tmp_path)


def test_builtin_log_tools(synthetic_incident: ToolExecutionContext):
    # LogSearchTool - match found
    search_tool = LogSearchTool()
    res = search_tool.run(
        LogSearchInput(pattern="Connection refused", relative_path="logs/application.log"),
        synthetic_incident,
    )
    assert res.is_success is True
    assert len(res.data) == 1
    assert isinstance(res.data[0], LogLineObservation)
    assert res.data[0].level == "ERROR"

    # LogSearchTool - zero matches -> SUCCESS with []
    res_empty = search_tool.run(
        LogSearchInput(pattern="NONEXISTENT_STRING_PATTERN", relative_path="logs/application.log"),
        synthetic_incident,
    )
    assert res_empty.is_success is True
    assert res_empty.data == []

    # LogErrorExtractTool
    err_tool = LogErrorExtractTool()
    res_err = err_tool.run(
        LogErrorExtractInput(relative_path="logs/application.log"),
        synthetic_incident,
    )
    assert res_err.is_success is True
    assert len(res_err.data) == 3

    # LogBurstExtractTool
    burst_tool = LogBurstExtractTool()
    res_burst = burst_tool.run(
        LogBurstExtractInput(relative_path="logs/application.log", window_seconds=10, min_events=2),
        synthetic_incident,
    )
    assert res_burst.is_success is True
    assert len(res_burst.data) >= 1
    assert isinstance(res_burst.data[0], LogBurstObservation)


def test_builtin_metric_tools(synthetic_incident: ToolExecutionContext):
    # MetricSeriesQueryTool - valid query
    query_tool = MetricSeriesQueryTool()
    res = query_tool.run(
        MetricSeriesQueryInput(metric_name="cpu_pct", relative_path="metrics/metrics.csv"),
        synthetic_incident,
    )
    assert res.is_success is True
    series_obs: MetricSeriesObservation = res.data
    assert series_obs.metric_name == "cpu_pct"
    assert series_obs.sample_count == 5
    assert series_obs.max_value == 98.0
    assert len(series_obs.samples) == 5

    # MetricSeriesQueryTool - unknown metric -> FAILED
    res_fail = query_tool.run(
        MetricSeriesQueryInput(metric_name="unknown_col", relative_path="metrics/metrics.csv"),
        synthetic_incident,
    )
    assert res_fail.is_failed is True
    assert "unknown_col" in res_fail.error

    # MetricAnomalyDetectTool - detect spikes
    anomaly_tool = MetricAnomalyDetectTool()
    res_anom = anomaly_tool.run(
        MetricAnomalyDetectInput(relative_path="metrics/metrics.csv", z_threshold=2.0),
        synthetic_incident,
    )
    assert res_anom.is_success is True
    assert len(res_anom.data) >= 1
    assert isinstance(res_anom.data[0], MetricAnomalyObservation)

    # MetricAnomalyDetectTool - very high threshold -> zero anomalies -> SUCCESS with []
    res_high = anomaly_tool.run(
        MetricAnomalyDetectInput(relative_path="metrics/metrics.csv", z_threshold=99.0),
        synthetic_incident,
    )
    assert res_high.is_success is True
    assert res_high.data == []


def test_builtin_code_tools(synthetic_incident: ToolExecutionContext):
    # GitDiffParseTool
    diff_tool = GitDiffParseTool()
    res_diff = diff_tool.run(
        GitDiffParseInput(relative_path="git_diff.patch"),
        synthetic_incident,
    )
    assert res_diff.is_success is True
    assert len(res_diff.data) == 1
    diff_file: DiffFileObservation = res_diff.data[0]
    assert diff_file.file_path == "service/app.py"
    assert diff_file.added_lines == 1
    assert len(diff_file.hunks) == 1

    # SourceFileSearchTool - pattern found
    search_tool = SourceFileSearchTool()
    res_search = search_tool.run(
        SourceFileSearchInput(pattern="db.query", relative_dir="service"),
        synthetic_incident,
    )
    assert res_search.is_success is True
    assert len(res_search.data) == 1
    match_obs: SourceMatchObservation = res_search.data[0]
    assert match_obs.pattern == "db.query"
    assert "SELECT * FROM items" in match_obs.line_text

    # SourceFileSearchTool - zero matches -> SUCCESS with []
    res_empty = search_tool.run(
        SourceFileSearchInput(pattern="NOT_FOUND_AT_ALL", relative_dir="service"),
        synthetic_incident,
    )
    assert res_empty.is_success is True
    assert res_empty.data == []


# ============================================================================
# 6. Real Incident Verification (inc_01_n_plus_one_query)
# ============================================================================

def test_real_incident_execution():
    incident_dir = Path("incidents/inc_01_n_plus_one_query")
    if not incident_dir.exists():
        pytest.skip("inc_01_n_plus_one_query fixture directory not present")

    context = ToolExecutionContext(
        incident_id="inc_01_n_plus_one_query",
        incident_root=incident_dir,
    )
    registry = ToolRegistry()
    registry.register(LogSearchTool())
    registry.register(MetricSeriesQueryTool())
    registry.register(GitDiffParseTool())

    # 1. Log search in real incident
    log_res = registry.execute(
        "search_logs",
        {"pattern": "ERROR"},
        context,
    )
    assert log_res.is_success is True
    assert isinstance(log_res.data, list)

    # 2. Metric query in real incident
    # Let's query an existing column
    metric_res = registry.execute(
        "query_metric_series",
        {"metric_name": "active_db_connections"},
        context,
    )
    assert metric_res.is_success is True
    assert isinstance(metric_res.data, MetricSeriesObservation)

    # 3. Diff parse in real incident
    diff_res = registry.execute(
        "parse_git_diff",
        {"relative_path": "git_diff.patch"},
        context,
    )
    assert diff_res.is_success is True
    assert isinstance(diff_res.data, list)


# ============================================================================
# 7. Observation Boundary Invariant
# ============================================================================

def test_observation_boundary_is_maintained():
    """Verify tool observations are strictly BaseObservation and never domain EvidenceItem."""
    obs = LogLineObservation(line_number=1, text="error", level="ERROR")
    assert isinstance(obs, BaseObservation)

    # Explicit check: observation is not a domain EvidenceItem
    from core.domain.models import LogEvidenceItem, MetricEvidenceItem
    assert not isinstance(obs, LogEvidenceItem)

    metric_obs = MetricSeriesObservation(
        metric_name="cpu", sample_count=1, min_value=1.0, max_value=1.0, mean_value=1.0
    )
    assert not isinstance(metric_obs, MetricEvidenceItem)
