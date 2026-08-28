# Changelog

All notable changes and iterative improvements to the Sentinel project will be documented in this file.

## [0.4.0] - 2026-08-29

### Added
- **Baseline Evaluation Harness (`eval/run_eval.py`)**:
  - Full evaluation runner with per-incident resume support, rate-limit awareness, and configurable `--sleep` between calls.
  - CLI flags: `--incident`, `--start`, `--end`, `--sleep`, `--no-resume`, `--mode baseline`.
  - Ground-truth isolation enforced: baseline produces output before evaluator reads `ground_truth.md`.
  - Per-incident raw outputs saved to `eval/results/baseline/<incident_id>.json` (no ground truth embedded).
  - Aggregate results saved to `eval/results_baseline.csv` with `incident_id`, `baseline_status`, `baseline_root_cause`, `ground_truth_root_cause`, `correctness`, `latency_seconds`, `input_tokens`, `output_tokens`, `model`, `error`.
  - Summary saved to `eval/baseline_summary.json` with accuracy, token usage, and fairness lock fields.
- **Deterministic Correctness Evaluator (`eval/evaluator.py`)**:
  - Canonical per-incident keyword criteria for all 10 incidents (no LLM judge).
  - Three-tier verdict: `CORRECT` / `INCORRECT` / `REVIEW`.
  - Hard-case check for inc_10: blaming only downstream symptoms without index mention is strictly `INCORRECT`.
  - `extract_ground_truth_root_cause()` reads `ground_truth.md` only after baseline output is committed.
- **Evaluation Test Suite (`tests/test_eval.py`)**:
  - 14 unit tests: incident discovery, ground-truth isolation, correctness evaluator semantics, API failure handling, rate-limit capture, resume detection, CSV/JSON output correctness, no advanced agents invoked, no incident data modification.
- **`core/llm.py` minor patch**: `_last_response` attribute caches last `LLMResponse` on `GroqLLMClient` for token telemetry extraction by evaluation harness (non-breaking).

### Status
- Evaluation harness: **COMPLETE** — all 42 tests pass.
- Real 10-incident Groq run: **PENDING** — requires `GROQ_API_KEY` in `.env`. Run with:
  ```
  python -m eval.run_eval --mode baseline --sleep 3
  ```
  Results will auto-save to `eval/results_baseline.csv` and `eval/baseline_summary.json`.
  The harness resumes automatically on rate-limit interruption.
- Model locked for fairness: `llama-3.3-70b-versatile` (Groq free tier).

## [0.3.0] - 2026-08-29



### Added
- **Single-Shot Groq Baseline Investigator (`baseline/baseline_agent.py`)**:
  - Implemented unverified single-shot baseline incident investigator using centralized `core.llm.GroqLLMClient`.
  - Ingests application logs, metrics CSV, git diff patch, and service source code while strictly isolating `ground_truth.md`.
  - Executes exactly ONE primary Groq LLM completion call per diagnosis with zero verification subroutines.
  - Formats output conforming strictly to `schemas/baseline_schema.json` with root-cause guess, reasoning, confidence, and evidence citations.
  - Added CLI interface supporting `--output` and incident directory arguments.
- **Baseline Unit Test Suite (`tests/test_baseline.py`)**:
  - 7 unit tests verifying schema conformance, incident ID mapping, strict ground truth exclusion, single LLM call enforcement, malformed JSON recovery, API error propagation, and verification of zero direct `groq` SDK imports in baseline code.
- **Sample Run Outputs**:
  - Added `eval/sample_runs/baseline_sample_inc_01.json` (Order API N+1 Query diagnosis).
  - Added `eval/sample_runs/baseline_sample_inc_10.json` (Multi-Symptom Cascade diagnosis demonstrating baseline distraction by downstream pod crashes).

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


