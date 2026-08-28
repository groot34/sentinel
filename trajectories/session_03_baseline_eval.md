# Agent Trajectory: Baseline Evaluation Harness Implementation

## Metadata
- **Tool / Agent Harness**: Antigravity IDE → Claude Sonnet 4.6 (Thinking)
- **Timestamp**: 2026-08-29 00:56:00 UTC
- **Task Scope**: Implement full baseline evaluation harness, deterministic correctness evaluator, and 14-test suite for the Baseline Evaluation milestone.

---

## 1. Instruction & Initial Prompt
```text
Implement the NEXT milestone ONLY: BASELINE EVALUATION.
- Run existing single-shot baseline against all 10 synthetic incidents.
- Implement deterministic CORRECT/INCORRECT/REVIEW evaluator (no LLM judge).
- Save per-incident results, CSV, and summary JSON.
- Support resume, rate-limit sleep, and --incident flag.
- Add 14 unit tests covering all evaluation harness paths.
- Do NOT modify incident dataset or implement advanced agents.
```

---

## 2. Execution Log & Tool Calls

### Step 1: Read Current State
- Viewed: `eval/run_eval.py` (stub), `eval/evaluator.py` (partial from previous session), `eval/rubric.md`, `core/llm.py`, `baseline/baseline_agent.py`, `schemas/baseline_schema.json`.
- Ran: `git status`, `git log --oneline -5`.

### Step 2: Complete `eval/evaluator.py`
- **Tool**: `write_to_file`
- **File**: `eval/evaluator.py`
- Added: `CANONICAL_INCIDENT_CRITERIA` dictionary with keyword groups and distractor lists for all 10 incidents. Special hard-case rule for inc_10 (must mention index, not just pods/retries).
- `CorrectnessEvaluator.evaluate_diagnosis()` returns `(status, explanation)` tuple.

### Step 3: Complete `eval/run_eval.py`
- **Tool**: `write_to_file`
- **File**: `eval/run_eval.py`
- Features: incident discovery, resume support, rate-limit aware sleep, per-incident file saving, CSV writing, summary JSON writing, ground-truth read AFTER baseline output.
- CLI: `--mode baseline`, `--incident`, `--start`, `--end`, `--sleep`, `--no-resume`.

### Step 4: Patch `core/llm.py`
- **Tool**: `multi_replace_file_content`
- Added `_last_response: Optional[LLMResponse] = None` attribute to cache last response for token telemetry (non-breaking).

### Step 5: Unit Tests (`tests/test_eval.py`)
- **Tool**: `write_to_file`
- 14 tests: incident discovery, ground-truth isolation, correctness verdict, correct/incorrect/REVIEW cases, API failure, rate-limit, resume, CSV output, JSON summary, no advanced agents imported, no incident data modification.

### Step 6: Test Execution
- **Command**: `python -m pytest tests/test_eval.py -v`
- **Result**: 14/14 passed.
- **Command**: `python -m pytest -v`
- **Result**: 42/42 passed.

### Step 7: Live Groq Evaluation Benchmark Execution
- Configured `.env` with Groq API key and model `openai/gpt-oss-120b`.
- Executed `python -m eval.run_eval --mode baseline --sleep 3`.
- Evaluation processed all 10 synthetic incidents with zero crashes.
- Incident 10 was rate-limited during initial burst (free-tier TPM 8000), and cleanly resumed using the built-in resume capability.
- Full 10-incident benchmark saved to `eval/results_baseline.csv` and `eval/baseline_summary.json`.

---

## 3. Benchmark Results
- **Model Used**: `openai/gpt-oss-120b` (Groq API)
- **Total Incidents**: 10
- **Evaluated**: 10
- **Correct**: 10 / 10 (100%)
- **Verification Score**: 0% (Baseline has no executable verification checks)
- **Average Latency**: 15.98s
- **Total Prompt Tokens**: 19,877
- **Total Completion Tokens**: 6,667

---

## 4. Final Output & Artifacts
- **Files Created / Modified**:
  - `eval/evaluator.py` — deterministic correctness evaluator
  - `eval/run_eval.py` — full evaluation runner with resume + rate limit sleep
  - `eval/results_baseline.csv` — full 10-incident benchmark output matrix
  - `eval/baseline_summary.json` — aggregate benchmark summary
  - `eval/results/baseline/*.json` — 10 raw baseline diagnoses (no ground truth embedded)
  - `tests/test_eval.py` — 14-test evaluation harness suite
  - `CHANGELOG.md` — v0.4.0 added with measured results
  - `README.md` — benchmark evaluation table updated
  - `REPRODUCE.md` — reproduction commands documented
  - `TODO.md` — Milestone 5 marked completed

---

## 5. Fairness Lock
- **Model**: `openai/gpt-oss-120b` (Groq API)
- **Evaluation Method**: Deterministic keyword/criteria matching (no LLM judge)
- Sentinel and Baseline will be compared on identical incident files with this locked model.

