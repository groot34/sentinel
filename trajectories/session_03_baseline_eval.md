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

### Step 7: Real Evaluation Attempt
- **Command**: `python -m eval.run_eval --mode baseline --sleep 3`
- **Result**: `LLMConfigurationError` — no `.env` file existed.
- Created `.env` from `.env.example`. Added friendly error message guiding user to add `GROQ_API_KEY`.
- **Status**: Real run **BLOCKED** pending `GROQ_API_KEY` in `.env`.

---

## 3. Human Checkpoints
- User confirmed: free Groq API key only (`llama-3.3-70b-versatile`).
- User opted to paste key in chat — but session continued without key.
- Evaluation harness fully implemented and tested; actual run requires user to add key.

---

## 4. Final Output & Artifacts
- **Files Created / Modified**:
  - `eval/evaluator.py` — deterministic correctness evaluator
  - `eval/run_eval.py` — full evaluation runner (replaced stub)
  - `tests/test_eval.py` — 14-test evaluation harness suite
  - `core/llm.py` — `_last_response` telemetry cache added
  - `CHANGELOG.md` — v0.4.0 added
  - `README.md` — evaluation table updated
  - `REPRODUCE.md` — all eval commands documented
  - `TODO.md` — Milestone 5 marked in progress

- **Pending Real Run**: User must set `GROQ_API_KEY` in `.env` then run:
  ```bash
  python -m eval.run_eval --mode baseline --sleep 3
  ```
  The harness auto-resumes on interruption.

---

## 5. Fairness Lock
- **Model**: `llama-3.3-70b-versatile` (Groq free tier)
- **Evaluation method**: deterministic keyword matching (no LLM judge)
- Baseline and future Sentinel must use the same model and incident set.
