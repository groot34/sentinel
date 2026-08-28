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
    ├─► Logs Agent ──────┐
    ├─► Metrics Agent ───┼─► Evidence Items (with IDs)
    └─► Code Agent ──────┘
            │
            ▼
    Hypothesis Engine (1–4 Falsifiable Hypotheses)
            │
            ▼
    Verification Agent (Executable Verification Checks)
            │
            ▼
    Root Cause Report (Confirmed Evidence-Backed Report)
            │
            ▼
    Fix Proposal Agent (Fix + Regression Test)
            │
            ▼
    Human Approval Gate (AWAITING HUMAN APPROVAL)
```

## 6. Evaluation
Sentinel is evaluated against a fair baseline across synthetic production incident scenarios:
- **Baseline**: Single LLM prompt receiving incident context to produce a one-shot root cause guess without verification tools.
- **Sentinel (Advanced)**: Full multi-stage evidence gathering, hypothesis generation, executable verification, and human-in-the-loop fix proposal.
- **Metrics**: Root cause accuracy, hallucination rate, evidence citation validity, and precision of proposed fixes.

*(Evaluation results and benchmark tables will be documented upon execution of the test suite).*

## 7. Hot Take
> *Plausibility is the enemy of reliability.* Most AI incident response tools are dangerous because they sound authoritative even when they are dead wrong. Incident investigation is an empirical science: if a hypothesis cannot be verified against concrete evidence, it has no business being in a root-cause report.
