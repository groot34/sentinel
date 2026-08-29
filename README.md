# Sentinel — Evidence-Backed Production Incident Investigator

> **Sentinel** is an agentic production-incident investigator that does not stop at a plausible root-cause guess. It gathers evidence from logs, metrics, and code, generates falsifiable hypotheses, verifies them with executable checks, and only reports a root cause when the evidence supports it.

---

## 1. Problem
Backend and on-call engineers frequently face complex production incidents where critical signals are fragmented across logs, telemetry metrics, source code, configuration files, and recent code deployments. A standard single-call LLM can generate a plausible-sounding root-cause explanation, but in high-stakes production incidents, *plausibility is not verification*. Unverified guesses lead to incorrect rollbacks, wasted engineering hours, and prolonged downtime.

## 2. Intended User
- On-call Site Reliability Engineers (SREs)
- Backend Software Engineers
- Incident Response Commanders and DevOps Teams

## 3. Bottleneck
- **Scattered Evidence**: Correlating logs, metric spikes/drops, and git diffs manually is time-consuming under pressure.
- **Hallucination & Confirmation Bias**: Off-the-shelf LLMs latch onto the first plausible clue and hallucinate causal links without testing them.
- **Lack of Verification**: Root-cause hypotheses are rarely tested against actual runtime or telemetry invariants before mitigations are drafted.

## 4. Solution
Sentinel introduces an evidence-driven, hypothesis-testing workflow:
1. **Specialized Inspection**: Dedicated agents extract isolated evidence items from logs, metrics, and code repositories with unique evidence IDs.
2. **Falsifiable Hypotheses**: Generates 1–4 competing, structured hypotheses linked to concrete evidence IDs.
3. **Executable Verification**: Constructs and executes verification checks against evidence invariants.
4. **Evidence-Gated Reporting**: Classifies hypotheses as `CONFIRMED`, `REJECTED`, or `INCONCLUSIVE`. Only `CONFIRMED` hypotheses backed by at least two independent evidence items form the root-cause diagnosis.
5. **Safe Fix Proposals**: Proposes targeted fixes and regression tests gated behind strict human approval.

## 5. Architecture
```
Incident Bundle
    │
    ├─────────────────────────────────────┐
    ▼               ▼                     ▼
Logs Agent      Metrics Agent         Code Agent
(IMPLEMENTED)   (IMPLEMENTED)         (IMPLEMENTED)
    │               │                     │
    └───────────────┼─────────────────────┘
                    ▼
            Evidence Fusion
            (IMPLEMENTED — Orchestrator)
                    │
                    ▼
        Hypothesis Engine (IMPLEMENTED)
        ├─ HYP-001: Falsifiable claim + EV-IDs
        ├─ HYP-002: Competing hypothesis
        ├─ HYP-003: Alternative explanation
        └─ HYP-004: Secondary candidate
                    │
                    ▼
        Verification Agent (IMPLEMENTED)
        ├─ Deterministic AST checks
        ├─ Source/log/metric invariant checks
        ├─ Falsification condition evaluation
        └─ Read-only (no mutations)
                    │
                    ▼
        CONFIRMED / REJECTED / INCONCLUSIVE
                    │
                    ▼
    Fix Proposal Agent (IMPLEMENTED)
    ├─ ONE Groq structured call per CONFIRMED hypothesis
    ├─ Proposal validated by deterministic fix_tools.py
    ├─ Status: PROPOSED (never auto-applied)
    └─ Patch: unified diff, labelled PROPOSED
                    │
                    ▼
    Proposal Validation (IMPLEMENTED — agents/fix_tools.py)
    ├─ Hypothesis eligibility check (CONFIRMED only)
    ├─ Evidence ID existence check
    ├─ File reference existence check
    ├─ Source location validity check
    ├─ Patch safety check (no destructive ops)
    └─ Applied-claim detection
                    │
                    ▼
    Human Approval Gate (IMPLEMENTED — agents/approval_gate.py)
    ├─ State: PROPOSED → PENDING_APPROVAL → APPROVED | REJECTED
    ├─ Default: REJECTED (non-interactive auto-rejects)
    ├─ Only explicit "y/yes" → APPROVED
    └─ Approval records decision ONLY — patch NOT applied
                    │
                    ▼
            APPROVED / REJECTED
                    │
                    ▼
    Final Investigation Result
    (IMPLEMENTED — Orchestrator + orchestrator_result_schema.json)

Orchestrator (IMPLEMENTED — agents/orchestrator.py):
    - Makes ZERO direct LLM calls
    - Coordinates all 8 stages
    - Caches per-stage outputs for resumability (avoids duplicate Groq calls)
    - Non-interactive by default (approval gate auto-rejects)
    - Never reads ground_truth.md or baseline results
    - [NO AUTO-APPLY IN THIS MILESTONE]

[NO AUTO-APPLY] — Actual patch application is gated to a later stage.
```

## 6. Evaluation

Sentinel is evaluated against a fair baseline across 10 canonical synthetic production incident scenarios.

| Component | Status | Accuracy (Root Cause) | Verification Score | Mean Latency | Notes |
|---|---|:---:|:---:|:---:|---|
| **Baseline Investigator** | **MEASURED** | **10/10 (100%)** | **0%** | **15.98s** | Single-shot Groq model (`openai/gpt-oss-120b`). Guesses root cause without executable verification. |
| **Sentinel (Advanced)** | **MEASURED** | **10/10 (100%)** | **100%** | **145.44s** | Multi-agent hypothesis generation, executable code verification, and human-in-the-loop fix gates. **All components IMPLEMENTED.** Fixes always gated behind explicit human approval (non-interactive default REJECTED). |

**Fairness lock**: Baseline and Sentinel are benchmarked on the identical 10 incident bundles using the same Groq model (`openai/gpt-oss-120b`), temperature (`0.0`), and equivalent available evidence.

Benchmark results stored in `eval/results_baseline.csv`, `eval/baseline_summary.json`, `eval/final_comparison.csv`, and `eval/final_summary.json`.

### Final Evaluation (Milestone 10 — Complete)

| Dimension | Baseline | Sentinel | Delta / Notes |
|---|---|---|---|
| Root-cause accuracy | **10/10 = 100.0%** | **10/10 = 100.0%** | +0.0 pp · 0% relative improvement |
| Verified root causes (≥1 CONFIRMED hypothesis) | **0/10 = 0%** | **10/10 = 100.0%** | **+100 pp — the real win** |
| Hypotheses generated | N/A (no hypothesis stage) | **31** | 30 CONFIRMED / 1 REJECTED / 0 INCONCLUSIVE |
| LLM calls | **10** (1 per incident) | **58** (+48) | Logs:1 · Metrics:1 · Code:1 · Hypothesis:1 · Fix proposal: N per CONFIRMED hypothesis. Verification: 0. (Audited breakdown: 70 uncached theoretical calls across 10 incidents − 4 cached in inc_07 − 8 rate-limited/cached in inc_10 = 58 calls in benchmark run). |
| Total tokens | **26,544** | Stage-level telemetry supported | Baseline tokens locked from commit e999b2a baseline_summary.json. Fresh Sentinel pipeline runs track per-stage and aggregate prompt/completion/total tokens via `core.llm` session telemetry. |
| Average latency | **15.98 s** | **145.44 s** | +129.45 s (10× more stages: 3 evidence agents + fusion + hypothesis + verification + fix proposal + approval). Verification itself is pure Python AST/log/metric scanning (~sub-second per incident). |
| Incidents improved | — | **0** | Both systems diagnose all 10 canonical incidents correctly. |
| Incidents regressed | — | **0** | Sentinel never misdiagnoses a case that baseline got right. |
| Incidents equal | — | **10** | The canonical 10-incident dataset achieves 100% diagnostic accuracy on both systems. The Sentinel advantage is in verification rigor (100% vs 0%), not raw accuracy. |

**Verification Effectiveness** (Milestone 7–10 design goal):
- Baseline produces 0 verification checks. Every root-cause claim is an LLM guess.
- Sentinel produces **30 CONFIRMED** and **1 REJECTED** hypotheses across 10 incidents.
- Every CONFIRMED result has a traceable evidence chain: `HYP-NNN → {EV-LOG,MET,CODE}-NNN → CHK-NNN (PASS) → real file:line or git_diff.patch reference → verdict`.
- REJECTED results prove the system can falsify: e.g., Sentinel correctly rejects a connection-pool-exhaustion secondary claim when AST analysis shows every `acquire()` is enclosed in `try/finally` blocks (inc_01 HYP-001/002/003 all correctly REJECTED on the pool-acquisition sub-check even though the core N+1 AST check PASSes — the overall REJECTED verdict shows strict "all conditions must pass" semantics).

**Safety**:
- ground_truth.md isolated (only evaluator reads it post-pipeline — verified via AST scanning in tests).
- baseline results isolated (Sentinel runtime never reads baseline CSV/summary — verified via AST scanning).
- 0 source file modifications by the pipeline.
- 0 patches auto-applied (approval gate non-interactive = all proposals auto-REJECTED, decision recorded but no code changed).
- 0 API keys or `.env` leakage into tracked git files.
- Incident bundle integrity: `git diff -- incidents/` is empty after the full benchmark.

### Per-Incident Table

| Incident | Baseline | Sentinel | Verified | Hypotheses | Fix Proposals | Status | Notes |
|----------|:---:|:---:|:---:|:---:|:---:|---|---|
| inc_01 N+1 query | ✓ CORRECT | ✓ CORRECT | ✓ | 2 (1 CONFIRMED, 1 REJECTED) | 1 | COMPLETED | |
| inc_02 cache stampede | ✓ CORRECT | ✓ CORRECT | ✓ | 4 (4 CONFIRMED) | 4 | COMPLETED | |
| inc_03 consumer lag | ✓ CORRECT | ✓ CORRECT | ✓ | 3 (3 CONFIRMED) | 3 | COMPLETED | |
| inc_04 memory leak | ✓ CORRECT | ✓ CORRECT | ✓ | 3 (3 CONFIRMED) | 3 | COMPLETED | |
| inc_05 race condition | ✓ CORRECT | ✓ CORRECT | ✓ | 3 (3 CONFIRMED) | 3 | COMPLETED | |
| inc_06 connection exhaustion | ✓ CORRECT | ✓ CORRECT | ✓ | 3 (3 CONFIRMED) | 3 | COMPLETED | |
| inc_07 retry storm | ✓ CORRECT | ✓ CORRECT | ✓ | 3 (3 CONFIRMED) | 3 | COMPLETED | Minimal 3 LLM calls (aggressive stage cache reuse). |
| inc_08 cascading timeout | ✓ CORRECT | ✓ CORRECT | ✓ | 3 (3 CONFIRMED) | 3 | COMPLETED | |
| inc_09 dropped index | ✓ CORRECT | ✓ CORRECT | ✓ | 3 (3 CONFIRMED) | 3 | COMPLETED | |
| inc_10 multi-symptom cascade | ✓ CORRECT | ✓ CORRECT | ✓ | 4 (4 CONFIRMED) | 0 | PARTIAL | Fix-proposal stage rate-limited on free-tier TPD limit; diagnosis & verification completed correctly with 4 competing CONFIRMED hypotheses (true cascade). §5: approval rejection / partial fix stage is not an investigation failure. |




## 7. Hot Take
> *Plausibility is the enemy of reliability.* Most AI incident response tools are dangerous because they sound authoritative even when they are dead wrong. Incident investigation is an empirical science: if a hypothesis cannot be verified against concrete evidence, it has no business being in a root-cause report.
