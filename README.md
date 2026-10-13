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
| Automated tests | — | **609 passed, 32 skipped, 1 warning** |
| Incident validation checks | — | **51 passed** |
| Patches auto-applied | — | **0** |

Sentinel's demonstrated advantage is **verification rigor**, not raw diagnostic accuracy.
Both systems diagnose all 10 incidents correctly. Only Sentinel can prove it.
*(Note: 32 tests are skipped in environments where PostgreSQL is unconfigured or non-admin Windows symlink privileges are absent.)*

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

### 6.1 Sentinel 2.0 Distributed Architecture

Sentinel 2.0 wraps the 8-stage core pipeline in a production-ready, distributed, event-driven infrastructure:

```
                      ┌──────────────────────────────────────┐
                      │    REST / SSE Clients & Frontend     │
                      └──────────────────┬───────────────────┘
                                         │ HTTP / SSE
                                         ▼
                      ┌──────────────────────────────────────┐
                      │   API Server (Starlette / Uvicorn)   │
                      └──────┬────────────────────────┬──────┘
                             │                        │
       Investigation Lifecycle│                        │ Live SSE / Event History
                             ▼                        ▼
  ┌──────────────────────────────────────┐   ┌───────────────────────────────────┐
  │         IncidentOrchestrator         │   │       Outbox Keyset Reader        │
  │ • State Mutation ──► Persist ──► Emit│   │ • Keyset pagination (created_at,  │
  │ • Distributed execution leases       │   │   event_id)                       │
  │ • Safe crash recovery after TTL      │   │ • Last-Event-ID reconnection      │
  └──────────────────┬───────────────────┘   └─────────────────┬─────────────────┘
                     │                                         │
                     ▼                                         ▼
  ┌──────────────────────────────────────────────────────────────────────────────┐
  │                      Pluggable Persistence Repositories                      │
  │                                                                              │
  │  FilesystemRepository (Default)           PostgresRepository (Production)    │
  │  • Single-process local dev               • Multi-worker OCC concurrency     │
  │  • Atomic writes via .tmp + replace       • Single-table hybrid JSONB schema │
  │  • backend_supports_event_history: false  • Transactional event_outbox table │
  └──────────────────────────────────────────────────┬───────────────────────────┘
                                                     │ polls pending events
                                                     ▼
                                     ┌───────────────────────────────────┐
                                     │        EventDeliveryWorker        │
                                     │ • At-least-once outbox delivery   │
                                     │ • Exponential backoff retries     │
                                     └───────────────────────────────────┘
```

- **Domain State**: Typed `InvestigationState` with integer `version` field for optimistic concurrency control (OCC).
- **Execution Leases**: Leases protect active investigations against concurrent worker collisions. Crashed workers' leases expire after TTL, allowing safe crash recovery.
- **Transactional Outbox**: Atomic database transactions commit state and domain events together in PostgreSQL.
- **Event Delivery Worker**: Background daemon ensuring reliable delivery to event subscribers with backoff retries.
- **REST & SSE API**: Full investigation lifecycle management, human approval recording, and live streaming via Server-Sent Events.

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
# Run complete test suite (mocked Groq client, zero API tokens consumed)
pytest
# Verified result: 609 passed, 32 skipped, 1 warning in ~13s
# (32 skipped: 30 PostgreSQL live integration tests skipped when SENTINEL_DATABASE_URL is unset;
#  2 Windows symlink tests skipped for non-admin accounts without Developer Mode)

# Run the incident dataset invariant suite
python tests/validate_incidents.py
# Verified result: 51/51 passed across all 10 canonical incidents
```

### Investigate one incident via CLI

```bash
# Runs full 8-stage Sentinel pipeline on inc_01 (N+1 query) — requires GROQ_API_KEY
python -m agents.orchestrator incidents/inc_01_n_plus_one_query
```

Output is a structured JSON result: evidence → hypotheses → CONFIRMED/REJECTED verdicts → fix proposals → approval gate.

---

## 10. Sentinel 2.0 API & Live Streaming

Sentinel 2.0 exposes a complete REST API and live SSE streaming interface served by Starlette and Uvicorn.

### Start the API Server

```bash
# Run server on http://127.0.0.1:8000
python -m api --host 127.0.0.1 --port 8000

# Or via Makefile
make serve
```

### Start the Event Delivery Worker (PostgreSQL Mode)

When configured with `SENTINEL_PERSISTENCE_BACKEND=postgres`, start the background outbox worker:

```bash
# Polls outbox and dispatches events reliably
python -m api.event_worker --interval 1.0

# Or via Makefile
make worker
```

### Core API Endpoints & Examples

#### 1. System Health Check
```bash
curl -s http://127.0.0.1:8000/health
# {"status":"ok","persistence":"filesystem","outbox":{"pending":0,"processed":0,"failed":0},"timestamp":"..."}
```

#### 2. Create & Run an Investigation
```bash
curl -s -X POST http://127.0.0.1:8000/investigations \
  -H "Content-Type: application/json" \
  -d '{"incident_id": "inc_01_n_plus_one_query"}'
```

#### 3. List Investigations
```bash
curl -s "http://127.0.0.1:8000/investigations?limit=10&offset=0"
```

#### 4. Retrieve Investigation Details
```bash
curl -s http://127.0.0.1:8000/investigations/<investigation_id>
```

#### 5. Resume an Investigation
```bash
# Resumes an interrupted or failed investigation from its last completed stage
# If an active unexpired lease is held by another worker, returns 409 Conflict
curl -s -X POST http://127.0.0.1:8000/investigations/<investigation_id>/resume
```

#### 6. Record Human Approval Decision
```bash
# Record an explicit human decision on generated fix proposals
# IMPORTANT: This records the decision ONLY; it NEVER modifies code files or applies patches
curl -s -X POST http://127.0.0.1:8000/investigations/<investigation_id>/approval \
  -H "Content-Type: application/json" \
  -d '{"decision": "APPROVED", "approver": "sre-oncall@example.com", "notes": "Verified against staging"}'
```

#### 7. Investigation Event History & Live SSE Streaming

The `GET /investigations/{id}/events` endpoint provides dual-mode event access via HTTP content negotiation:

- **JSON Snapshot (`Accept: application/json`)**:
  ```bash
  curl -s http://127.0.0.1:8000/investigations/<investigation_id>/events \
    -H "Accept: application/json"
  ```
  Returns `InvestigationEventsResponse`. On PostgreSQL, queries all events from `event_outbox` in deterministic order `(created_at ASC, event_id ASC)`. On Filesystem, returns `backend_supports_event_history: false`.

- **Live Server-Sent Events (`Accept: text/event-stream`)**:
  ```bash
  # Stream live investigation events from the beginning
  curl -N http://127.0.0.1:8000/investigations/<investigation_id>/events \
    -H "Accept: text/event-stream"

  # Resume stream from a specific event ID (keyset cursor)
  curl -N http://127.0.0.1:8000/investigations/<investigation_id>/events \
    -H "Accept: text/event-stream" \
    -H "Last-Event-ID: evt_01J9X..."
  ```
  Streams real-time events, periodically emits `: keep-alive\n\n` comments, safely drains all pending persisted events, and closes cleanly without emitting synthetic frames upon reaching a terminal state (`COMPLETED`, `FAILED`, `PARTIAL`). If an unresolvable cursor is supplied, the server returns HTTP 200, emits an SSE comment notice, and restarts streaming from the beginning (never failing with HTTP 400).

---

## 11. Persistence Backend Comparison

| Feature | Filesystem Backend (`filesystem`) | PostgreSQL Backend (`postgres`) |
|---|---|---|
| **Primary Use Case** | Local development, single-run CLI | Production, multi-worker deployments |
| **Storage Mechanism** | Atomic JSON files under `.sentinel/` | Relational + JSONB in PostgreSQL 16 |
| **Concurrency Control** | Process-local file writes | Transactional OCC (`version` column) |
| **Execution Leases** | Memory/file level | Distributed atomic leases with TTL crash recovery |
| **Event History** | `backend_supports_event_history: false` | Fully supported via durable `event_outbox` |
| **SSE Streaming** | In-memory during run only | Durable history replay + live streaming |
| **Event Worker** | Not applicable | Background daemon with retry backoff & deduplication |

---

## 12. Safety Invariants & Boundary Guarantees

1. **Zero Auto-Applied Patches**: Human approval records the human decision in `ApprovalRecord` **ONLY**. Sentinel never applies patches, runs shell commands to modify source, or executes unverified code.
2. **Ground Truth Isolation**: The Sentinel pipeline and API runtime **never** read `ground_truth.md` or baseline evaluation results. Only the post-run evaluation harness accesses ground truth.
3. **Secret Masking**: All exception handlers, logs, and telemetry sanitize API keys to prevent secret leakage.
4. **Deterministic Verification**: Hypotheses are falsified using pure Python AST analysis, log scanning, and invariant checking with zero LLM inference.

---

## 13. Documentation & Verification References

- **[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)** — Comprehensive architecture specification for Sentinel 1.0 (multi-agent verification engine) and Sentinel 2.0 (distributed state, OCC, outbox, leasing, and SSE).
- **[docs/CURRENT_STATE_AUDIT.md](docs/CURRENT_STATE_AUDIT.md)** — Exhaustive repository state audit, current test counts, verified capabilities, and unresolved items.
- **[CHANGELOG.md](CHANGELOG.md)** — Full chronological release history from initial skeleton through Sentinel 2.0 Mission 12C.
- **[REPRODUCE.md](REPRODUCE.md)** — Complete guide for setting up, running offline tests, launching the API server, querying endpoints, and executing benchmark evaluations.
- **[TODO.md](TODO.md)** — Roadmap milestone tracker documenting all completed missions and upcoming post-12C engineering work.
