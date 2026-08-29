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

## Running Advanced Sentinel (Not Yet Implemented)
```bash
python -m eval.run_eval --mode advanced
```
