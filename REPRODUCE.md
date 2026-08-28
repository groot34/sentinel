# Reproduction Guide

This guide provides step-by-step instructions to reproduce the baseline and advanced Sentinel evaluation results on synthetic incident bundles.

> **Note**: Complete reproduction commands and environment instructions will be finalized after full implementation of the evaluation pipeline and incident scenarios.

## Prerequisites
- Python 3.11+
- Virtual environment (`venv` or `conda`)
- API key for configured LLM provider (OpenAI, Anthropic, Gemini, or local/LiteLLM endpoint)

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
# Edit .env with your LLM configuration
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
