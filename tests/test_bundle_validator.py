"""Tests for the incident bundle contract and deterministic preflight validation.

Covers, per Mission 18:
1. Valid existing incident bundles (the 10 canonical fixtures).
2. Missing required files / structures.
3. Empty or malformed files.
4. Legitimate partial-evidence bundles.
5. Invalid incident IDs and path traversal / escape attempts.
6. Clear, actionable validation errors naming the affected file.
7. Validation running before any LLM call.
8. No source-file mutation and no access to evaluation-only files.

All LLM calls are mocked or never reached — no real Groq API calls.
"""

from __future__ import annotations

import builtins
import json
from pathlib import Path
from typing import Any, Dict, List
from unittest.mock import patch

import pytest
from starlette.testclient import TestClient

from agents.bundle_validator import (
    EVIDENCE_ENTRIES,
    InvalidIncidentBundleError,
    require_valid_incident_bundle,
    validate_incident_bundle,
)
from agents.orchestrator import IncidentOrchestrator
from api.app import create_app
from core.persistence import FilesystemRepository


REPO = Path(__file__).parent.parent
INCIDENTS = REPO / "incidents"

EXPECTED_INCIDENTS = [
    "inc_01_n_plus_one_query",
    "inc_02_cache_stampede",
    "inc_03_consumer_lag",
    "inc_04_memory_leak",
    "inc_05_race_condition",
    "inc_06_connection_exhaustion",
    "inc_07_retry_storm",
    "inc_08_cascading_timeout",
    "inc_09_dropped_index",
    "inc_10_multi_symptom_cascade",
]

EXPECTED_EVIDENCE = ("logs/application.log", "metrics/metrics.csv", "git_diff.patch", "service")

VALID_LOG = "2026-08-28T14:00:00Z INFO [order-service] [main] Application initialized.\n"
VALID_METRICS = (
    "timestamp,latency_p95_ms,error_rate_pct\n"
    "2026-08-28T14:00:00Z,120.0,0.5\n"
    "2026-08-28T14:00:01Z,480.0,3.5\n"
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _bundle(tmp_path: Path, name: str = "inc_test_bundle") -> Path:
    bundle = tmp_path / name
    bundle.mkdir()
    return bundle


def _add_logs(bundle: Path, text: str = VALID_LOG) -> None:
    (bundle / "logs").mkdir(exist_ok=True)
    (bundle / "logs" / "application.log").write_text(text, encoding="utf-8")


def _add_metrics(bundle: Path, text: str = VALID_METRICS) -> None:
    (bundle / "metrics").mkdir(exist_ok=True)
    (bundle / "metrics" / "metrics.csv").write_text(text, encoding="utf-8")


def _error_codes(result) -> List[str]:
    return [issue.code for issue in result.errors]


def _snapshot(root: Path) -> Dict[str, Any]:
    snapshot: Dict[str, Any] = {}
    for path in sorted(root.rglob("*")):
        if path.is_file():
            snapshot[str(path.relative_to(root))] = path.read_bytes()
    return snapshot


def _can_symlink() -> bool:
    import tempfile

    try:
        with tempfile.TemporaryDirectory() as td:
            src = Path(td) / "src"
            src.mkdir()
            dst = Path(td) / "dst"
            dst.symlink_to(src, target_is_directory=True)
            return True
    except (OSError, NotImplementedError):
        return False


class _RecordingLLMClient:
    """Fails loudly if any LLM surface is touched during preflight."""

    def __init__(self) -> None:
        self.calls: List[str] = []

    def generate(self, *args: Any, **kwargs: Any):
        self.calls.append("generate")
        raise AssertionError("LLM must not be called when preflight rejects a bundle")

    def generate_structured(self, *args: Any, **kwargs: Any):
        self.calls.append("generate_structured")
        raise AssertionError("LLM must not be called when preflight rejects a bundle")


# ===========================================================================
# 1. Valid existing incident bundles
# ===========================================================================

def test_exactly_ten_canonical_bundles_exist():
    assert len(EXPECTED_INCIDENTS) == 10


@pytest.mark.parametrize("incident_name", EXPECTED_INCIDENTS)
def test_canonical_fixture_bundle_is_valid(incident_name: str):
    result = validate_incident_bundle(INCIDENTS / incident_name)
    assert result.ok, result.error_messages()
    assert result.warnings == ()
    assert result.evidence_sources == EXPECTED_EVIDENCE


@pytest.mark.parametrize("incident_name", EXPECTED_INCIDENTS)
def test_require_valid_incident_bundle_accepts_canonical_fixtures(incident_name: str):
    result = require_valid_incident_bundle(
        INCIDENTS / incident_name, expected_id=incident_name
    )
    assert result.incident_id == incident_name
    assert result.ok


def test_canonical_fixture_bundle_full_contract_is_satisfied():
    """The 10 fixtures must satisfy every documented part of the contract."""
    for incident_name in EXPECTED_INCIDENTS:
        bundle = INCIDENTS / incident_name
        result = validate_incident_bundle(bundle)
        assert result.ok
        assert (bundle / "logs" / "application.log").is_file()
        assert (bundle / "metrics" / "metrics.csv").is_file()
        assert (bundle / "git_diff.patch").is_file()
        assert (bundle / "service").is_dir()
        assert (bundle / "docker-compose.yml").is_file()


# ===========================================================================
# 2. Missing required files / structures
# ===========================================================================

def test_missing_bundle_directory_raises_file_not_found(tmp_path: Path):
    with pytest.raises(FileNotFoundError):
        require_valid_incident_bundle(tmp_path / "inc_does_not_exist")


def test_bundle_path_that_is_a_file_raises_not_a_directory(tmp_path: Path):
    bogus = tmp_path / "inc_bundle_is_a_file"
    bogus.write_text("not a directory", encoding="utf-8")
    with pytest.raises(NotADirectoryError):
        require_valid_incident_bundle(bogus)


def test_logs_directory_without_log_file_is_rejected(tmp_path: Path):
    bundle = _bundle(tmp_path)
    (bundle / "logs").mkdir()
    result = validate_incident_bundle(bundle)
    assert not result.ok
    assert _error_codes(result) == ["required_file_missing"]
    assert result.errors[0].path == "logs/application.log"


def test_metrics_directory_without_metrics_file_is_rejected(tmp_path: Path):
    bundle = _bundle(tmp_path)
    (bundle / "metrics").mkdir()
    result = validate_incident_bundle(bundle)
    assert not result.ok
    assert _error_codes(result) == ["required_file_missing"]
    assert result.errors[0].path == "metrics/metrics.csv"


def test_both_declared_channels_missing_their_files_are_reported_together(tmp_path: Path):
    bundle = _bundle(tmp_path)
    (bundle / "logs").mkdir()
    (bundle / "metrics").mkdir()
    result = validate_incident_bundle(bundle)
    assert sorted(_error_codes(result)) == ["required_file_missing", "required_file_missing"]
    assert sorted(issue.path for issue in result.errors) == [
        "logs/application.log",
        "metrics/metrics.csv",
    ]


# ===========================================================================
# 3. Empty or malformed files
# ===========================================================================

def test_empty_metrics_csv_is_rejected(tmp_path: Path):
    bundle = _bundle(tmp_path)
    _add_logs(bundle)
    _add_metrics(bundle, "")
    result = validate_incident_bundle(bundle)
    assert not result.ok
    assert _error_codes(result) == ["metrics_csv_empty"]
    assert result.errors[0].path == "metrics/metrics.csv"


def test_metrics_csv_without_timestamp_column_is_rejected(tmp_path: Path):
    bundle = _bundle(tmp_path)
    _add_metrics(bundle, "latency_ms,error_rate\n120.0,0.5\n")
    result = validate_incident_bundle(bundle)
    assert not result.ok
    assert _error_codes(result) == ["metrics_csv_invalid"]
    assert "timestamp" in result.errors[0].detail


def test_metrics_csv_with_non_numeric_value_is_rejected(tmp_path: Path):
    bundle = _bundle(tmp_path)
    _add_metrics(
        bundle,
        "timestamp,latency_ms\n2026-08-28T14:00:00Z,120.0\n2026-08-28T14:00:01Z,not-a-number\n",
    )
    result = validate_incident_bundle(bundle)
    assert not result.ok
    assert _error_codes(result) == ["metrics_csv_invalid"]
    assert "row 3" in result.errors[0].detail


def test_metrics_csv_with_missing_value_is_rejected(tmp_path: Path):
    bundle = _bundle(tmp_path)
    _add_metrics(bundle, "timestamp,latency_ms\n2026-08-28T14:00:00Z,\n")
    result = validate_incident_bundle(bundle)
    assert not result.ok
    assert _error_codes(result) == ["metrics_csv_invalid"]
    assert "row 2" in result.errors[0].detail


def test_iso_utc_timestamps_are_accepted(tmp_path: Path):
    bundle = _bundle(tmp_path)
    _add_metrics(
        bundle,
        "timestamp,latency_ms\n"
        "2026-08-28T14:00:00Z,120.0\n"
        "2026-08-28T14:00:01Z,121.0\n",
    )
    result = validate_incident_bundle(bundle)
    assert result.ok, result.error_messages()


def test_fractional_second_utc_timestamps_are_accepted(tmp_path: Path):
    bundle = _bundle(tmp_path)
    _add_metrics(bundle, "timestamp,latency_ms\n2026-08-28T14:00:00.250Z,120.0\n")
    assert validate_incident_bundle(bundle).ok


def test_non_iso_timestamp_is_rejected(tmp_path: Path):
    bundle = _bundle(tmp_path)
    _add_metrics(bundle, "timestamp,latency_ms\nNOT-A-TIMESTAMP,120.0\n")
    result = validate_incident_bundle(bundle)
    assert not result.ok
    assert _error_codes(result) == ["metrics_timestamp_invalid"]
    assert result.errors[0].path == "metrics/metrics.csv"
    assert "row 2" in result.errors[0].detail
    assert "Z" in result.errors[0].detail


@pytest.mark.parametrize(
    "bad_timestamp",
    [
        "2026-08-28",                  # date only
        "2026-08-28 14:00:00Z",        # space instead of 'T'
        "2026-08-28T14:00Z",           # missing seconds
        "2026-08-28T14:00:00+00:00",   # explicit offset instead of 'Z'
        "2026-08-28T14:00:00",         # no timezone designator
        "2026-13-45T99:99:99Z",        # right shape, impossible values
    ],
)
def test_malformed_or_non_utc_timestamps_are_rejected(tmp_path: Path, bad_timestamp: str):
    bundle = _bundle(tmp_path)
    _add_metrics(bundle, f"timestamp,latency_ms\n{bad_timestamp},120.0\n")
    result = validate_incident_bundle(bundle)
    assert not result.ok
    assert _error_codes(result) == ["metrics_timestamp_invalid"]
    assert "row 2" in result.errors[0].detail


def test_invalid_timestamp_blocks_the_orchestrator_preflight(tmp_path: Path):
    bundle = _bundle(tmp_path, "inc_bad_timestamp")
    _add_metrics(bundle, "timestamp,latency_ms\nnot-a-date,120.0\n")

    orchestrator = IncidentOrchestrator(non_interactive=True)
    with pytest.raises(InvalidIncidentBundleError) as excinfo:
        orchestrator.investigate(bundle)

    assert "metrics/metrics.csv" in str(excinfo.value)
    assert "invalid timestamp" in str(excinfo.value)


def test_empty_log_file_is_a_warning_not_an_error(tmp_path: Path):
    bundle = _bundle(tmp_path)
    _add_logs(bundle, "")
    result = validate_incident_bundle(bundle)
    assert result.ok, result.error_messages()
    assert [issue.code for issue in result.warnings] == ["empty_log_file"]
    assert result.evidence_sources == ("logs/application.log",)


def test_logs_entry_that_is_a_file_is_rejected(tmp_path: Path):
    bundle = _bundle(tmp_path)
    (bundle / "logs").write_text("not a directory", encoding="utf-8")
    result = validate_incident_bundle(bundle)
    assert not result.ok
    assert _error_codes(result) == ["not_a_directory"]
    assert result.errors[0].path == "logs"


def test_metrics_csv_that_is_a_directory_is_rejected(tmp_path: Path):
    bundle = _bundle(tmp_path)
    (bundle / "metrics" / "metrics.csv").mkdir(parents=True)
    result = validate_incident_bundle(bundle)
    assert not result.ok
    assert _error_codes(result) == ["not_a_file"]
    assert result.errors[0].path == "metrics/metrics.csv"


def test_service_entry_that_is_a_file_is_rejected(tmp_path: Path):
    bundle = _bundle(tmp_path)
    (bundle / "service").write_text("not a directory", encoding="utf-8")
    result = validate_incident_bundle(bundle)
    assert not result.ok
    assert "not_a_directory" in _error_codes(result)


def test_docker_compose_that_is_a_directory_is_rejected(tmp_path: Path):
    bundle = _bundle(tmp_path)
    _add_logs(bundle)
    (bundle / "docker-compose.yml").mkdir()
    result = validate_incident_bundle(bundle)
    assert not result.ok
    assert _error_codes(result) == ["not_a_file"]
    assert result.errors[0].path == "docker-compose.yml"


# ===========================================================================
# 4. Legitimate partial-evidence bundles
# ===========================================================================

def test_logs_only_bundle_is_valid(tmp_path: Path):
    bundle = _bundle(tmp_path)
    _add_logs(bundle)
    result = validate_incident_bundle(bundle)
    assert result.ok, result.error_messages()
    assert result.evidence_sources == ("logs/application.log",)
    assert result.warnings == ()


def test_metrics_only_bundle_is_valid(tmp_path: Path):
    bundle = _bundle(tmp_path)
    _add_metrics(bundle)
    result = validate_incident_bundle(bundle)
    assert result.ok, result.error_messages()
    assert result.evidence_sources == ("metrics/metrics.csv",)


def test_code_source_only_bundle_is_valid(tmp_path: Path):
    bundle = _bundle(tmp_path)
    (bundle / "service").mkdir()
    (bundle / "service" / "app.py").write_text("x = 1\n", encoding="utf-8")
    result = validate_incident_bundle(bundle)
    assert result.ok, result.error_messages()
    assert result.evidence_sources == ("service",)


def test_diff_only_bundle_is_valid(tmp_path: Path):
    bundle = _bundle(tmp_path)
    (bundle / "git_diff.patch").write_text("diff --git a/x b/x\n", encoding="utf-8")
    result = validate_incident_bundle(bundle)
    assert result.ok, result.error_messages()
    assert result.evidence_sources == ("git_diff.patch",)


def test_evidence_free_bundle_is_valid_with_warning(tmp_path: Path):
    bundle = _bundle(tmp_path)
    result = validate_incident_bundle(bundle)
    assert result.ok, result.error_messages()
    assert result.evidence_sources == ()
    assert [issue.code for issue in result.warnings] == ["no_evidence_sources"]
    require_valid_incident_bundle(bundle)  # must not raise


def test_evidence_free_bundle_compatible_with_existing_dispatch_fixtures(tmp_path: Path):
    """tests/test_async_dispatch.py uses bare empty incident directories."""
    bundle = tmp_path / "incidents" / "inc_01"
    bundle.mkdir(parents=True)
    require_valid_incident_bundle(bundle)


def test_non_standard_filenames_are_tolerated(tmp_path: Path):
    """tests/test_investigation_leasing.py uses logs.txt / metrics.json / diff.patch."""
    bundle = _bundle(tmp_path, "inc_01_sample")
    (bundle / "logs.txt").write_text("test logs", encoding="utf-8")
    (bundle / "metrics.json").write_text("{}", encoding="utf-8")
    (bundle / "diff.patch").write_text("", encoding="utf-8")
    result = validate_incident_bundle(bundle)
    assert result.ok, result.error_messages()
    assert result.evidence_sources == ()
    assert "no_evidence_sources" in [issue.code for issue in result.warnings]


def test_docker_compose_alone_is_not_evidence(tmp_path: Path):
    bundle = _bundle(tmp_path)
    (bundle / "docker-compose.yml").write_text("version: '3.8'\n", encoding="utf-8")
    result = validate_incident_bundle(bundle)
    assert result.ok
    assert result.evidence_sources == ()
    assert "docker-compose.yml" not in EVIDENCE_ENTRIES


# ===========================================================================
# 5. Invalid incident IDs and path traversal
# ===========================================================================

@pytest.mark.parametrize(
    "bad_name",
    ["inc 01", "inc.01", "-inc_01", "inc_01..inc_02", "inc_01!"],
)
def test_invalid_incident_directory_name_is_rejected(tmp_path: Path, bad_name: str):
    bundle = tmp_path / bad_name
    bundle.mkdir()
    result = validate_incident_bundle(bundle)
    assert not result.ok
    assert "invalid_incident_id" in _error_codes(result)


@pytest.mark.parametrize(
    "bad_name",
    ["inc 01", "inc.01", "-inc_01", "inc_01..inc_02", "inc_01!"],
)
def test_invalid_incident_directory_name_raises(tmp_path: Path, bad_name: str):
    bundle = tmp_path / bad_name
    bundle.mkdir()
    with pytest.raises(InvalidIncidentBundleError):
        require_valid_incident_bundle(bundle)


def test_valid_incident_directory_names_are_accepted(tmp_path: Path):
    for name in ("inc_01", "inc_01_n_plus_one_query", "INC-01", "a1"):
        bundle = _bundle(tmp_path, name)
        assert validate_incident_bundle(bundle).ok, name


def test_expected_id_mismatch_is_rejected():
    result = validate_incident_bundle(
        INCIDENTS / "inc_01_n_plus_one_query", expected_id="inc_02_cache_stampede"
    )
    assert not result.ok
    assert _error_codes(result) == ["incident_id_mismatch"]


def test_expected_id_matching_is_accepted():
    result = validate_incident_bundle(
        INCIDENTS / "inc_01_n_plus_one_query", expected_id="inc_01_n_plus_one_query"
    )
    assert result.ok


@pytest.mark.parametrize("traversal_id", ["../inc_01", "sub/inc_01", "..", "inc:01"])
def test_traversal_shaped_expected_id_never_matches(traversal_id: str):
    result = validate_incident_bundle(
        INCIDENTS / "inc_01_n_plus_one_query", expected_id=traversal_id
    )
    assert not result.ok
    assert "incident_id_mismatch" in _error_codes(result)


@pytest.mark.skipif(not _can_symlink(), reason="Symlinks are not supported on this platform")
def test_symlinked_bundle_entry_escaping_root_is_rejected(tmp_path: Path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "metrics.csv").write_text(VALID_METRICS, encoding="utf-8")

    bundle = _bundle(tmp_path, "inc_01")
    (bundle / "metrics").symlink_to(outside, target_is_directory=True)

    result = validate_incident_bundle(bundle)
    assert not result.ok
    assert "bundle_escapes_root" in _error_codes(result)
    assert any(issue.path == "metrics" for issue in result.errors)


@pytest.mark.skipif(not _can_symlink(), reason="Symlinks are not supported on this platform")
def test_symlinked_bundle_entry_inside_root_is_accepted(tmp_path: Path):
    bundle = _bundle(tmp_path, "inc_01")
    (bundle / "service").mkdir()
    (bundle / "service" / "app.py").write_text("x = 1\n", encoding="utf-8")
    (bundle / "src_link").symlink_to(bundle / "service", target_is_directory=True)

    result = validate_incident_bundle(bundle)
    assert result.ok, result.error_messages()


@pytest.mark.skipif(not _can_symlink(), reason="Symlinks are not supported on this platform")
def test_symlinked_bundle_root_is_rejected(tmp_path: Path):
    real = _bundle(tmp_path, "real_incident")
    _add_logs(real)
    link = tmp_path / "inc_01"
    link.symlink_to(real, target_is_directory=True)

    result = validate_incident_bundle(link)
    assert not result.ok
    assert "bundle_symlink" in _error_codes(result)


@pytest.mark.skipif(not _can_symlink(), reason="Symlinks are not supported on this platform")
def test_metrics_file_symlink_escaping_root_is_rejected(tmp_path: Path):
    outside = tmp_path / "outside_metrics.csv"
    outside.write_text(
        "timestamp,latency_ms\n2026-08-28T14:00:00Z,SENTINEL_OUTSIDE_VALUE\n",
        encoding="utf-8",
    )

    bundle = _bundle(tmp_path, "inc_01")
    (bundle / "metrics").mkdir()
    (bundle / "metrics" / "metrics.csv").symlink_to(outside)

    result = validate_incident_bundle(bundle)
    assert not result.ok
    assert "bundle_escapes_root" in _error_codes(result)
    assert any(issue.path == "metrics/metrics.csv" for issue in result.errors)
    # The escaping file must never be parsed: if it had been read, the
    # non-numeric sentinel value would appear in a metrics_csv_invalid issue.
    assert "metrics_csv_invalid" not in _error_codes(result)
    assert all("SENTINEL_OUTSIDE_VALUE" not in issue.detail for issue in result.issues)


@pytest.mark.skipif(not _can_symlink(), reason="Symlinks are not supported on this platform")
def test_log_file_symlink_escaping_root_is_rejected(tmp_path: Path):
    outside = tmp_path / "outside.log"
    outside.write_text("", encoding="utf-8")

    bundle = _bundle(tmp_path, "inc_01")
    (bundle / "logs").mkdir()
    (bundle / "logs" / "application.log").symlink_to(outside)

    result = validate_incident_bundle(bundle)
    assert not result.ok
    assert "bundle_escapes_root" in _error_codes(result)
    assert any(issue.path == "logs/application.log" for issue in result.errors)
    # An empty log file is a warning; the escaping one must not be read at all.
    assert "empty_log_file" not in {issue.code for issue in result.issues}


@pytest.mark.skipif(not _can_symlink(), reason="Symlinks are not supported on this platform")
def test_escaping_evidence_symlink_is_never_opened(tmp_path: Path, monkeypatch):
    outside = tmp_path / "outside_metrics.csv"
    outside.write_text(
        "timestamp,latency_ms\n2026-08-28T14:00:00Z,SENTINEL_OUTSIDE_VALUE\n",
        encoding="utf-8",
    )

    bundle = _bundle(tmp_path, "inc_01")
    _add_logs(bundle)  # a legitimate file, so the trace is non-empty
    (bundle / "metrics").mkdir()
    (bundle / "metrics" / "metrics.csv").symlink_to(outside)

    opened: List[str] = []
    real_path_open = Path.open

    def recording_path_open(self, *args, **kwargs):
        opened.append(str(self))
        return real_path_open(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", recording_path_open)

    validate_incident_bundle(bundle)

    assert any("application.log" in path for path in opened)
    assert all("outside_metrics.csv" not in path for path in opened)


def test_api_rejects_path_traversal_incident_ids(tmp_path: Path):
    incidents_root = tmp_path / "incidents"
    incidents_root.mkdir()
    _add_logs(_bundle(incidents_root, "inc_01"))

    app = create_app(
        repository=FilesystemRepository(persistence_root=tmp_path / "persistence"),
        incidents_root=incidents_root,
    )
    client = TestClient(app)

    for bad_id in ("../inc_01", "sub/inc_01", "inc:01", "inc 01"):
        response = client.post("/investigations", json={"incident_id": bad_id})
        assert response.status_code == 400, bad_id


# ===========================================================================
# 6. Clear, actionable validation errors
# ===========================================================================

def test_metrics_error_names_file_problem_and_row(tmp_path: Path):
    bundle = _bundle(tmp_path)
    _add_metrics(
        bundle,
        "timestamp,latency_ms\n2026-08-28T14:00:00Z,120.0\n2026-08-28T14:00:01Z,boom\n",
    )
    result = validate_incident_bundle(bundle)
    message = result.error_messages()[0]
    assert "metrics/metrics.csv" in message
    assert "latency_ms" in message
    assert "row 3" in message


def test_error_message_includes_issue_path_and_detail():
    result = validate_incident_bundle(INCIDENTS / "inc_01_n_plus_one_query", expected_id="other")
    issue = result.errors[0]
    assert issue.as_message() == f"{issue.path}: {issue.detail}"
    assert "inc_01_n_plus_one_query" in issue.detail


def test_all_problems_are_reported_in_one_pass(tmp_path: Path):
    bundle = _bundle(tmp_path)
    (bundle / "logs").mkdir()  # declares logs but provides no file
    _add_metrics(bundle, "timestamp,latency_ms\n2026-08-28T14:00:00Z,boom\n")
    result = validate_incident_bundle(bundle)
    codes = set(_error_codes(result))
    assert "required_file_missing" in codes
    assert "metrics_csv_invalid" in codes
    assert len(result.errors) == 2


def test_issue_severities_are_partitioned(tmp_path: Path):
    bundle = _bundle(tmp_path)
    _add_logs(bundle, "")
    result = validate_incident_bundle(bundle)
    assert result.ok
    assert {issue.severity for issue in result.warnings} == {"warning"}
    assert {issue.severity for issue in result.errors} == set()


def test_orchestrator_preflight_error_names_the_affected_file(tmp_path: Path):
    bundle = _bundle(tmp_path, "inc_bad_metrics")
    _add_metrics(bundle, "timestamp,latency_ms\n2026-08-28T14:00:00Z,boom\n")

    orchestrator = IncidentOrchestrator(non_interactive=True)
    with pytest.raises(InvalidIncidentBundleError) as excinfo:
        orchestrator.investigate(bundle)

    message = str(excinfo.value)
    assert "metrics/metrics.csv" in message
    assert "row 2" in message


def test_orchestrator_missing_bundle_still_raises_file_not_found(tmp_path: Path):
    orchestrator = IncidentOrchestrator(non_interactive=True)
    with pytest.raises(FileNotFoundError):
        orchestrator.investigate(tmp_path / "inc_missing_bundle")


def test_api_maps_invalid_bundle_to_400(tmp_path: Path):
    incidents_root = tmp_path / "incidents"
    incidents_root.mkdir()
    bundle = _bundle(incidents_root, "inc_bad_metrics")
    _add_metrics(bundle, "timestamp,latency_ms\n2026-08-28T14:00:00Z,boom\n")

    app = create_app(
        repository=FilesystemRepository(persistence_root=tmp_path / "persistence"),
        incidents_root=incidents_root,
    )
    client = TestClient(app)

    response = client.post("/investigations", json={"incident_id": "inc_bad_metrics"})
    assert response.status_code == 400
    payload = response.json()
    assert payload["error"] == "invalid_incident_bundle"
    # The API sanitizes absolute/relative paths, so assert on the actionable
    # parts of the message instead of the redacted path fragment.
    assert "latency_ms" in payload["detail"]
    assert "row 2" in payload["detail"]


def test_api_sync_dispatch_rejects_invalid_bundle_before_persisting_state(tmp_path: Path):
    incidents_root = tmp_path / "incidents"
    incidents_root.mkdir()
    bundle = _bundle(incidents_root, "inc_bad_metrics_sync")
    _add_metrics(bundle, "timestamp,latency_ms\n2026-08-28T14:00:00Z,boom\n")

    repository = FilesystemRepository(persistence_root=tmp_path / "persistence")
    app = create_app(repository=repository, incidents_root=incidents_root)
    client = TestClient(app)

    response = client.post("/investigations", json={"incident_id": "inc_bad_metrics_sync"})

    # The synchronous path rejects before any state is persisted or lease acquired.
    assert response.status_code == 400
    assert response.json()["error"] == "invalid_incident_bundle"
    assert repository.load("inc_bad_metrics_sync") is None


def test_api_async_dispatch_rejects_invalid_bundle_before_accepting(tmp_path: Path):
    incidents_root = tmp_path / "incidents"
    incidents_root.mkdir()
    bundle = _bundle(incidents_root, "inc_bad_metrics_async")
    _add_metrics(bundle, "timestamp,latency_ms\n2026-08-28T14:00:00Z,boom\n")

    repository = FilesystemRepository(persistence_root=tmp_path / "persistence")
    app = create_app(repository=repository, incidents_root=incidents_root)
    client = TestClient(app)

    response = client.post(
        "/investigations",
        json={"incident_id": "inc_bad_metrics_async"},
        headers={"Prefer": "respond-async"},
    )

    # The bundle is rejected before a 202 is issued: no RUNNING state is persisted.
    assert response.status_code == 400
    assert response.json()["error"] == "invalid_incident_bundle"
    assert repository.load("inc_bad_metrics_async") is None


# ===========================================================================
# 7. Validation occurs before any LLM call
# ===========================================================================

def test_no_llm_client_is_constructed_when_bundle_is_invalid(tmp_path: Path):
    bundle = _bundle(tmp_path, "inc_bad_metrics")
    _add_metrics(bundle, "timestamp,latency_ms\n2026-08-28T14:00:00Z,boom\n")

    orchestrator = IncidentOrchestrator(non_interactive=True)
    with patch("agents.orchestrator.get_llm_client", side_effect=AssertionError("LLM client created")) as mocked:
        with pytest.raises(InvalidIncidentBundleError):
            orchestrator.investigate(bundle)
    assert mocked.call_count == 0


def test_no_llm_call_is_made_when_bundle_is_invalid(tmp_path: Path):
    bundle = _bundle(tmp_path, "inc_bad_metrics")
    _add_metrics(bundle, "timestamp,latency_ms\n2026-08-28T14:00:00Z,boom\n")

    client = _RecordingLLMClient()
    orchestrator = IncidentOrchestrator(llm_client=client, non_interactive=True)

    with pytest.raises(InvalidIncidentBundleError):
        orchestrator.investigate(bundle)

    assert client.calls == []
    assert orchestrator.state is None


def test_pipeline_is_not_started_when_bundle_is_invalid(tmp_path: Path):
    bundle = _bundle(tmp_path, "inc_bad_metrics")
    (bundle / "logs").mkdir()

    orchestrator = IncidentOrchestrator(non_interactive=True)
    with patch.object(orchestrator, "_run_pipeline") as pipeline:
        with pytest.raises(InvalidIncidentBundleError):
            orchestrator.investigate(bundle)
    pipeline.assert_not_called()


def test_valid_bundle_reaches_the_pipeline(tmp_path: Path):
    bundle = _bundle(tmp_path, "inc_01")
    _add_logs(bundle)

    orchestrator = IncidentOrchestrator(non_interactive=True)
    with patch.object(orchestrator, "_run_pipeline", return_value={"ok": True}) as pipeline:
        assert orchestrator.investigate(bundle) == {"ok": True}
    pipeline.assert_called_once()


def test_resume_validates_the_bundle_before_running_stages(tmp_path: Path):
    bundle = _bundle(tmp_path, "inc_bad_metrics")
    _add_metrics(bundle, "timestamp,latency_ms\n2026-08-28T14:00:00Z,boom\n")

    repository = FilesystemRepository(persistence_root=tmp_path / "persistence")
    orchestrator = IncidentOrchestrator(repository=repository, non_interactive=True)

    with patch.object(orchestrator, "_run_pipeline") as pipeline:
        with pytest.raises(InvalidIncidentBundleError):
            orchestrator.resume("inc_bad_metrics", bundle)
    pipeline.assert_not_called()


# ===========================================================================
# 8. No source mutation, no access to evaluation-only files
# ===========================================================================

def test_validation_does_not_modify_the_bundle(tmp_path: Path):
    bundle = _bundle(tmp_path, "inc_01")
    _add_logs(bundle)
    _add_metrics(bundle)
    (bundle / "git_diff.patch").write_text("diff --git a/x b/x\n", encoding="utf-8")
    (bundle / "docker-compose.yml").write_text("version: '3.8'\n", encoding="utf-8")

    before = _snapshot(bundle)
    validate_incident_bundle(bundle)
    require_valid_incident_bundle(bundle)
    assert _snapshot(bundle) == before


def test_validation_does_not_modify_invalid_bundles(tmp_path: Path):
    bundle = _bundle(tmp_path, "inc_bad_metrics")
    _add_metrics(bundle, "timestamp,latency_ms\n2026-08-28T14:00:00Z,boom\n")

    before = _snapshot(bundle)
    with pytest.raises(InvalidIncidentBundleError):
        require_valid_incident_bundle(bundle)
    assert _snapshot(bundle) == before


def test_canonical_fixture_is_not_modified_by_validation():
    bundle = INCIDENTS / "inc_01_n_plus_one_query"
    before = _snapshot(bundle)
    require_valid_incident_bundle(bundle, expected_id="inc_01_n_plus_one_query")
    assert _snapshot(bundle) == before


def test_validator_never_opens_evaluation_only_files(tmp_path: Path, monkeypatch):
    bundle = _bundle(tmp_path, "inc_01")
    _add_logs(bundle)
    _add_metrics(bundle)
    (bundle / "ground_truth.md").write_text("SECRET_GROUND_TRUTH", encoding="utf-8")

    opened: List[str] = []
    real_path_open = Path.open
    real_builtin_open = builtins.open

    def recording_path_open(self, *args, **kwargs):
        opened.append(self.name)
        return real_path_open(self, *args, **kwargs)

    def recording_builtin_open(file, *args, **kwargs):
        opened.append(Path(file).name)
        return real_builtin_open(file, *args, **kwargs)

    monkeypatch.setattr(Path, "open", recording_path_open)
    monkeypatch.setattr(builtins, "open", recording_builtin_open)

    result = validate_incident_bundle(bundle)
    require_valid_incident_bundle(bundle)

    assert result.ok
    assert opened, "expected the structured evidence files to be read"
    assert all("ground_truth" not in name for name in opened)
    assert all("results_baseline" not in name for name in opened)
    assert all("baseline_summary" not in name for name in opened)


def test_validation_never_returns_evaluation_only_content(tmp_path: Path):
    bundle = _bundle(tmp_path, "inc_01")
    _add_logs(bundle)
    _metrics = _add_metrics
    _metrics(bundle)
    (bundle / "ground_truth.md").write_text("SECRET_GROUND_TRUTH", encoding="utf-8")

    result = validate_incident_bundle(bundle)
    serialized = json.dumps(
        {
            "incident_id": result.incident_id,
            "bundle_root": result.bundle_root,
            "evidence_sources": list(result.evidence_sources),
            "errors": result.error_messages(),
            "warnings": result.warning_messages(),
        }
    )
    assert "SECRET_GROUND_TRUTH" not in serialized
    assert "ground_truth" not in serialized


def test_orchestrator_never_reads_ground_truth_during_preflight(tmp_path: Path, monkeypatch):
    bundle = _bundle(tmp_path, "inc_01")
    _add_logs(bundle)
    (bundle / "ground_truth.md").write_text("SECRET_GROUND_TRUTH", encoding="utf-8")

    opened: List[str] = []
    real_path_open = Path.open

    def recording_path_open(self, *args, **kwargs):
        opened.append(self.name)
        return real_path_open(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", recording_path_open)

    orchestrator = IncidentOrchestrator(non_interactive=True)
    with patch.object(orchestrator, "_run_pipeline", return_value={"ok": True}):
        orchestrator.investigate(bundle)

    assert all("ground_truth" not in name for name in opened)
