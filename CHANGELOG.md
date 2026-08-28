# Changelog
 
All notable changes and iterative improvements to the Sentinel project will be documented in this file.

## [0.2.0] - 2026-08-29


### Added
- **Centralized Groq Runtime LLM Client (`core/llm.py`)**:
  - Implemented `GroqLLMClient` adhering to Groq Python SDK with strict environment-based configuration (`GROQ_API_KEY`, `GROQ_MODEL`).
  - Added unstructured text generation (`generate`) and schema-enforced structured JSON output (`generate_structured`).
  - Built conservative exponential backoff retry handler (default 2 retries) with 429 rate-limit and timeout detection.
  - Implemented secret sanitization utility (`_sanitize_message`) ensuring API keys are never leaked into error messages, exceptions, or logs.
  - Added token and latency telemetry tracking.
- **LLM Client Unit Test Suite (`tests/test_llm.py`)**:
  - 10 deterministic mocked tests covering configuration validation, text completion, structured JSON parsing, code block stripping, error wrapping, rate-limit backoff, and secret masking.

## [0.1.0] - 2026-08-29

### Added
- **Repository Skeleton & Architecture**: Initialized Sentinel project structure with strict JSON Schema contracts (`baseline_schema.json`, `evidence_schema.json`, `hypothesis_schema.json`, `verification_schema.json`, `report_schema.json`).
- **Synthetic Incident Dataset (10 Scenarios)**:
  - `inc_01_n_plus_one_query`: N+1 SQL query inside order loop causing connection pool exhaustion.
  - `inc_02_cache_stampede`: 5-second TTL misconfiguration triggering database stampede.
  - `inc_03_consumer_lag`: Synchronous HTTP webhook in Kafka consumer causing lag surge.
  - `inc_04_memory_leak`: Unevicted in-memory upload payloads causing container OOM kill.
  - `inc_05_race_condition`: Non-atomic check-then-act stock deduction causing overselling.
  - `inc_06_connection_exhaustion`: Unhandled exception branch leaking database pool connections.
  - `inc_07_retry_storm`: 10 immediate zero-delay retries multiplying traffic 10x upon 503 errors.
  - `inc_08_cascading_timeout`: 60s timeout without circuit breaker causing thread starvation.
  - `inc_09_dropped_index`: Table partition migration missing compound index causing sequential table scans.
  - `inc_10_multi_symptom_cascade` (HARD CASE): Dropped index -> slow queries -> client retries -> connection exhaustion -> pod crash cascade.
- **Dataset Validation Suite**: Added `tests/validate_incidents.py` verifying 51 invariant checks (chronological timestamps, metric validity, ground truth, test executability, secret zeroization).

## [Unreleased]

### Planned Next
- `baseline/baseline_agent.py`: Single-call unverified LLM baseline runner for fair comparative benchmark.
- Specialized Evidence Agents (`logs_agent.py`, `metrics_agent.py`, `code_agent.py`).
- Verification Engine (`verification_agent.py`) executing programmatic invariant checks.


