# Session 08 — Fix Proposal Agent & Human Approval Gate

**Date**: 2026-08-29
**Agent**: Kiro (KiroCrew)
**Model**: Auto (openai/gpt-oss-120b for Groq calls)
**Milestone**: 8 — Fix Proposal Agent + Human Approval Gate

---

## Goal

Implement the Fix Proposal Agent and Human Approval Gate, completing the evidence-gated fix pipeline from CONFIRMED hypothesis to a human-reviewed, never-auto-applied remediation proposal.

---

## Files Created

| File | Role |
|---|---|
| `agents/fix_proposal_agent.py` | ONE Groq call per CONFIRMED hypothesis; returns PROPOSED patch |
| `agents/fix_tools.py` | Zero-LLM deterministic safety validator (10 checks) |
| `agents/approval_gate.py` | State machine: PROPOSED → APPROVED \| REJECTED |
| `schemas/fix_proposal_schema.json` | JSON Schema for FixProposal |
| `schemas/approval_schema.json` | JSON Schema for ApprovalRecord |
| `tests/test_fix_proposal_agent.py` | 21 unit tests (mocked Groq) |
| `tests/test_fix_tools.py` | 48 unit tests |
| `tests/test_approval_gate.py` | 22 unit tests |
| `run_fix_proposals.py` | End-to-end live run script (4 incidents) |

---

## Key Design Decisions

1. **One Groq call per CONFIRMED hypothesis** — REJECTED and INCONCLUSIVE hypotheses produce zero LLM calls.
2. **Force-normalization** — immutable fields (`proposal_id`, `hypothesis_id`, `status`, `human_approval_notice`) are stamped after the LLM call regardless of what the LLM returned.
3. **Patch file restriction** — `check_patch_targets_allowed_files()` enforces that only `service/` relative paths appear in changes. Ground truth, eval, and test files are blocked by the validator.
4. **Default-reject approval gate** — only explicit `y`/`yes`/`Y`/`YES` produces APPROVED; everything else (empty, EOF, whitespace, `n`, unknown) produces REJECTED.
5. **Non-interactive detection** — `sys.stdin.isatty()` used to auto-reject in CI/cron/test environments.
6. **Approval does NOT apply the patch** — the gate only records a decision. Actual patch application is wired in the Orchestrator (Milestone 9).

---

## Test Results

- `tests/test_fix_tools.py`: 48/48 PASSED
- `tests/test_approval_gate.py`: 22/22 PASSED
- `tests/test_fix_proposal_agent.py`: 21/21 PASSED
- Full suite: **261/261 PASSED**

---

## Live Run Results

| Incident | Proposals | Approved | Source Files Changed |
|---|---|---|---|
| inc_01_n_plus_one_query | 0 (all hyps REJECTED) | — | None ✅ |
| inc_04_memory_leak | 3 | 0 (all rejected by human) | None ✅ |
| inc_07_retry_storm | 3 | 1 (FIX-001) | None ✅ |
| inc_10_multi_symptom_cascade | 4 | 1 (FIX-001) | None ✅ |

---

## Coding Agent Disclosure

- **Code authored by**: Kiro (KiroCrew AI agent)
- **LLM for code generation**: Auto model (Claude)
- **LLM for Groq proposal calls**: `openai/gpt-oss-120b` (locked)
- **Ground truth consulted**: No
- **Baseline results consulted**: No
