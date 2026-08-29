# Session 10 — Sentinel Final Evaluation

**Date**: 2026-08-29
**Agent**: Antigravity (Google DeepMind)
**Model**: Gemini 3.7 Flash
**Milestone**: 10 — Final Sentinel Evaluation

---

## Goal

Execute fair, deterministic evaluation comparing the Baseline Investigator against the full multi-agent Sentinel pipeline across all 10 canonical incidents on Groq (`openai/gpt-oss-120b`, temperature `0.0`).

---

## Files Created / Modified

| File | What |
|---|---|
| `eval/run_sentinel_eval.py` | Full Sentinel evaluation runner with resume and rate-limit support |
| `scripts/_normalize_final_benchmark.py` | Integrity normalizer locking baseline numbers to `eval/baseline_summary.json` |
| `tests/test_final_evaluation.py` | Unit tests for evaluation harness and integrity guarantees |
| `eval/final_comparison.csv` | 10-row matrix comparing baseline vs Sentinel per incident |
| `eval/final_summary.json` | Aggregate benchmark summary, accuracy delta, and safety audit |
| `README.md`, `CHANGELOG.md`, `TODO.md`, `REPRODUCE.md` | Milestone 10 documentation updates |

---

## Benchmark Results (All 10 Canonical Incidents)

| Dimension | Baseline | Sentinel |
|---|---|---|
| Correctness | **10/10 (100.0%)** | **10/10 (100.0%)** |
| Verified Root Causes | **0/10 (0%)** | **10/10 (100.0%)** |
| Hypotheses Generated | N/A | **31** (30 CONFIRMED, 1 REJECTED) |
| Total LLM Calls | **10** | **58** (accounting for cached stage reuse) |
| Average Latency | **15.98s** | **145.44s** |

---

## Key Findings

1. **Accuracy Parity**: Both systems correctly identify the root causes of all 10 canonical synthetic incidents (+0.0 percentage point accuracy delta).
2. **Verification Rigor**: The true differentiator is executable verification (+100 percentage points). Baseline offers 0% verification (unverified guess); Sentinel constructs 30 confirmed, evidence-backed proofs with deterministic AST, metric, and log invariant checks.
3. **Safety Isolation**: Ground truth is isolated from runtime agents. Baseline outputs are isolated from Sentinel. No patches auto-applied; zero source modifications.
