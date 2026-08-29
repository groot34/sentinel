# Sentinel Architecture & Runtime LLM Specification

## 1. Two-Layer AI Model Architecture

To guarantee strict reproducibility and avoid vendor lock-in, Sentinel separates the AI workflow into two distinct layers:

```
┌─────────────────────────────────────────────────────────────┐
│ LAYER A: Coding Assistant (Development Time)                │
│ (Antigravity / Cursor / Claude Code / Local LLM)            │
│ Builds and maintains files in Git repository.               │
└──────────────────────────────┬──────────────────────────────┘
                               │ writes code
                               ▼
┌─────────────────────────────────────────────────────────────┐
│ LAYER B: Sentinel Runtime (Execution Time)                  │
│ Free Groq API (`openai/gpt-oss-120b`)                       │
└──────────────────────────────┬──────────────────────────────┘
                               │
               ┌───────────────┴───────────────┐
               │                               │
               ▼                               ▼
       Baseline System                 Advanced Sentinel
      (Single LLM Guess)         (Multi-Agent Evidence Engine)
```

---

## 2. Centralized Groq Abstraction (`core/llm.py`)

All agents interact exclusively with `core.llm.GroqLLMClient` via `get_llm_client()`. Individual agent modules **never** instantiate third-party SDKs directly.

```
agents/logs_agent.py ────────┐
agents/metrics_agent.py ─────┼──► core/llm.py (GroqLLMClient) ──► Groq API (openai/gpt-oss-120b)
agents/code_agent.py ────────┤
agents/hypothesis_engine.py ─┘
  └─ verification_agent.py: ZERO LLM calls (deterministic Python only)
```

### Logs Agent pipeline (implemented)

```
incident logs (logs/application.log)
        ↓
deterministic log tools (agents/log_tools.py)
        ↓
candidate evidence (exact excerpts + file:line references)
        ↓
Groq evidence summarisation (one generate_structured call)
        ↓
structured log evidence (schemas/logs_agent_schema.json)
```

The Logs Agent organises observations only. It does not emit a verified root cause.

### Metrics Agent pipeline (implemented)

```
metrics.csv
        ↓
deterministic metric tools (agents/metric_tools.py)
        ↓
candidate quantitative evidence
        ↓
Groq summarisation (one generate_structured call)
        ↓
structured metric evidence (schemas/metrics_agent_schema.json)
```

The Metrics Agent reports what the numbers show (spikes, drops, period shifts, correlations). It does not emit a verified root cause.

### Code Agent pipeline (implemented)

```
source code + git_diff.patch
        ↓
deterministic code tools (agents/code_tools.py)
        ↓
candidate code evidence — added/removed lines, hunks, suspicious patterns
        ↓
small evidence bundle with real file:line or git_diff.patch:hunk references
        ↓
Groq interpretation (one generate_structured call)
        ↓
structured code evidence (schemas/code_agent_schema.json)
```

The Code Agent reports what changed in the source and what behaviour that change may introduce. It does not emit a verified root cause.

### Hypothesis Engine pipeline (implemented)

```
┌──────────────────────┐
│ Logs Evidence        │  EV-LOG-NNN
│ Metrics Evidence     │  EV-MET-NNN   ──►  allowed evidence ID set
│ Code Evidence        │  EV-CODE-NNN
└──────────────────────┘
           │
           ▼
ONE Groq structured-generation call (core.llm, openai/gpt-oss-120b, temp=0.0)
           │
           ▼
Raw hypothesis JSON (claim + evidence_ids + falsification + plan)
           │
           ▼
Deterministic validation + repair:
  • Unknown evidence IDs stripped
  • Hypotheses with no valid evidence dropped
  • IDs re-stamped HYP-001..HYP-NNN
  • 1 ≤ |hypotheses| ≤ 4 enforced
  • JSON Schema validate against hypothesis_schema.json
           │
           ▼
1–4 competing, falsifiable hypotheses (schemas/hypothesis_schema.json)
```

The Hypothesis Engine **proposes**; it never verifies. Exactly one Groq call. Never reads ground truth. Never reads baseline results.

### Verification pipeline (implemented — ZERO Groq)

```
Incident directory
  + hypothesis_claim + plan_step
  + referenced_evidence (EV-LOG/MET/CODE IDs)
           │
           ▼
Keyword dispatch (agents/verification_tools.py: run_dispatch_check)
  ├─ AST: DB call inside For/While loop?
  ├─ AST: retry constants MAX_RETRIES / backoff = 0 / retry loops?
  ├─ AST: acquire() without guaranteed release()?
  ├─ AST: class-level or module-level mutable dict/list?
  ├─ Patch: DROP INDEX SQL line delta (added − removed)?
  ├─ Metrics CSV: metric spike ordering / max values?
  ├─ Log file: pattern counts (ERROR / WARN / retry / …)?
  └─ Fallback: ground referenced excerpts against real source/logs/metrics
           │
           ▼
CheckResult { check_id=CHK-NNN, result ∈ {PASS, FAIL, INCONCLUSIVE},
              evidence[] ← EV-IDs, reference ← real file:line or git_diff.patch }
           │
           ▼
Verdict rule (agents/verification_agent.py: _checks_to_verdict):
  • ANY FAIL → REJECTED
  • ≥1 PASS AND 0 FAIL → CONFIRMED
  • otherwise → INCONCLUSIVE
           │
           ▼
Per-hypothesis: hypothesis_id, verdict, checks[], reasoning, confidence (0.0–1.0)
```

The Verification Agent **tests**; it never invokes Groq. Verdict is always computed deterministically from check results. Strictly read-only (SHA256 of incident files unchanged after full sweep).

### Full reasoning chain topology

```
Logs Evidence ─────┐
Metrics Evidence ──┼──→ Hypothesis Engine (1× Groq)
Code Evidence ─────┘           │
                               ▼
                         HYP-001..HYP-00N
                               │
                               ▼
                      Verification Tools (deterministic, 0 Groq)
                               │
                               ▼
                 CONFIRMED / REJECTED / INCONCLUSIVE  ←  per HYP-NNN
                               │
                               ▼
                 Fix Proposal Agent (N Groq; one per CONFIRMED hypothesis)
                               │
                               ▼
                 Proposal Validation (agents/fix_tools.py; 0 Groq)
                               │
                               ▼
                 Human Approval Gate (PROPOSED → APPROVED | REJECTED)
                       default / non-interactive → REJECTED
```

### Final Evaluation pipeline (Milestone 10 — IMPLEMENTED)

```
10 canonical incident bundles ─────────────────────────────────────┐
                                                                   │
                                                                   ▼
┌────────────────────────────────────────────────────────────────────┐
│                    eval.run_sentinel_eval.py                        │
│ ┌────────────────── for each incident ──────────────────────────┐ │
│ │  1. load_sentinel_final() → reuse cache?                       │ │
│ │     YES → skip directly to evaluator.                          │ │
│ │     NO  → IncidentOrchestrator: Logs→Metrics→Code→Fusion→     │ │
│ │             Hypothesis→Verification→FixProposal→Approval      │ │
│ │             8 stages; caches each stage output under          │ │
│ │             eval/results/sentinel/<incident_id>/               │ │
│ └────────────────────────────────────────────────────────────────┘ │
│                                                                     │
│ ┌────────── evaluator (only now reads ground_truth.md) ──────────┐ │
│ │  CorrectnessEvaluator.evaluate_diagnosis(...)                  │ │
│ │  → sentinel_verdict ∈ {CORRECT, INCORRECT, REVIEW, FAILURE}    │ │
│ │  → sentinel_verdict_explanation                                │ │
│ └────────────────────────────────────────────────────────────────┘ │
│                                                                     │
│ Write per-incident → eval/results/sentinel/<id>/sentinel_final.json │
│ After all 10: write final_comparison.csv + final_summary.json       │
└────────────────────────────────────────────────────────────────────┘
                                                                   │
Post-hoc integrity pass (scripts/_normalize_final_benchmark.py):    │
  • locks baseline aggregate numbers to baseline_summary.json       │
    (the authoritative commit-e999b2a numbers) instead of           │
    recomputing from an incomplete CSV rows commit.                 │
  • verifies denominator = 10 for both systems; failures are       │
    never removed from the denominator.                             │
  • cross-checks CSV rows ↔ per-incident records ↔ summary.        │
  • adds integrity block documenting provenance + instrumentation   │
    gaps (Sentinel stage token counts not exposed).                 │
                                                                   ▼
                  final_comparison.csv + final_summary.json
```

### Evaluation Isolation Contracts (Milestone 10)

| Component | reads ground_truth.md? | reads baseline CSV/summary? | direct LLM calls? |
|---|:---:|:---:|:---:|
| Orchestrator runtime | **NO** (AST-tested in `test_orchestrator_never_reads_ground_truth`) | **NO** | 0 |
| Logs / Metrics / Code agents | **NO** | **NO** | 1 each |
| Hypothesis Engine | **NO** | **NO** | 1 |
| Verification Agent | **NO** | **NO** | 0 |
| Fix Proposal Agent | **NO** | **NO** | N per CONFIRMED hypothesis |
| Approval Gate | **NO** | **NO** | 0 |
| Sentinel evaluation runner (runtime portion) | **NO** (AST-tested in `test_run_sentinel_eval_never_reads_ground_truth`) | **NO** (AST-tested in `test_run_sentinel_eval_never_reads_baseline_results`) | 0 |
| `CorrectnessEvaluator` (**post-run only**) | YES | NO | 0 |
| Normalizer (**post-run only**) | NO | YES (for locked baseline numbers) | 0 |

### Key Architectural Guardrails
1. **Zero Secret Leakage**: `_sanitize_message` strips Groq/OpenAI pattern tokens from all exceptions, error logs, and stack traces.
2. **Rate Limit & Backoff**: Exponential backoff on 429 rate limit errors with conservative retry limits (default 2 retries) to preserve free-tier quotas.
3. **Structured JSON Output**: Native JSON mode with markdown wrapper stripping ensures deterministic contract conformance with `schemas/*.json`.
4. **Deterministic Work Before LLM**: Agents use deterministic Python tools for log pattern parsing, metric spike detection, and git diff analysis before calling Groq.
