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
    ▼
Orchestrator
    │
    ├─► Logs Agent (IMPLEMENTED) ──┐
    ├─► Metrics Agent (IMPLEMENTED)┼─► Evidence Items (with IDs)
    └─► Code Agent (IMPLEMENTED) ──┘
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
    Root Cause Report (Confirmed Evidence-Backed Report)
            │
            ▼
    Fix Proposal Agent (NOT IMPLEMENTED)
            │
            ▼
    Human Approval Gate (NOT IMPLEMENTED)
```

## 6. Evaluation

Sentinel is evaluated against a fair baseline across 10 canonical synthetic production incident scenarios.

| Component | Status | Accuracy (Root Cause) | Verification Score | Mean Latency | Notes |
|---|---|:---:|:---:|:---:|---|
| **Baseline Investigator** | **MEASURED** | **10/10 (100%)** | **0%** | **15.98s** | Single-shot Groq model (`openai/gpt-oss-120b`). Guesses root cause without executable verification. |
| **Sentinel (Advanced)** | In Progress | *Pending* | *Target: 100%* | *TBD* | Multi-agent hypothesis generation, executable code verification, and human-in-the-loop fix gates. **Logs Agent = IMPLEMENTED. Metrics Agent = IMPLEMENTED. Code Agent = IMPLEMENTED. Hypothesis Engine = IMPLEMENTED. Verification Agent = IMPLEMENTED.** Fix Proposal Agent, Human Approval Gate, and Orchestrator are not implemented. |

**Fairness lock**: Baseline and Sentinel are benchmarked on the identical 10 incident bundles using the same Groq model (`openai/gpt-oss-120b`), temperature (`0.0`), and equivalent available evidence.

Benchmark results are stored in `eval/results_baseline.csv` and `eval/baseline_summary.json`.




## 7. Hot Take
> *Plausibility is the enemy of reliability.* Most AI incident response tools are dangerous because they sound authoritative even when they are dead wrong. Incident investigation is an empirical science: if a hypothesis cannot be verified against concrete evidence, it has no business being in a root-cause report.
