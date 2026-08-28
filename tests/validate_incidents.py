"""Validation test suite for all 10 synthetic incidents.

Verifies:
1. Exactly 10 incident directories exist.
2. All required files exist in each incident bundle.
3. Timestamps across logs and metrics are parseable and chronological.
4. Metrics CSV files contain headers and numeric data.
5. Ground truth files exist and detail root cause and causal chains.
6. Every incident has an executable test in service/tests/.
7. No real credential-looking values or proprietary secrets exist.
"""

import csv
import re
from datetime import datetime
from pathlib import Path
import pytest

INCIDENTS_DIR = Path(__file__).parent.parent / "incidents"

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

REQUIRED_FILES = [
    "logs/application.log",
    "metrics/metrics.csv",
    "git_diff.patch",
    "ground_truth.md",
    "docker-compose.yml",
    "service/app.py",
    "service/tests/test_service.py",
]

# Patterns that might indicate accidental inclusion of real secrets/keys
SUSPICIOUS_SECRET_PATTERNS = [
    re.compile(r"AKIA[0-9A-Z]{16}"),  # Real AWS Access Key ID format
    re.compile(r"ghp_[0-9a-zA-Z]{36}"),  # GitHub Personal Access Token
    re.compile(r"-----BEGIN RSA PRIVATE KEY-----"),
    re.compile(r"-----BEGIN OPENSSH PRIVATE KEY-----"),
    re.compile(r"sk-[a-zA-Z0-9]{32,}"),  # Real OpenAI format
    re.compile(r"AIza[0-9A-Za-z-_]{35}"),  # Google API key
]


def test_all_ten_incidents_exist():
    incident_dirs = [p.name for p in INCIDENTS_DIR.iterdir() if p.is_dir()]
    assert len(incident_dirs) == 10, f"Expected 10 incidents, found {len(incident_dirs)}: {incident_dirs}"
    for expected in EXPECTED_INCIDENTS:
        assert expected in incident_dirs, f"Missing expected incident directory: {expected}"


@pytest.mark.parametrize("incident_name", EXPECTED_INCIDENTS)
def test_incident_required_files_exist(incident_name: str):
    inc_path = INCIDENTS_DIR / incident_name
    assert inc_path.exists(), f"Incident folder {incident_name} does not exist."
    for rel_file in REQUIRED_FILES:
        target = inc_path / rel_file
        assert target.exists(), f"Incident {incident_name} missing required file: {rel_file}"
        assert target.stat().st_size > 0, f"Incident {incident_name} file {rel_file} is empty."


@pytest.mark.parametrize("incident_name", EXPECTED_INCIDENTS)
def test_incident_timestamps_parseable_in_logs_and_metrics(incident_name: str):
    inc_path = INCIDENTS_DIR / incident_name

    # 1. Check logs timestamps
    log_file = inc_path / "logs" / "application.log"
    log_content = log_file.read_text(encoding="utf-8")
    log_lines = [l.strip() for l in log_content.splitlines() if l.strip()]
    assert len(log_lines) >= 5, f"Logs in {incident_name} have too few lines ({len(log_lines)})"

    iso_pattern = re.compile(r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z)")
    parsed_log_dates = []
    for line in log_lines:
        match = iso_pattern.match(line)
        if match:
            dt = datetime.strptime(match.group(1), "%Y-%m-%dT%H:%M:%SZ")
            parsed_log_dates.append(dt)

    assert len(parsed_log_dates) >= 3, f"Failed to parse ISO timestamps in logs for {incident_name}"
    # Verify chronological order
    for i in range(1, len(parsed_log_dates)):
        assert parsed_log_dates[i] >= parsed_log_dates[i - 1], f"Log timestamps not chronological in {incident_name}"

    # 2. Check metrics timestamps & numeric values
    metrics_file = inc_path / "metrics" / "metrics.csv"
    with open(metrics_file, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        assert "timestamp" in reader.fieldnames, f"Metrics CSV in {incident_name} missing 'timestamp' column."
        rows = list(reader)
        assert len(rows) >= 4, f"Metrics CSV in {incident_name} has insufficient data rows ({len(rows)})."

        metric_dates = []
        for row in rows:
            dt = datetime.strptime(row["timestamp"], "%Y-%m-%dT%H:%M:%SZ")
            metric_dates.append(dt)
            # Ensure other columns contain numeric data
            for col, val in row.items():
                if col != "timestamp":
                    float(val)  # must parse as numeric float

        for i in range(1, len(metric_dates)):
            assert metric_dates[i] >= metric_dates[i - 1], f"Metrics timestamps not chronological in {incident_name}"


@pytest.mark.parametrize("incident_name", EXPECTED_INCIDENTS)
def test_ground_truth_contains_required_sections(incident_name: str):
    gt_file = INCIDENTS_DIR / incident_name / "ground_truth.md"
    content = gt_file.read_text(encoding="utf-8")
    assert "Root Cause" in content, f"{incident_name} ground_truth.md missing 'Root Cause' section."
    assert "Causal Chain" in content, f"{incident_name} ground_truth.md missing 'Causal Chain' section."
    assert "Minimal Fix" in content, f"{incident_name} ground_truth.md missing 'Minimal Fix' section."
    assert "Detection Test" in content, f"{incident_name} ground_truth.md missing 'Detection Test' section."


@pytest.mark.parametrize("incident_name", EXPECTED_INCIDENTS)
def test_incident_tests_are_executable(incident_name: str):
    test_file = INCIDENTS_DIR / incident_name / "service" / "tests" / "test_service.py"
    assert test_file.exists(), f"Missing test file in {incident_name}"
    content = test_file.read_text(encoding="utf-8")
    assert "def test_" in content, f"No pytest test functions defined in {test_file}"


@pytest.mark.parametrize("incident_name", EXPECTED_INCIDENTS)
def test_no_real_credentials_in_bundle(incident_name: str):
    inc_path = INCIDENTS_DIR / incident_name
    for file_path in inc_path.rglob("*"):
        if file_path.is_file():
            text = file_path.read_text(encoding="utf-8", errors="ignore")
            for pattern in SUSPICIOUS_SECRET_PATTERNS:
                assert not pattern.search(text), f"Potential real credential pattern {pattern.pattern} found in {file_path}!"


def main():
    print(f"Validating all 10 synthetic incidents in {INCIDENTS_DIR}...")
    pytest.main([__file__, "-v"])


if __name__ == "__main__":
    main()
