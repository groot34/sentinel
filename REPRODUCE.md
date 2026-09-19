# Reproduction Guide

This guide provides step-by-step instructions to reproduce Sentinel evaluation results on synthetic incident bundles.

## Prerequisites
- Python 3.11+
- Virtual environment (`venv` or `conda`)
- Free **Groq API Key** — get one at https://console.groq.com (no credit card required)

## Setup
```bash
# 1. Clone repository and navigate to root
git clone https://github.com/groot34/sentinel.git
cd sentinel-incident-investigator

# 2. Create and activate virtual environment
python -m venv .venv
source .venv/bin/activate  # On Windows: .venv\Scripts\activate

# 3. Install dependencies
pip install -r requirements.txt

# 4. Configure environment
cp .env.example .env
# Edit .env and set:  GROQ_API_KEY=gsk_your_key_here
```

## Running Tests (No API Key Required)
```bash
# Run full test suite — zero real Groq API calls
pytest
```

## Starting the API Server

The Sentinel API server exposes the investigation pipeline over HTTP using
the Starlette/Uvicorn stack (both included in `requirements.txt`).

```bash
# Start the server with default settings (filesystem backend, localhost:8000)
python -m api

# Or via Makefile
make serve
```

The server binds to `127.0.0.1:8000` by default and is not exposed to the
network.

### Configuration

The launcher calls `load_dotenv()` before reading any environment variable,
so values in `.env` are applied automatically if the file exists. Variables
already set in the shell take precedence (python-dotenv does not override
them).

Copy `.env.example` to `.env` and configure before starting:

```bash
cp .env.example .env
# Required for investigation requests:
#   GROQ_API_KEY=gsk_your_key_here
#   GROQ_MODEL=openai/gpt-oss-120b   (already the default)
#
# Persistence (filesystem is the default, no extra config needed):
#   SENTINEL_PERSISTENCE_BACKEND=filesystem
#   SENTINEL_PERSISTENCE_ROOT=.sentinel_persistence
#
# PostgreSQL backend:
#   SENTINEL_PERSISTENCE_BACKEND=postgres
#   SENTINEL_DATABASE_URL=postgresql://sentinel:password@localhost:5432/sentinel_db
```

### CLI options

| Option | Default | Description |
|--------|---------|-------------|
| `--host` | `127.0.0.1` | Bind address |
| `--port` | `8000` | Listen port |
| `--log-level` | `info` | Uvicorn log level (`debug`, `info`, `warning`, `error`) |
| `--incidents-root` | `incidents` | Path to incident bundles directory |

### Health check

```bash
curl http://127.0.0.1:8000/health
# {"status":"ok","service":"sentinel","version":"2.0"}
```

### Submit an investigation (synchronous)

```bash
curl -s -X POST http://127.0.0.1:8000/investigations \
  -H "Content-Type: application/json" \
  -d '{"incident_id": "inc_01_n_plus_one_query"}'
```

> **Note:** `POST /investigations` blocks until the full pipeline completes.
> Ensure `GROQ_API_KEY` is set and the incident directory exists under `incidents/`.
> The human approval gate is always active; proposed patches are never applied automatically.

### List and retrieve investigations

```bash
# List all stored investigation IDs
curl http://127.0.0.1:8000/investigations

# Retrieve a specific investigation state (including recorded approvals)
curl http://127.0.0.1:8000/investigations/inc_01_n_plus_one_query
```

### Resume an interrupted/failed investigation

```bash
# Resume from the first incomplete/failed stage (reuses previously succeeded stages)
curl -s -X POST http://127.0.0.1:8000/investigations/inc_01_n_plus_one_query/resume
```

> **Resume Rules:**
> - **Allowed statuses:** `FAILED` and `PARTIAL`. Resuming re-injects outputs from previously succeeded stages and executes only remaining stages.
> - **Disallowed statuses:** `COMPLETED` (returns `409 Conflict`) and `RUNNING` (returns `409 Conflict` because an abandoned or active running process cannot be safely distinguished without active leases).

### Record human approval for a fix proposal

```bash
# Record an explicit human review decision on a proposed fix
curl -s -X POST http://127.0.0.1:8000/investigations/inc_01_n_plus_one_query/approval \
  -H "Content-Type: application/json" \
  -d '{
    "proposal_id": "FIX-001",
    "decision": "approved",
    "reviewer": "oncall-lead",
    "notes": "Verified root cause in staging environment."
  }'
```

> **Approval & Decision Integrity Rules:**
> - Non-interactive investigation runs generate automated placeholder rejections (`approved_by: "human"` with default rejection note).
> - Calling `POST /investigations/{id}/approval` records the genuine human decision (`approved` or `rejected`) with reviewer name and optional notes.
> - **Prior human decisions cannot be overwritten:** If an explicit human review is already recorded, subsequent approval calls return `409 Conflict`.
> - **Safety Guarantee:** Recording an approval records the decision metadata in durable state only. It **never** automatically applies patches, modifies repository files, executes shell commands, or deploys code.

### Operational limitations

- **Localhost only by default.** Use `--host` to override (do not expose to the
  network without additional security controls).
- **Single-process concurrency.** Process-local active locking (`_ACTIVE_LOCK`) is
  not shared across multiple Uvicorn worker processes.
- **Optimistic Concurrency Control (OCC).** When using the PostgreSQL backend, concurrent
  state modifications raise `ConcurrencyError` mapped to HTTP `409 Conflict`.
- **Synchronous requests.** Each investigation occupies the server for the full pipeline
  duration. Concurrent requests for the same incident return `409 Conflict`.

---

## Running Logs Agent (Evidence Only)
```bash
# Incident 01 — deterministic log tools + one Groq summarisation call
python -m agents.logs_agent incidents/inc_01_n_plus_one_query \
  --output eval/sample_runs/logs_agent_inc_01.json

# Incident 10 — multi-symptom case (observations only, not a root-cause diagnosis)
python -m agents.logs_agent incidents/inc_10_multi_symptom_cascade \
  --output eval/sample_runs/logs_agent_inc_10.json
```

Requires `GROQ_API_KEY` and locked model `openai/gpt-oss-120b`. The Logs Agent does not read `ground_truth.md` and does not run the Sentinel benchmark.

## Running Metrics Agent (Evidence Only)
```bash
# Incident 01 — database/latency metrics
python -m agents.metrics_agent incidents/inc_01_n_plus_one_query \
  --output eval/sample_runs/metrics_agent_inc_01.json

# Incident 03 — Kafka/consumer-lag metrics
python -m agents.metrics_agent incidents/inc_03_consumer_lag \
  --output eval/sample_runs/metrics_agent_inc_03.json

# Incident 10 — multi-symptom cascade (observations only, not a root-cause diagnosis)
python -m agents.metrics_agent incidents/inc_10_multi_symptom_cascade \
  --output eval/sample_runs/metrics_agent_inc_10.json
```

Requires `GROQ_API_KEY` and locked model `openai/gpt-oss-120b`. The Metrics Agent does not read `ground_truth.md` and does not run the Sentinel benchmark.

## Running Code Agent (Evidence Only)
```bash
# Incident 01 — N+1 query pattern inside serializer loop
python -m agents.code_agent incidents/inc_01_n_plus_one_query \
  --output eval/sample_runs/code_agent_inc_01.json

# Incident 04 — memory-related unbounded registry code change
python -m agents.code_agent incidents/inc_04_memory_leak \
  --output eval/sample_runs/code_agent_inc_04.json

# Incident 07 — aggressive retry behaviour (10 retries, zero backoff)
python -m agents.code_agent incidents/inc_07_retry_storm \
  --output eval/sample_runs/code_agent_inc_07.json

# Incident 10 — hard multi-symptom cascade (observations only, not a root-cause diagnosis)
python -m agents.code_agent incidents/inc_10_multi_symptom_cascade \
  --output eval/sample_runs/code_agent_inc_10.json
```

Requires `GROQ_API_KEY` and locked model `openai/gpt-oss-120b`. The Code Agent does not read `ground_truth.md` and does not run the Sentinel benchmark.

## Running Single Baseline Diagnosis
```bash
# Diagnose one incident (requires GROQ_API_KEY in .env)
python -m baseline.baseline_agent incidents/inc_01_n_plus_one_query

# Save output to file
python -m baseline.baseline_agent incidents/inc_01_n_plus_one_query \
  --output eval/sample_runs/baseline_sample_inc_01.json
```

## Running Full Baseline Evaluation (All 10 Incidents)
```bash
# Full evaluation — sleeps 3s between incidents to respect free-tier rate limits
python -m eval.run_eval --mode baseline --sleep 3

# Run only a specific incident
python -m eval.run_eval --mode baseline --incident inc_01_n_plus_one_query

# Run incidents 1–5 only
python -m eval.run_eval --mode baseline --start 1 --end 5 --sleep 3

# Re-run everything from scratch (discard cached results)
python -m eval.run_eval --mode baseline --no-resume --sleep 3
```

> **Rate limit note**: The harness saves results after each incident. If a rate-limit error occurs,
> re-run the same command — already-completed incidents are automatically skipped.

Results are written to:
- `eval/results/baseline/<incident_id>.json` — per-incident raw diagnosis (no ground truth)
- `eval/results_baseline.csv` — full results with correctness verdicts
- `eval/baseline_summary.json` — accuracy, token usage, and model lock

## Running the Hypothesis Engine (requires Groq)
```bash
# Generate hypotheses from pre-extracted evidence bundles
python agents/hypothesis_engine.py \
  --incident-id inc_01_n_plus_one_query \
  --logs eval/sample_runs/logs_agent_inc_01.json \
  --metrics eval/sample_runs/metrics_agent_inc_01.json \
  --code eval/sample_runs/code_agent_inc_01.json \
  --out eval/sample_runs/hypothesis_inc_01.json
```

Rules: one Groq call only; 1–4 hypotheses; every evidence_id must be present in inputs.

## Running the Verification Agent (No Groq)
```bash
# Verify hypotheses against the incident bundle using deterministic read-only checks
python agents/verification_agent.py \
  incidents/inc_10_multi_symptom_cascade \
  --hypotheses eval/sample_runs/hypothesis_inc_10.json \
  --logs eval/sample_runs/logs_agent_inc_10.json \
  --metrics eval/sample_runs/metrics_agent_inc_10.json \
  --code eval/sample_runs/code_agent_inc_10.json \
  --out eval/sample_runs/hypothesis_verification_inc_10.json
```

Verification is pure Python: AST scanning, log/metric CSV parsing, git-diff line counting, and referenced-excerpt grounding. No mutations, no shell execution, no LLM. Each hypothesis verdict is strictly computed from check PASS/FAIL counts.

## Running the Full Hypothesis + Verification Chain (Incidents 01/04/07/10)
```bash
# Produces hypothesis_*.json and hypothesis_verification_*.json under eval/sample_runs/.
# Reuses existing evidence samples when present; generates missing logs/metrics samples first.
# Requires GROQ_API_KEY only when evidence samples or hypotheses need generation.
python run_hypothesis_verification.py
```

## Running Advanced Sentinel (Full Orchestrator Pipeline)
```bash
python -m eval.run_sentinel_eval --sleep 2 --start 1 --end 10
```


## Running the Sentinel Orchestrator (Full Pipeline)

```bash
# Run the complete Sentinel investigation pipeline on one incident.
# Non-interactive mode: approval gate auto-rejects all proposals.
# --sleep 3 respects the free-tier RPM rate limit between LLM-heavy stages.
# --cache-dir enables resumability: completed stages are reused on reruns.

python -m agents.orchestrator incidents/inc_01_n_plus_one_query \
  --non-interactive --sleep 3 \
  --cache-dir eval/results/sentinel \
  --output eval/sample_runs/orchestrator/orchestrator_inc_01.json

# Re-running after a rate limit: cached stages are reused automatically.
# Only stages that failed (or were never run) are re-attempted.

python -m agents.orchestrator incidents/inc_04_memory_leak \
  --non-interactive --sleep 3 \
  --cache-dir eval/results/sentinel \
  --output eval/sample_runs/orchestrator/orchestrator_inc_04.json
```

> **Rate limit note:** The free-tier 200k tokens/day (TPD) limit is easily reached when running 4
> large incidents back-to-back. Use `--cache-dir` to avoid re-running completed stages. If the fix-
> proposal stage fails with a TPD error, wait for the daily reset and rerun — all earlier stages
> will be restored from cache at zero token cost.

## Running the Final Comparative Benchmark (Milestone 10 — Sentinel vs Baseline, All 10 Incidents)

```bash
# Produces:
#   eval/results/sentinel/<incident_id>/sentinel_final.json  (per-incident result)
#   eval/final_comparison.csv          (10 rows, baseline vs sentinel per incident)
#   eval/final_summary.json            (aggregate numbers + safety + integrity block)
#
# Resumable: existing sentinel_final.json are reused.
# Rate-limit safe: sleeps between LLM-heavy stages, preserves completed results.
# Uses CorrectnessEvaluator only AFTER the pipeline has finished (ground-truth isolation).
# Sentinel runtime never reads baseline results or ground truth.

python -m eval.run_sentinel_eval --sleep 2 --start 1 --end 10

# Single-incident check
python -m eval.run_sentinel_eval --incident inc_10_multi_symptom_cascade

# Ignore cache and force a fresh pipeline run for one incident
python -m eval.run_sentinel_eval --incident inc_10_multi_symptom_cascade --force-rerun
```

**Post-benchmark integrity normalization (ensures baseline numbers come from the locked
`baseline_summary.json` rather than being recomputed from potentially-incomplete CSV rows):**

```bash
python scripts/_normalize_final_benchmark.py
```

### Final Artifact Structure
```
eval/
├── results_baseline.csv          ← LOCKED (10 rows; 10/10 correct from commit e999b2a)
├── baseline_summary.json         ← LOCKED authoritative baseline aggregator
├── final_comparison.csv          ← 10 rows; baseline_vs_sentinel per incident
├── final_summary.json            ← aggregate numbers, accuracy_Δ, verification stats, safety
└── results/sentinel/<inc_id>/
    ├── logs.json                  ← Logs Agent evidence output (cached)
    ├── metrics.json               ← Metrics Agent evidence output (cached)
    ├── code.json                  ← Code Agent evidence output (cached)
    ├── evidence_fusion.json       ← Fusion stage (zero Groq)
    ├── hypotheses.json            ← Hypothesis Engine (1× Groq)
    ├── verification.json          ← Verification Agent (zero Groq)
    └── sentinel_final.json        ← Final result + evaluator verdict for this incident
```

### Benchmark Integrity Rules (Milestone 10)
1. **Incidents never touched.** `git diff -- incidents/` must be empty after the run.
2. **Ground-truth isolated.** Only `eval.evaluator.CorrectnessEvaluator` opens `ground_truth.md` and only *after* the pipeline finishes.
3. **Baseline isolated.** Sentinel runtime never opens `results_baseline.csv`, `baseline_summary.json`, or `eval/results/baseline/`.
4. **Accuracy denominator = 10** for both systems. Failures/PARTIAL status are NOT removed.
5. **Approval gate auto-rejection is NOT an investigation failure** (per §5). Incident 10 reports `PARTIAL` status because the fix-proposal stage was rate-limited; the diagnosis verdict still counts because hypotheses + verification produced a CONFIRMED root cause.
6. **All Sentinel verification is Groq-free.** Verification = 0 LLM calls (pure AST + CSV + git-diff + excerpt grounding).
