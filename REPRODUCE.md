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
# Run full test suite — 42 tests, zero real Groq API calls
pytest
```

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
