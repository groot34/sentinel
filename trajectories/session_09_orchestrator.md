# Session 09 — Sentinel Orchestrator

**Date**: 2026-08-29
**Agent**: Kiro (KiroCrew)
**Model**: Auto
**Milestone**: 9 — Sentinel Orchestrator Pipeline Integration

---

## Goal

Wire all previously implemented agents into one coherent `agents/orchestrator.py` pipeline.

---

## Files Created / Modified

| File | What |
|---|---|
| `agents/orchestrator.py` | Full orchestrator (replaced stub) |
| `schemas/orchestrator_result_schema.json` | Final result JSON Schema |
| `tests/test_orchestrator.py` | 28 unit tests |
| `eval/sample_runs/orchestrator/orchestrator_inc_{01,04,07,10}.json` | Live run results |
| `CHANGELOG.md`, `README.md`, `TODO.md`, `REPRODUCE.md` | Documentation updated |

---

## Architecture

```
Orchestrator.investigate(incident_dir)
    ├─ Stage 1: LogsAgent.extract_evidence()          [1 Groq call, cached]
    ├─ Stage 2: MetricsAgent.extract_evidence()       [1 Groq call, cached]
    ├─ Stage 3: CodeAgent.extract_evidence()          [1 Groq call, cached]
    ├─ Stage 4: Evidence Fusion (deterministic)       [0 Groq calls]
    ├─ Stage 5: HypothesisEngine.generate()           [1 Groq call, cached]
    ├─ Stage 6: VerificationAgent.verify()            [0 Groq calls, cached]
    ├─ Stage 7: FixProposalAgent.propose_fix()        [N Groq calls — N = confirmed hyps]
    └─ Stage 8: ApprovalGate.review_all()             [0 Groq calls]
```

Orchestrator LLM calls: **0**.

---

## Key Design Decisions

1. **Zero direct LLM calls** — the orchestrator is purely a coordinator.
2. **Per-stage caching** — logs, metrics, code, hypotheses, and verification outputs are cached to disk. Reused on reruns if `incident_id` matches.
3. **Structured failures** — every stage failure is recorded with a typed error; nothing is silently swallowed.
4. **Non-interactive default** — all proposals auto-REJECTED unless the caller explicitly sets `interactive=True` on the gate.
5. **No patch application** — the APPROVED status is recorded only; no source files are written.

---

## LLM Call Accounting

| Stage | Groq calls |
|---|---|
| Logs Agent | 1 |
| Metrics Agent | 1 |
| Code Agent | 1 |
| Hypothesis Engine | 1 |
| Verification Agent | 0 |
| Fix Proposal Agent | N (one per CONFIRMED hyp) |
| Approval Gate | 0 |
| **Orchestrator itself** | **0** |

---

## Test Results

- `tests/test_orchestrator.py`: **28/28 PASSED**
- Full suite: **289/289 PASSED**

---

## Live Run Results

| Incident | Pipeline Status | LLM Calls | Confirmed Hyps | Proposals |
|---|---|---|---|---|
| inc_01 | COMPLETED | 5 | 1 | 1 (REJECTED) |
| inc_04 | COMPLETED | 8 | 4 | 4 (all REJECTED) |
| inc_07 | PARTIAL | 0 (all cached) | 3 | 0 (rate-limit) |
| inc_10 | PARTIAL | 0 (all cached) | 4 | 0 (rate-limit) |

inc_07/inc_10 fix proposals failed due to 200k TPD free-tier exhaustion. All earlier stages reused from cache correctly. Fix proposals for inc_07/inc_10 already exist from milestone 8.

**No source files modified in any run.**

---

## Coding Agent Disclosure

- **Code authored by**: Kiro (KiroCrew AI agent)
- **LLM for code**: Auto model (Claude)
- **LLM for Groq calls**: `openai/gpt-oss-120b` (locked)
- **Ground truth consulted**: No
- **Baseline results consulted**: No
