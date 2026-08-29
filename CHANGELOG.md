# Changelog

All notable changes and iterative improvements to the Sentinel project will be documented in this file.

## [0.8.0] - 2026-08-29

### Added
- **Hypothesis Engine (`agents/hypothesis_engine.py`)**:
  - Receives structured evidence from all three specialist agents (Logs / Metrics / Code).
  - Generates 1–4 competing, falsifiable hypotheses using exactly **one** Groq structured-generation call via `core.llm` (locked model `openai/gpt-oss-120b`).
  - Deterministic validation: every hypothesis `evidence_id` must exist in the supplied evidence bundles — unknown IDs are stripped; hypotheses with no remaining valid evidence are dropped.
  - Hypothesis IDs are deterministically re-stamped `HYP-001..HYP-NNN` regardless of what the LLM emits.
  - Enforces JSON Schema conformance (`schemas/hypothesis_schema.json`) after normalization.
  - CLI: `python agents/hypothesis_engine.py --incident-id inc_X --logs <path> --metrics <path> --code <path> --out <path>`.
  - Strict isolation: never reads `ground_truth.md`, `eval/results_baseline.csv`, or evaluator labels.
- **Hypothesis Schema (`schemas/hypothesis_schema.json`)**: Requires `incident_id` + array of hypotheses each with `hypothesis_id`, `claim`, `evidence_ids` (unique), `supporting_reasoning`, `falsification_criteria`, and `verification_plan`. 1–4 hypotheses enforced.
- **Deterministic Verification Tools (`agents/verification_tools.py`)**:
  - Zero LLM calls. Read-only. No shell execution from LLM output.
  - Public helpers: `iter_service_py_files`, `read_application_log`, `read_metrics_rows`, `contains_db_call_inside_loop` (AST), `find_retry_constants`, `find_backoff_zero`, `find_unbounded_mutable_class_level`, `find_acquire_without_release` (AST name-count + try-finally enclosure check), `find_drop_index_actual_change` (patch SQL-DROP line delta), `count_log_errors_by_pattern`, `metric_spike_order`, `max_metric_value`.
  - `run_dispatch_check` keyword-dispatches plan-step claims to 8 concrete check families: query-in-loop, retry-config, acquire-release, index-drop-SQL change, unbounded collection, metric spike order/correlation, log pattern count, and metric value threshold. Fallback grounds referenced-evidence excerpts against real source/log/metric files.
  - All results return `CheckResult` with `check_id` (`CHK-NNN`), `result` ∈ {PASS, FAIL, INCONCLUSIVE}, referenced `evidence` IDs, and real `reference` paths.
- **Verification Agent (`agents/verification_agent.py`)**:
  - Zero LLM calls. Zero Groq imports. Read-only; never mutates incident files.
  - Consumes hypotheses + three evidence bundles + incident directory.
  - Validates every hypothesis evidence_id against the supplied bundles (unknown IDs are dropped before dispatch).
  - Dispatches each plan step AND each falsification criterion through `run_dispatch_check`.
  - Verdict is computed strictly from check results:
    - **REJECTED** if ANY check result is FAIL.
    - **CONFIRMED** if ≥1 PASS and 0 FAIL.
    - **INCONCLUSIVE** otherwise.
  - Each hypothesis result carries `confidence` (0.0–1.0), per-check detail, and human-readable `reasoning`.
  - Output conforms to `VERIFICATION_OUTPUT_SCHEMA` (inline JSON Schema) with `hypothesis_id`, `verdict` ∈ {CONFIRMED, REJECTED, INCONCLUSIVE}, `checks[]`, `reasoning`, `confidence`.
  - CLI: `python agents/verification_agent.py <incident_dir> --hypotheses <hyp.json> --logs <l.json> --metrics <m.json> --code <c.json> --out <path>`.
  - Strict isolation: never reads `ground_truth.md` or baseline evaluation artifacts.
- **Unit tests** (all mocked Groq; zero real API calls in tests):
  - `tests/test_hypothesis_engine.py` (16 tests): valid generation, 1–4 limit, unique IDs, evidence ID presence, unknown evidence rejection, ground-truth isolation spy, baseline isolation, no direct Groq import, `core.llm` usage, exactly one call, malformed LLM handling, missing/empty evidence handling, no hardcoded incident IDs, schema validation, `_validate_output` strict unknown-ID rejection.
  - `tests/test_verification_tools.py` (20 tests): deterministic behaviour (same input→same result), full incident-file SHA256 read-only check, real source/metric/log references pointing to actual files, safe handling of unsupported plan keywords, no shell execution imports, no hardcoded incident IDs in AST `If` nodes, and incident-specific tool checks (inc_01 DB loop, inc_04 unbounded collection, inc_07 retry config, inc_10 index-drop net delta + acquire-without-release).
  - `tests/test_verification_agent.py` (10 tests): CONFIRMED path (inc_01, query-in-loop dispatches → PASS), REJECTED path (inc_10 index-drop claim with net_sql_drops=0 → FAIL → REJECTED), INCONCLUSIVE path (unresolvable evidence + ungroundable plan), evidence chain preservation (CHK-IDs, per-check evidence IDs, PASS→refs), unknown evidence IDs silently excluded, ground-truth filesystem spy isolation, baseline isolation, no direct Groq imports, zero `get_llm_client`/`generate_structured`, incident files SHA256-unmodified after full dispatch sweep.
  - `tests/test_schemas.py`: extended with `hypothesis_schema.json` parametrised entry and dedicated `test_hypothesis_schema_validation` covering the full multi-hypothesis schema shape.
- **Live Groq sample runs** (hypothesis engine uses real Groq; verification is deterministic, zero Groq):
  - `eval/sample_runs/logs_agent_inc_04.json`, `eval/sample_runs/metrics_agent_inc_04.json` (incident 04 evidence samples).
  - `eval/sample_runs/logs_agent_inc_07.json`, `eval/sample_runs/metrics_agent_inc_07.json` (incident 07 evidence samples).
  - `eval/sample_runs/hypothesis_inc_01.json`, `eval/sample_runs/hypothesis_verification_inc_01.json` (inc 01: 3 hypotheses, verification produces REJECTED on connection-pool subsidiary checks; query-in-loop AST check grounds correctly at `service/app.py:41`).
  - `eval/sample_runs/hypothesis_inc_04.json`, `eval/sample_runs/hypothesis_verification_inc_04.json` (inc 04: 3 hypotheses, all CONFIRMED; `AUDIT_TRACE_REGISTRY` class-level dict check grounds at `service/app.py:5`).
  - `eval/sample_runs/hypothesis_inc_07.json`, `eval/sample_runs/hypothesis_verification_inc_07.json` (inc 07: 3 hypotheses, all CONFIRMED; retry config check grounds at `service/app.py:22` (MAX_RETRIES + zero backoff)).
  - `eval/sample_runs/hypothesis_inc_10.json`, `eval/sample_runs/hypothesis_verification_inc_10.json` (inc 10 cascade: 4 hypotheses all CONFIRMED demonstrating multi-cause evidence chain: pool saturation, dropped-index symptom, acquire-without-release leak, readiness-probe downstream symptom).

## [0.7.0] - 2026-08-29

### Added
- **Deterministic Code Tools (`agents/code_tools.py`)**:
  - Mechanical extractors with no LLM calls: `load_git_diff`, `parse_git_diff`, `list_changed_files`, `extract_added_lines`, `extract_removed_lines`, `extract_hunks`, `iter_source_files`, `search_source`, `get_source_context`, `detect_suspicious_patterns`, `collect_candidate_evidence`.
  - Unified-diff parser with per-hunk added/removed line tracking and 1-indexed file/patch line numbers.
  - AST-based pattern detectors: DB queries inside loops, class-level mutable collections, connection-acquire without guaranteed release, retry loops, check-then-act, unbounded collections.
  - Regex-based line detectors: DROP/CREATE INDEX, high retry counts (>=5), zero backoff, short cache TTL (<=30s), long timeouts (>=30s), circuit-breaker-disabled hints, outbound HTTP calls.
  - References of the form `service/app.py:40-42` (real files) or `git_diff.patch:hunk 1` (patch-hunk fallback when added lines have interleaved context).
- **Code Evidence Agent (`agents/code_agent.py`)**:
  - Locates `git_diff.patch` and `service/` source under an incident directory. Never reads `ground_truth.md`.
  - Runs deterministic extraction first, then exactly one `core.llm` structured summarisation call (`openai/gpt-oss-120b`).
  - Returns observational evidence JSON (not a root-cause diagnosis), with stable IDs `EV-CODE-001+` grounded to real source/diff content.
  - CLI: `python -m agents.code_agent <incident_dir> --output <path>`.
- **Code Agent Schema (`schemas/code_agent_schema.json`)** with sources `git_diff|code|config`, types `added_code|removed_code|suspicious_pattern|changed_config`, and stable `EV-CODE-NNN` IDs.
- **Unit tests**:
  - `tests/test_code_tools.py` (25 tests) covering diff loading/parsing, added/removed/hunk extraction, source search, source context, pattern detection across all 10 canonical incidents, reference grounding, empty/missing diff handling, determinism, and zero hardcoded incident IDs.
  - `tests/test_code_agent.py` (20 tests, mocked Groq) covering core.llm usage, single LLM call, ground-truth isolation, malformed JSON fallback, empty/missing source/diff handling, unique IDs, real reference grounding, unmodified incident files, AST-only hardcoded-answer check, and schema conformance across all incidents.
- **Live Groq sample runs** (not a Sentinel benchmark):
  - `eval/sample_runs/code_agent_inc_01.json` (N+1 query pattern, EV-CODE-001..004)
  - `eval/sample_runs/code_agent_inc_04.json` (memory-leak style unbounded registry, EV-CODE-001..004)
  - `eval/sample_runs/code_agent_inc_07.json` (10 retries + zero backoff, EV-CODE-001..006)
  - `eval/sample_runs/code_agent_inc_10.json` (multi-symptom migration + connection pool observations, EV-CODE-001..004)

## [0.6.0] - 2026-08-29

### Added
- **Deterministic Metric Tools (`agents/metric_tools.py`)**:
  - Mechanical extractors with no LLM calls: `load_metrics`, `list_metrics`, `get_metric_window`, `calculate_summary`, `detect_spikes`, `detect_drops`, `detect_threshold_violations`, `compare_periods`, `detect_metric_correlations`, `find_metric_anomalies`, `collect_candidate_evidence`.
  - Anomaly method: early baseline window of `ceil(n/3)` samples; spike if z >= 2.0 (or any increase when baseline std is 0); drop if z <= -2.0 (or any decrease when std is 0). Population stdev (`statistics.pstdev`).
  - References of the form `metrics/metrics.csv:row N` (1-indexed file lines, header is row 1) with original numeric values preserved.
- **Metrics Evidence Agent (`agents/metrics_agent.py`)**:
  - Loads `metrics/metrics.csv` from an incident directory, never `ground_truth.md`.
  - Runs deterministic extraction first, then exactly one `core.llm` structured summarisation call (`openai/gpt-oss-120b`).
  - Returns observational evidence JSON (not a root-cause diagnosis), with stable IDs `EV-MET-001+` grounded to real samples.
  - CLI: `python -m agents.metrics_agent <incident_dir> --output <path>`.
- **Metrics Agent Schema (`schemas/metrics_agent_schema.json`)**.
- **Unit tests**:
  - `tests/test_metric_tools.py` (12 tests).
  - `tests/test_metrics_agent.py` (11 tests, mocked Groq).
- **Live Groq sample runs** (not a Sentinel benchmark):
  - `eval/sample_runs/metrics_agent_inc_01.json`
  - `eval/sample_runs/metrics_agent_inc_03.json`
  - `eval/sample_runs/metrics_agent_inc_10.json`

## [0.5.0] - 2026-08-29

### Added
- **Deterministic Log Tools (`agents/log_tools.py`)**:
  - Mechanical extractors with no LLM calls: `search_log`, `find_error_lines`, `find_warning_lines`, `count_pattern`, `find_bursts`, `extract_time_window`, `extract_context`, `extract_request_ids`, `collect_candidate_evidence`.
  - 1-indexed references of the form `logs/application.log:<line>` pointing at exact original excerpts.
- **Logs Evidence Agent (`agents/logs_agent.py`)**:
  - Loads `logs/application.log` from an incident directory, never `ground_truth.md`.
  - Runs deterministic extraction first, then exactly one `core.llm` structured summarisation call (`openai/gpt-oss-120b`).
  - Returns observational evidence JSON (not a root-cause diagnosis), with stable IDs `EV-LOG-001+` grounded to real lines.
  - CLI: `python -m agents.logs_agent <incident_dir> --output <path>`.
- **Logs Agent Schema (`schemas/logs_agent_schema.json`)**: Structured evidence contract with `incident_id`, `agent`, `summary`, and traceable evidence items.
- **Unit tests**:
  - `tests/test_log_tools.py` (11 tests) covering search, false references, errors, warnings, counts, context, time windows, request IDs, bursts, empty logs.
  - `tests/test_logs_agent.py` (11 tests, mocked Groq) covering core.llm usage, single call, ground-truth isolation, malformed JSON fallback, empty/noisy logs, unique IDs, unmodified incident files.
- **Live Groq sample runs** (not a Sentinel benchmark):
  - `eval/sample_runs/logs_agent_inc_01.json`
  - `eval/sample_runs/logs_agent_inc_10.json`

## [0.4.0] - 2026-08-29


### Added
- **Baseline Evaluation Benchmark Completed (`eval/run_eval.py`)**:
  - Successfully executed single-shot baseline evaluation against all 10 synthetic incidents on Groq (`openai/gpt-oss-120b`).
  - Recorded 10/10 evaluated incidents with 10/10 root cause guesses matching canonical ground truth.
  - Baseline Verification Score: 0% (single-shot model provides unverified guesses without executable proof).
  - Saved per-incident raw outputs to `eval/results/baseline/inc_*.json`.
  - Saved aggregate matrix to `eval/results_baseline.csv` and benchmark telemetry to `eval/baseline_summary.json`.
  - Telemetry: Average latency = 15.98s, total input tokens = 19,877, total output tokens = 6,667.
- **Deterministic Correctness Evaluator (`eval/evaluator.py`)**:
  - Implemented transparent rule and keyword matcher across 10 canonical incident definitions without an LLM judge.
- **Evaluation Test Suite (`tests/test_eval.py`)**:
  - 14 tests verifying incident discovery, ground truth isolation, resume logic, and metric calculation.
- **Fairness Lock**: Locked model `openai/gpt-oss-120b` (Groq API) for identical comparison when testing the multi-agent Sentinel pipeline.


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
- Fix Proposal Agent and Human Approval Gate (strict "AWAITING HUMAN APPROVAL" notice; auto-apply strictly forbidden).
- Orchestrator: end-to-end multi-agent pipeline wiring (Logs → Metrics → Code → Hypotheses → Verification → Report).
- Comparative Sentinel evaluation benchmark (Baseline 10/10 vs. Advanced) across all 10 incidents with Root Cause Accuracy + Verification Rigor scores.


