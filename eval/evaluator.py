"""Deterministic correctness evaluator comparing diagnoses against ground truth.

This module evaluates incident diagnoses against canonical ground truth definitions.
It operates purely on output text without invoking LLMs for judging.
Outputs one of:
- CORRECT: The underlying technical root cause and mechanism were correctly identified.
- INCORRECT: The diagnosis identified only superficial symptoms, blamed a distractor, or hallucinated causes.
- REVIEW: The diagnosis is semantically ambiguous and warrants human reviewer adjudication.
"""

import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


# Canonical root cause criteria and key causal markers for the 10 incidents
CANONICAL_INCIDENT_CRITERIA: Dict[str, Dict[str, Any]] = {
    "inc_01_n_plus_one_query": {
        "title": "N+1 Database Query in Order API",
        "canonical_root_cause": "N+1 query pattern: individual address queries executed in a loop during order serialization.",
        "required_keywords": [["n+1", "n + 1", "iterative query", "query in loop", "queries in a loop", "repeated query", "individual query", "query per item", "for loop query"]],
        "distractors": ["tcp retransmission", "network packet loss", "replica lag", "network jitter"],
    },
    "inc_02_cache_stampede": {
        "title": "Redis Cache Stampede from TTL Misconfiguration",
        "canonical_root_cause": "TTL reduced to 5s without locking/early expiration, triggering simultaneous cache misses and DB stampede.",
        "required_keywords": [["ttl", "time to live", "cache expiration", "cache expiry", "short cache", "5 second", "5s"], ["stampede", "thundering herd", "cache miss", "concurrency", "dogpiling", "unlocked cache"]],
        "distractors": ["image url", "deprecation warning", "banner payload"],
    },
    "inc_03_consumer_lag": {
        "title": "Kafka Consumer Lag from Synchronous Downstream Handler",
        "canonical_root_cause": "Synchronous, blocking HTTP webhook call placed directly inside consumer loop.",
        "required_keywords": [["synchronous", "blocking", "webhook", "http call in loop", "sync call", "partner-gateway"], ["consumer lag", "slow handler", "poll interval", "message processing time", "blocked consumer"]],
        "distractors": ["broker 2", "partition balance", "rebalance advisory"],
    },
    "inc_04_memory_leak": {
        "title": "Memory Leak from Unclosed File Streaming Buffers",
        "canonical_root_cause": "Unbounded class-level dictionary (AUDIT_TRACE_REGISTRY) retaining full file bytes indefinitely.",
        "required_keywords": [["audit_trace_registry", "audit registry", "global dict", "global map", "retaining reference", "unbounded dict", "class-level", "retained byte", "unreleased memory", "memory leak"]],
        "distractors": ["sts token", "temporary credentials", "s3 renewal"],
    },
    "inc_05_race_condition": {
        "title": "Race Condition in Concurrent Inventory Counter",
        "canonical_root_cause": "Non-atomic check-then-act stock deduction without locking or atomic decrement.",
        "required_keywords": [["race condition", "check-then-act", "non-atomic", "unlocked", "read-modify-write", "concurrency issue", "atomic decrement", "lost update"]],
        "distractors": ["replication ping", "redis replica delay", "12ms delay"],
    },
    "inc_06_connection_exhaustion": {
        "title": "Connection Pool Exhaustion from Missing Close",
        "canonical_root_cause": "ValueError raised before conn.release() without try-finally / context manager, leaking pool connections.",
        "required_keywords": [["leak", "unclosed connection", "missing release", "missing close", "exception before release", "valueerror", "try-finally", "try finally", "context manager"]],
        "distractors": ["autovacuum", "postgres maintenance"],
    },
    "inc_07_retry_storm": {
        "title": "Retry Storm from Aggressive Policy Without Backoff",
        "canonical_root_cause": "Aggressive retry policy (10 immediate retries, 0s backoff, no jitter) amplifying downstream traffic.",
        "required_keywords": [["retry storm", "immediate retry", "no backoff", "0s backoff", "zero backoff", "max_retries", "10 retries", "retry amplification", "tight loop retry"]],
        "distractors": ["dns renewal", "nameserver update"],
    },
    "inc_08_cascading_timeout": {
        "title": "Cascading Timeouts from Missing Circuit Breaker",
        "canonical_root_cause": "60s timeout on slow downstream tax service without circuit breaker causing thread starvation.",
        "required_keywords": [["circuit breaker", "60s timeout", "60 second", "unbounded timeout", "thread starvation", "blocked worker", "tax service timeout"]],
        "distractors": ["tls cert", "certificate check"],
    },
    "inc_09_dropped_index": {
        "title": "Missing Database Index After Migration",
        "canonical_root_cause": "Partition migration omitted composite index on items(tenant_id, status, name), causing full table scans.",
        "required_keywords": [["missing index", "dropped index", "index dropped", "composite index", "table scan", "seq scan", "sequential scan", "idx_tenant_status"]],
        "distractors": ["elasticsearch yellow", "unassigned replica", "dev shard"],
    },
    "inc_10_multi_symptom_cascade": {
        "title": "Complex Multi-Symptom Cascade (HARD CASE)",
        "canonical_root_cause": "Dropped composite index on ledger_entries triggered slow queries -> client retries -> pool saturation -> pod crashes.",
        "required_keywords": [["dropped index", "missing index", "idx_ledger_account_entry_date", "composite index", "012_drop_legacy_ledger_index", "table scan on ledger"]],
        "distractors": ["kubernetes pod", "pod restart", "readiness probe", "node disk pressure", "connection pool size", "client retry policy"],
    },
}


class CorrectnessEvaluator:
    """Evaluates diagnosis text against ground truth using transparent deterministic rules."""

    @staticmethod
    def extract_ground_truth_root_cause(ground_truth_path: Path) -> str:
        """Extract the root cause section from ground_truth.md."""
        if not ground_truth_path.exists():
            return "Ground truth not found."
        text = ground_truth_path.read_text(encoding="utf-8", errors="ignore")
        
        # Match ## Root Cause or ## Underlying Root Cause
        match = re.search(r"##\s+(?:Underlying\s+)?Root\s+Cause\s*\n(.*?)(?=\n##|\Z)", text, re.DOTALL)
        if match:
            return match.group(1).strip()
        return text[:300].strip()

    @classmethod
    def evaluate_diagnosis(
        cls,
        incident_id: str,
        diagnosis_text: str,
        reasoning_text: str = "",
    ) -> Tuple[str, str]:
        """Evaluate a diagnosis against the canonical criteria.

        Args:
            incident_id: Incident directory name.
            diagnosis_text: Model's root_cause_guess.
            reasoning_text: Optional model reasoning text.

        Returns:
            Tuple of (correctness_status, explanation)
            Status is one of: CORRECT, INCORRECT, REVIEW
        """
        criteria = CANONICAL_INCIDENT_CRITERIA.get(incident_id)
        if not criteria:
            return "REVIEW", f"Incident '{incident_id}' has no predefined canonical criteria."

        combined_text = f"{diagnosis_text}\n{reasoning_text}".lower()

        # Check for distractor blame without mentioning root cause
        blamed_distractor = False
        for distractor in criteria.get("distractors", []):
            if distractor.lower() in combined_text:
                blamed_distractor = True
                break

        # Check required keyword groups (each group must have at least one match)
        required_groups = criteria.get("required_keywords", [])
        matched_groups = 0

        for group in required_groups:
            if any(keyword.lower() in combined_text for keyword in group):
                matched_groups += 1

        total_groups = len(required_groups)

        # Special check for incident 10 (hard case):
        # If it only blamed downstream symptoms (K8s pods, connection pool size, retries)
        # without mentioning the root dropped index, it is strictly INCORRECT.
        if incident_id == "inc_10_multi_symptom_cascade":
            has_index_mention = any(k in combined_text for k in ["index", "idx_ledger", "table scan", "seq scan"])
            if not has_index_mention:
                return (
                    "INCORRECT",
                    "Blamed downstream symptoms (Kubernetes pods/connection pool/retries) without identifying the underlying dropped index.",
                )

        if matched_groups == total_groups:
            return "CORRECT", f"Identified all core causal mechanisms ({matched_groups}/{total_groups} criteria groups matched)."
        elif matched_groups > 0 and not blamed_distractor:
            return "REVIEW", f"Partially matched core concepts ({matched_groups}/{total_groups} groups). Requires human verification."
        else:
            reason = "Failed to identify root cause."
            if blamed_distractor:
                reason += " Blamed non-root-cause distractor."
            return "INCORRECT", reason
