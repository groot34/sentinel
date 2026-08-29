"""Test suite to validate all Sentinel JSON schemas."""

import json
from pathlib import Path
import pytest
import jsonschema


SCHEMAS_DIR = Path(__file__).parent.parent / "schemas"

SCHEMA_FILES = [
    "baseline_schema.json",
    "evidence_schema.json",
    "hypothesis_schema.json",
    "verification_schema.json",
    "orchestrator_schema.json",
    "report_schema.json",
    "logs_agent_schema.json",
    "metrics_agent_schema.json",
    "code_agent_schema.json",
]


@pytest.mark.parametrize("schema_filename", SCHEMA_FILES)
def test_schema_file_exists_and_is_valid_json(schema_filename: str) -> None:
    schema_path = SCHEMAS_DIR / schema_filename
    assert schema_path.exists(), f"Schema file {schema_filename} does not exist."
    with open(schema_path, "r", encoding="utf-8") as f:
        schema_data = json.load(f)
    assert isinstance(schema_data, dict), f"Schema {schema_filename} is not a valid JSON object."
    assert "$schema" in schema_data, f"Schema {schema_filename} missing '$schema' definition."
    assert "properties" in schema_data, f"Schema {schema_filename} missing 'properties'."


def test_baseline_schema_validation() -> None:
    schema_path = SCHEMAS_DIR / "baseline_schema.json"
    with open(schema_path, "r", encoding="utf-8") as f:
        schema = json.load(f)

    valid_sample = {
        "incident_id": "INC-001",
        "root_cause_guess": "Database connection pool exhausted due to leak in retry loop",
        "reasoning": "Logs show multiple timeout errors following deployment of retry middleware.",
        "confidence": 0.85,
        "suggested_mitigation": "Increase pool size and rollback retry middleware",
    }
    jsonschema.validate(instance=valid_sample, schema=schema)


def test_logs_agent_schema_validation() -> None:
    schema_path = SCHEMAS_DIR / "logs_agent_schema.json"
    with open(schema_path, "r", encoding="utf-8") as f:
        schema = json.load(f)

    valid_sample = {
        "incident_id": "inc_01_n_plus_one_query",
        "agent": "logs_agent",
        "summary": "Errors show pool exhaustion after bulk queries.",
        "evidence": [
            {
                "evidence_id": "EV-LOG-001",
                "source": "logs",
                "reference": "logs/application.log:9",
                "timestamp": "2026-08-28T14:10:30Z",
                "type": "error",
                "excerpt": "ERROR [order-service] [db-pool] Pool exhausted: 20/20 active connections held by bulk order serializer",
                "interpretation": "Connection pool is fully occupied.",
            }
        ],
    }
    jsonschema.validate(instance=valid_sample, schema=schema)


def test_metrics_agent_schema_validation() -> None:
    schema_path = SCHEMAS_DIR / "metrics_agent_schema.json"
    with open(schema_path, "r", encoding="utf-8") as f:
        schema = json.load(f)

    valid_sample = {
        "incident_id": "inc_01_n_plus_one_query",
        "agent": "metrics_agent",
        "summary": "Latency and connection count rose versus the early window.",
        "evidence": [
            {
                "evidence_id": "EV-MET-001",
                "source": "metrics",
                "reference": "metrics/metrics.csv:row 9",
                "timestamp": "2026-08-28T14:12:00Z",
                "metric": "latency_p95_ms",
                "value": 10000.0,
                "type": "spike",
                "interpretation": "p95 latency reached 10000ms.",
            }
        ],
    }
    jsonschema.validate(instance=valid_sample, schema=schema)


def test_code_agent_schema_validation() -> None:
    schema_path = SCHEMAS_DIR / "code_agent_schema.json"
    with open(schema_path, "r", encoding="utf-8") as f:
        schema = json.load(f)

    valid_sample = {
        "incident_id": "inc_01_n_plus_one_query",
        "agent": "code_agent",
        "summary": "Added per-item DB query inside serialization loop.",
        "evidence": [
            {
                "evidence_id": "EV-CODE-001",
                "source": "code",
                "reference": "service/app.py:40-42",
                "type": "suspicious_pattern",
                "excerpt": (
                    "        for item in order.items:\n"
                    "            address = db_session.query_address_by_id(item.shipping_address_id)\n"
                    '            data["items"].append({"item_id": item.id})'
                ),
                "interpretation": "DB call executed inside a loop over order items (N+1 query pattern).",
            }
        ],
    }
    jsonschema.validate(instance=valid_sample, schema=schema)


def test_evidence_schema_validation() -> None:
    schema_path = SCHEMAS_DIR / "evidence_schema.json"
    with open(schema_path, "r", encoding="utf-8") as f:
        schema = json.load(f)

    valid_sample = {
        "evidence_id": "EV-LOG-001",
        "source_type": "logs",
        "timestamp": "2026-08-28T12:00:00Z",
        "description": "500 Internal Server Error spikes in auth service",
        "raw_snippet": "ERROR 2026-08-28T12:00:00Z [auth-svc] Connection pool timeout after 30000ms",
        "metadata": {"service": "auth-svc", "severity": "ERROR"},
    }
    jsonschema.validate(instance=valid_sample, schema=schema)


def test_hypothesis_schema_validation() -> None:
    schema_path = SCHEMAS_DIR / "hypothesis_schema.json"
    with open(schema_path, "r", encoding="utf-8") as f:
        schema = json.load(f)

    valid_sample = {
        "incident_id": "inc_01_n_plus_one_query",
        "hypotheses": [
            {
                "hypothesis_id": "HYP-001",
                "claim": "A database query inside the order-item loop caused excessive query volume and pool exhaustion.",
                "evidence_ids": ["EV-LOG-001", "EV-MET-002", "EV-CODE-003"],
                "supporting_reasoning": (
                    "Logs show pool exhaustion by the bulk-order serializer, metrics show elevated DB connection counts, "
                    "and code evidence locates a DB-style call inside the order-item For loop (N+1 amplification)."
                ),
                "falsification_criteria": [
                    "If query count does not increase with item count, reject this hypothesis.",
                    "If the DB call sits outside any loop, reject this hypothesis.",
                ],
                "verification_plan": [
                    "Inspect the serializer query path using AST-based loop+DB-call analysis.",
                    "Run the supplied invariant check that correlates item count with emitted query calls.",
                ],
            }
        ],
    }
    jsonschema.validate(instance=valid_sample, schema=schema)


def test_verification_schema_validation() -> None:
    schema_path = SCHEMAS_DIR / "verification_schema.json"
    with open(schema_path, "r", encoding="utf-8") as f:
        schema = json.load(f)

    valid_sample = {
        "verification_id": "VER-001",
        "hypothesis_id": "HYP-001",
        "status": "CONFIRMED",
        "check_type": "code_invariant",
        "check_code_or_query": "assert unclosed_connections_count > 0",
        "execution_output": "Check passed: 42 connections leaked across 42 retry failures.",
        "verified_evidence_ids": ["EV-LOG-001", "EV-CODE-002"],
        "reasoning": "Metric series confirms pool exhaustion precisely correlates with retry exception timestamps.",
    }
    jsonschema.validate(instance=valid_sample, schema=schema)


def test_report_schema_validation() -> None:
    schema_path = SCHEMAS_DIR / "report_schema.json"
    with open(schema_path, "r", encoding="utf-8") as f:
        schema = json.load(f)

    valid_sample = {
        "incident_id": "INC-001",
        "status": "CONFIRMED",
        "primary_root_cause": "Database connection leak triggered by unhandled exception path in retry middleware.",
        "confirmed_hypothesis_id": "HYP-001",
        "supporting_evidence_ids": ["EV-LOG-001", "EV-CODE-002"],
        "verification_summary": "Executable check verified 42 unreleased connections during retry failures.",
        "executive_summary": "At 12:00 UTC, the auth service experienced degraded availability due to connection exhaustion.",
        "timeline": [
            {
                "timestamp": "2026-08-28T11:58:00Z",
                "event": "Deployment v2.4.1 completed",
                "evidence_id": "EV-CODE-001"
            },
            {
                "timestamp": "2026-08-28T12:00:00Z",
                "event": "Connection pool timeout errors begin",
                "evidence_id": "EV-LOG-001"
            }
        ],
        "fix_proposal": {
          "human_approval_notice": "AWAITING HUMAN APPROVAL — this fix has not been applied.",
          "description": "Wrap database connection in a context manager / try-finally block in retry handler.",
          "patch_diff": "--- a/auth/retry.py\n+++ b/auth/retry.py\n- conn = db.get_connection()\n+ with db.get_connection() as conn:",
          "regression_test": "def test_retry_releases_connection_on_error(): ...",
          "rollback_plan": "Revert commit a1b2c3d and deploy v2.4.0."
        }
    }
    jsonschema.validate(instance=valid_sample, schema=schema)
