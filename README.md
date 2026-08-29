# Sentinel — Evidence-Backed Production Incident Investigator

**Sentinel** is a multi-agent production incident investigator that turns noisy logs, metrics,
and source code into evidence-backed, verifiable root-cause diagnoses.

A single LLM call can produce a *plausible* answer. Plausible ≠ verified.
Sentinel does not stop at plausibility: it gathers evidence, generates falsifiable hypotheses,
executes deterministic verification checks, and only reports a root cause when the evidence
chain is complete and traceable.

**Measured on 10 canonical production incidents:**

| | Baseline (single-shot LLM) | Sentinel (multi-agent) |
|---|:---:|:---:|
| Root-cause accuracy | 10/10 | 10/10 |
| Verified root causes | **0/10** | **10/10** |
| Unit tests | — | **315 passed** |
| Incident validation checks | — | **51 passed** |
| Patches auto-applied | — | **0** |

Sentinel's demonstrated advantage is **verification rigor**, not raw diagnostic accuracy.
Both systems diagnose all 10 incidents correctly. Only Sentinel can prove it.

---

## 1. Problem

Backend and on-call engineers face complex production incidents where critical signals are
fragmented across logs, telemetry metrics, source code, configuration files, and recent
deployments. A standard single-call LLM can produce a plausible-sounding root-cause
explanation, but in high-stakes incidents, *plausibility is not verification*. Unverified
guesses lead to incorrect rollbacks, wasted engineering hours, and prolonged downtime.

## 2. Intended User

- On-call Site Reliability Engineers (SREs)
- Backend Software Engineers
- Incident Response Commanders and DevOps Teams

## 3. Bottleneck

- **Scattered Evidence**: Correlating logs, metric spikes/drops, and git diffs manually is
  time-consuming under pressure.
- **Hallucination & Confirmation Bias**: Off-the-shelf LLMs latch onto the first plausible
  clue and hallucinate causal links without testing them.
- **Lack of Verification**: Root-cause hypotheses are rarely tested against actual runtime or
  telemetry invariants before mitigations are drafted.

## 4. Solution

Sentinel introduces an evidence-driven, hypothesis-testing workflow:

1. **Specialized Inspection**: Dedicated agents extract isolated evidence items from logs,
   metrics, and code repositories with unique evidence IDs.
2. **Falsifiable Hypotheses**: Generates 1–4 competing, structured hypotheses linked to
   concrete evidence IDs.
3. **Executable Verification**: Constructs and executes verification checks against evidence
   invariants. Verification is deterministic, read-only Python — zero LLM calls.
4. **Evidence-Gated Reporting**: Classifies hypotheses as `CONFIRMED`, `REJECTED`, or
   `INCONCLUSIVE`. Only `CONFIRMED` hypotheses backed by at least two independent evidence
   items form the root-cause diagnosis.
5. **Safe Fix Proposals**: Proposes targeted fixes and regression tests gated behind strict
   human approval.

## 5. Why Sentinel?

A single LLM can produce a diagnosis. Sentinel separates five distinct responsibilities that
a monolithic call conflates:

| Stage | What it does | How |
|---|---|---|
| Evidence collection | Logs, metrics, code examined independently | 3 LLM-assisted agents |
| Hypothesis generation | Competing falsifiable claims linked to EV-IDs | 1 LLM call |
| Verification | Invariant checks, AST analysis, log/metric scanning | Deterministic Python — 0 LLM calls |
| Remediation proposal | Unified-diff fix per CONFIRMED hypothesis | 1 LLM call per hypothesis |
| Human approval | Explicit human gate before any change | Non-interactive default: REJECTED |

This separation makes every step auditable and every conclusion reproducible.

## 6. Architecture

```
Incident Bundle
    │
    ├─────────────────────────────────────┐
    ▼               ▼                     ▼
Logs Agent      Metrics Agent         Code Agent
(LLM-assisted)  (LLM-assisted)        (LLM-assisted)
    │               │                     │
    └───────────────┼─────────────────────┘
                    ▼
            Evidence Fusion
            (deterministic — Orchestrator)
                    │
                    ▼
        Hypothesis Engine
        (LLM-assisted — 1 call)
        ├─ HYP-001: Falsifiable claim + EV-IDs
        ├─ HYP-002: Competing hypothesis
        ├─ HYP-003: Alternative explanation
        └─ HYP-004: Secondary candidate
                    │
                    ▼
        Verification Agent
        (deterministic — 0 LLM calls)
        ├─ Deterministic AST checks
        ├─ Source/log/metric invariant checks
        ├─ Falsification condition evaluation
        └─ Read-only (no mutations)
                    │
                    ▼
        CONFIRMED / REJECTED / INCONCLUSIVE
                    │
                    ▼
    Fix Proposal Agent
    (LLM-assisted — 1 call per CONFIRMED hypothesis)
    ├─ Proposal validated by deterministic fix_tools.py
    ├─ Status: PROPOSED (never auto-applied)
    └─ Patch: unified diff, labelled PROPOSED
                    │
                    ▼
    ┌─── SAFETY BOUNDARY ────────────────────────────────┐
    │  Human Approval Gate                               │
    │  ├─ State: PROPOSED → PENDING_APPROVAL             │
    │  │          → APPROVED | REJECTED                  │
    │  ├─ Default: REJECTED (non-interactive auto-rejects)│
    │  ├─ Only explicit "y/yes" → APPROVED               │
    │  └─ Approval records decision ONLY — patch NOT applied│
    └────────────────────────────────────────────────────┘
                    │
                    ▼
            APPROVED / REJECTED
                    │
                    ▼
    Final Investigation Result
    (Orchestrator + orchestrator_result_schema.json)

Orchestrator (agents/orchestrator.py):
    - Makes ZERO direct LLM calls
    - Coordinates all 8 stages
    - Caches per-stage outputs for resumability (avoids duplicate Groq calls)
    - Non-interactive by default (approval gate auto-rejects)
    - Never reads ground_truth.md or baseline results
```

**Legend**
- *LLM-assisted*: stage makes one structured Groq call (`openai/gpt-oss-120b`, `temperature=0.0`)
- *Deterministic*: pure Python — AST analysis, log/metric scanning, invariant checks; no model inference
- *Safety boundary*: explicit human decision required; non-interactive default is REJECTED; patch never applied automatically

## 7. Evaluation

Sentinel is evaluated against a fair baseline across 10 canonical synthetic production incidents.

**Same model, same evidence, same incidents — different pipeline.**

| Component | Root-cause accuracy | Verified root causes | Mean latency | LLM calls |
|---|:---:|:---:|:---:|:---:|
| **Baseline** (single-shot) | **10/10 = 100%** | **0/10 = 0%** | 15.98 s | 10 |
| **Sentinel** (multi-agent) | **10/10 = 100%** | **10/10 = 100%** | 145.44 s | 58 |

**Fairness lock**: both systems use `openai/gpt-oss-120b` at `temperature=0.0` on identical incident bundles.

### Key result at a glance

```
Baseline:   10/10 diagnosed    0/10 verified
Sentinel:   10/10 diagnosed   10/10 verified
```

**Sentinel's advantage is verification rigor, not raw diagnostic accuracy.**

### Final Evaluation (Milestone 10 — Complete)

| Dimension | Baseline | Sentinel | Notes |
|---|---|---|---|
| Root-cause accuracy | **10/10 = 100.0%** | **10/10 = 100.0%** | +0.0 pp — no accuracy improvement |
| Verified root causes (≥1 CONFIRMED hypothesis) | **0/10 = 0%** | **10/10 = 100.0%** | **+100 pp — the real win** |
| Hypotheses generated | N/A | **31** | 30 CONFIRMED / 1 REJECTED / 0 INCONCLUSIVE |
| LLM calls | **10** (1 per incident) | **58** (+48) | Logs:1 · Metrics:1 · Code:1 · Hypothesis:1 · Fix proposal: N per CONFIRMED hypothesis. Verification: 0. (Audited: 70 uncached theoretical calls − 4 cached in inc_07 − 8 cached/rate-limited in inc_10 = 58 benchmark calls.) |
| Total tokens | **26,544** | Stage-level telemetry supported | Baseline locked from commit e999b2a. Sentinel per-stage token tracking via `core.llm` session telemetry. |
| Average latency | **15.98 s** | **145.44 s** | +129.45 s — 10× more stages. Verification itself is pure Python AST/log/metric scanning (~sub-second per incident). |
| Incidents improved | — | **0** | Both systems diagnose all 10 incidents correctly. |
| Incidents regressed | — | **0** | Sentinel never misdiagnoses a case that baseline got right. |
| Incidents equal | — | **10** | 100% diagnostic accuracy on both systems. Sentinel advantage: verification rigor (100% vs 0%). |

**Verification effectiveness:**
- Baseline: 0 verification checks — every root-cause claim is an LLM guess.
- Sentinel: **30 CONFIRMED** and **1 REJECTED** hypotheses across 10 incidents.
- Every CONFIRMED result has a traceable evidence chain:
  `HYP-NNN → {EV-LOG,MET,CODE}-NNN → CHK-NNN (PASS) → real file:line or git_diff.patch reference → verdict`
- REJECTED results prove the system can falsify: e.g. Sentinel correctly rejects a
  connection-pool-exhaustion sub-claim when AST analysis shows every `acquire()` is enclosed
  in `try/finally` blocks — strict "all conditions must pass" semantics.

> **⚠ Incident 10 — known limitation.**
> `inc_10_multi_symptom_cascade` completed diagnosis (4 CONFIRMED hypotheses) and verification
> successfully. The fix-proposal stage hit the Groq free-tier daily token limit before
> completing, so **0 fix proposals were produced** for this incident. The overall pipeline
> status is **PARTIAL**. The diagnosis and verification results remain valid and are counted
> in the 10/10 verified score. This limitation is documented here and in
> `eval/final_summary.json` — it is not hidden or adjusted away.

### Safety

- `ground_truth.md` isolated — only the evaluator reads it post-pipeline (verified via AST scan in tests).
- Baseline results isolated — Sentinel runtime never reads baseline CSV/summary (verified via AST scan).
- **0** source file modifications by the pipeline.
- **0** patches auto-applied — approval gate non-interactive = all proposals auto-REJECTED; decision recorded, no code changed.
- **0** API keys or `.env` content leaked into tracked git files.
- Incident bundle integrity: `git diff -- incidents/` is empty after the full benchmark run.

### Per-Incident Table

| Incident | Baseline | Sentinel | Verified | Hypotheses | Fix Proposals | Status | Notes |
|----------|:---:|:---:|:---:|:---:|:---:|---|---|
| inc_01 N+1 query | ✓ | ✓ | ✓ | 2 (1 CONFIRMED, 1 REJECTED) | 1 | COMPLETED | |
| inc_02 cache stampede | ✓ | ✓ | ✓ | 4 (4 CONFIRMED) | 4 | COMPLETED | |
| inc_03 consumer lag | ✓ | ✓ | ✓ | 3 (3 CONFIRMED) | 3 | COMPLETED | |
| inc_04 memory leak | ✓ | ✓ | ✓ | 3 (3 CONFIRMED) | 3 | COMPLETED | |
| inc_05 race condition | ✓ | ✓ | ✓ | 3 (3 CONFIRMED) | 3 | COMPLETED | |
| inc_06 connection exhaustion | ✓ | ✓ | ✓ | 3 (3 CONFIRMED) | 3 | COMPLETED | |
| inc_07 retry storm | ✓ | ✓ | ✓ | 3 (3 CONFIRMED) | 3 | COMPLETED | Minimal 3 LLM calls — aggressive stage cache reuse. |
| inc_08 cascading timeout | ✓ | ✓ | ✓ | 3 (3 CONFIRMED) | 3 | COMPLETED | |
| inc_09 dropped index | ✓ | ✓ | ✓ | 3 (3 CONFIRMED) | 3 | COMPLETED | |
| inc_10 multi-symptom cascade | ✓ | ✓ | ✓ | 4 (4 CONFIRMED) | **0** | **PARTIAL** | Fix-proposal stage rate-limited on free-tier TPD limit. Diagnosis & verification completed with 4 CONFIRMED hypotheses. See warning above. |

Benchmark artefacts: `eval/results_baseline.csv`, `eval/baseline_summary.json`,
`eval/final_comparison.csv`, `eval/final_summary.json`.

---

## 8. Hot Take

> *Plausibility is the enemy of reliability.* Most AI incident response tools are dangerous
> because they sound authoritative even when they are dead wrong. Incident investigation is an
> empirical science: if a hypothesis cannot be verified against concrete evidence, it has no
> business being in a root-cause report.

---

## 9. Quickstart

### Prerequisites
- Python 3.11+
- A free [Groq API key](https://console.groq.com) (no credit card required)

### Install

```bash
git clone https://github.com/groot34/sentinel.git
cd sentinel-incident-investigator
python -m venv .venv
# Linux / macOS
source .venv/bin/activate
# Windows
.venv\Scripts\activate

pip install -r requirements.txt
cp .env.example .env
# Edit .env and set GROQ_API_KEY=gsk_your_key_here
```

### Run the test suite (no API key required)

```bash
pytest
# 315 unit tests + 51 incident dataset validation tests — all mocked, zero real Groq calls
```

### Investigate one incident

```bash
# Runs full 8-stage Sentinel pipeline on inc_01 (N+1 query) — requires GROQ_API_KEY
python -m agents.orchestrator incidents/inc_01_n_plus_one_query
```

Output is a structured JSON result: evidence → hypotheses → CONFIRMED/REJECTED verdicts →
fix proposals → approval gate.

### Reproduce the full benchmark

See **[REPRODUCE.md](REPRODUCE.md)** for the complete step-by-step guide to running the
baseline evaluation, the Sentinel evaluation, and the final comparative benchmark across all
10 incidents.

```bash
# Baseline benchmark (10 incidents, ~3 min on free tier)
python -m eval.run_eval --mode baseline

# Sentinel full benchmark (10 incidents, ~25 min on free tier; resumes from cache)
python -m eval.run_sentinel_eval
```
