# Reproduction Guide

This guide provides step-by-step instructions to reproduce the baseline and advanced Sentinel evaluation results on synthetic incident bundles.

> **Note**: Complete reproduction commands and environment instructions will be finalized after full implementation of the evaluation pipeline and incident scenarios.

## Prerequisites
- Python 3.11+
- Virtual environment (`venv` or `conda`)
- Free **Groq API Key** (Set as `GROQ_API_KEY` in `.env`)

## Setup
```bash
# 1. Clone repository and navigate to root
cd sentinel-incident-investigator

# 2. Create and activate virtual environment
python -m venv .venv
source .venv/bin/activate  # On Windows: .venv\Scripts\activate

# 3. Install dependencies
pip install -r requirements.txt

# 4. Configure environment
cp .env.example .env
# Edit .env and set your GROQ_API_KEY
```


## Running Baseline
```bash
# Run the single-call baseline across all incident bundles
python eval/run_eval.py --mode baseline
```

## Running Advanced Sentinel
```bash
# Run the evidence-backed Sentinel pipeline across all incident bundles
python eval/run_eval.py --mode advanced
```

## Running Tests
```bash
pytest tests/
```
