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

---

## 3. Sentinel 2.0: Enterprise & Distributed Architecture

Sentinel 2.0 evolves the multi-agent reasoning engine from a single-run CLI tool into an enterprise-ready, event-driven, distributed investigation system with durable persistence, optimistic concurrency control, execution leases, transactional event delivery, and an asynchronous HTTP/SSE API layer.

```
┌────────────────────────────────────────────────────────────────────────────────────────┐
│                                   CLIENT LAYER                                         │
│         CLI Launcher / REST API Clients / Event Listeners / Web Browsers               │
└───────────────────────────────────────────┬────────────────────────────────────────────┘
                                            │ HTTP / SSE
                                            ▼
┌────────────────────────────────────────────────────────────────────────────────────────┐
│                              API SERVER (Starlette / Uvicorn)                          │
│  ├─ POST /investigations               ├─ POST /investigations/{id}/resume             │
│  ├─ GET  /investigations               ├─ POST /investigations/{id}/approval           │
│  ├─ GET  /investigations/{id}          └─ GET  /investigations/{id}/events             │
│  │                                        (JSON Snapshot & SSE Live Stream)            │
│  └────────────────────────────────────────┬────────────────────────────────────────────┘
                                            │
                                            ▼
┌────────────────────────────────────────────────────────────────────────────────────────┐
│                         INCIDENT ORCHESTRATOR (Sentinel Core)                          │
│  Coordinates 8-stage lifecycle:                                                        │
│  Logs → Metrics → Code → Evidence Fusion → Hypotheses → Verification → Proposals → Gate│
│                                                                                        │
│  Lifecycle Contract: State Mutation ──► Persistence ──► Event Publication              │
└─────────────────────────────┬───────────────────────────────┬──────────────────────────┘
                              │                               │
                              ▼                               ▼
       ┌───────────────────────────────┐      ┌────────────────────────────────┐
       │     InvestigationState        │      │       Domain Event Bus         │
       │  • Typed Pydantic V2 model    │      │  • Synchronous in-memory bus   │
       │  • Versioning (OCC `version`) │      │  • 8 immutable domain events   │
       │  • Execution lease attributes │      │  • Subscriber isolation        │
       └──────────────┬────────────────┘      └────────────────────────────────┘
                      │                                       ▲
                      ▼                                       │ delivers
┌─────────────────────────────────────────────────────────────┼──────────────────────────┐
│                   PERSISTENCE LAYER (core/persistence/)     │                          │
│                                                             │                          │
│  ┌───────────────────────────────┐     ┌────────────────────┴───────────────────────┐  │
│  │ FilesystemRepository          │     │ PostgresRepository                         │  │
│  │ • Atomic writes (.tmp+rename) │     │ • Single-table hybrid schema               │  │
│  │ • Single-process only         │     │ • Optimistic Concurrency Control (OCC)     │  │
│  │ • No outbox / no event history│     │ • Transactional Outbox (event_outbox table)│  │
│  └───────────────────────────────┘     └────────────────────┬───────────────────────┘  │
└─────────────────────────────────────────────────────────────┼──────────────────────────┘
                                                              │ polls outbox
                                                              ▼
                                              ┌────────────────────────────────┐
                                              │      EventDeliveryWorker       │
                                              │ • Atomic SKIP LOCKED claims    │
                                              │ • At-least-once outbox delivery│
                                              │ • Durable DB-scheduled retries │
                                              │ • Claim-expiry crash recovery  │
                                              │ • Deduplication by event_id    │
                                              └────────────────────────────────┘
```

### 3.1 Domain Model & InvestigationState (`core/domain/`)

The core unit of state is `InvestigationState` (`core/domain/state.py`), a typed Pydantic V2 model representing the full lifecycle and findings of an incident investigation:
- **Identity & Context**: `investigation_id`, `incident_id`, `incident_dir`.
- **Status & Progression**: `status: InvestigationStatus` (`PENDING`, `RUNNING`, `COMPLETED`, `PARTIAL`, `FAILED`), `current_stage: Optional[PipelineStage]` (8 stages).
- **Concurrency & Leases**: `version: int = 1` for OCC; `lease_owner: Optional[str]`, `lease_expires_at: Optional[datetime]`.
- **Aggregated Artifacts**: `evidence: List[EvidenceItem]`, `hypotheses: List[Hypothesis]`, `verification_results: List[VerificationResult]`, `fix_proposals: List[FixProposal]`, `approvals: List[ApprovalRecord]`.
- **Audit Trails**: `transitions: List[StageTransition]` recording every stage progression with timestamps, plus cumulative token and call `telemetry`.
- **Bidirectional Serialization**: Strict `.to_dict()` and `.from_dict()` methods ensuring complete fidelity across storage layers.

### 3.2 Investigation Lifecycle & Orchestrator Responsibilities

The `IncidentOrchestrator` (`agents/orchestrator.py`) drives the 8-stage pipeline:
1. `logs`: Observational log scanning (`LogsAgent`).
2. `metrics`: Metric anomaly and spike detection (`MetricsAgent`).
3. `code`: Static code and git diff inspection (`CodeAgent`).
4. `evidence_fusion`: Deterministic cross-evidence consolidation (0 LLM calls).
5. `hypotheses`: Formulating 1–4 competing falsifiable hypotheses (`HypothesisEngine`, 1 LLM call).
6. `verification`: Deterministic falsification checks via AST/metrics/logs (`VerificationAgent`, 0 LLM calls).
7. `fix_proposals`: Synthesizing targeted unified diffs for CONFIRMED hypotheses only (`FixProposalAgent`).
8. `approvals`: Human approval recording gate (`ApprovalGate`).

**Strict Lifecycle Invariant**: At every stage transition, the orchestrator enforces:
$$\text{State Mutation} \longrightarrow \text{Durable Persistence} \longrightarrow \text{Event Publication}$$
If persistence fails, the in-memory mutation is not published and downstream stages halt. On cache hits, only `StageCached` is emitted—never `StageStarted` or `StageCompleted`.

### 3.3 Persistence Implementations & Optimistic Concurrency Control

Sentinel 2.0 provides pluggable persistence through `PersistenceRepository` (`core/persistence/repository.py`):

1. **FilesystemRepository (`core/persistence/filesystem.py`)**:
   - Atomic file persistence using temporary write files (`.tmp`) and `os.replace`.
   - Path-safety assertions blocking directory traversal, symlink escapes, benchmark test leakage, and sensitive API keys.
   - **Backend Capabilities & Boundaries**: Intended for local single-process development. Does not provide cross-process locking or OCC version checking. Does not store outbox events; reports `backend_supports_event_history = False`.

2. **PostgresRepository (`core/persistence/postgres.py`)**:
   - High-performance connection pooling via `psycopg-pool` and `psycopg 3`.
   - **Hybrid Relational/Document Storage**: Operational fields (`status`, `current_stage`, `version`, `started_at`, `lease_owner`, `lease_expires_at`) are stored in indexed relational columns of the `investigations` table, while the comprehensive domain structure resides in `state_payload JSONB`.
   - **Optimistic Concurrency Control (OCC)**: Every state update asserts `WHERE investigation_id = %s AND version = %s` while incrementing `version = version + 1`. If a concurrent worker modifies the record, zero rows are affected and a typed `ConcurrencyError` is raised immediately.
   - Reports `backend_supports_event_history = True`.

### 3.4 Transactional Outbox & Background Event Delivery

To avoid dual-write inconsistencies between database persistence and event emission:
- **Atomic Outbox Persistence**: When `PostgresRepository.save(state, events)` is called, state mutation and pending domain events are written to `investigations` and `event_outbox` (`core/persistence/sql/002_outbox_schema.sql`) inside the **same database transaction**. Duplicate event IDs are handled with `INSERT ... ON CONFLICT (event_id) DO NOTHING`, so a duplicate can never abort an otherwise valid state change.
- **Outbox Schema**: `event_outbox` captures `event_id` (VARCHAR, PRIMARY KEY), `investigation_id`, `event_type`, `payload JSONB`, `status`, `created_at`, `processed_at`, `retry_count`, and `error_message`. The additive Mission 16 migration (`003_outbox_delivery_claim.sql`) adds claim state: `claimed_by`, `claimed_at`, `claim_expires_at`, and `next_attempt_at`, plus the `idx_event_outbox_claim` index.
- **Atomic Claiming (Mission 16)**: `PostgresRepository.claim_pending(worker_id, limit, claim_ttl_seconds)` locks eligible rows in canonical `(created_at ASC, event_id ASC)` order with `SELECT ... FOR UPDATE SKIP LOCKED`, then claims exactly those rows. Concurrent worker processes therefore claim **disjoint** event sets; locks are held until commit.
- **EventDeliveryWorker (`api/event_worker/worker.py`)**: A standalone background worker runs the claim → deliver → finalise cycle. Each poll first calls `release_expired_claims()` (crash recovery), then `claim_pending()`, then delivers each event and records the outcome with `mark_processed()`, `schedule_retry()`, or `mark_failed()`. Retries use durable, database-scheduled bounded exponential backoff with full jitter — **no sleeping occurs inside the batch loop**.
- **Visibility Timeout / Crash Recovery**: Each claim carries a `claim_expires_at` (default TTL 300 s). If a worker crashes mid-delivery, `release_expired_claims()` returns the abandoned claim to `pending` so another worker can reclaim it. Completed (`processed`) and terminally `failed` events are never reclaimed.
- **Delivery Guarantee**: durable, concurrency-safe claims with **at-least-once** delivery. Because the destination is an in-process `EventBus`, exactly-once **external** delivery is **not** claimed; consumers must be idempotent and key on `event_id`.
- **Observational query**: `fetch_pending()` remains available for auditing and event-history reads but does **not** claim or mutate events; it must not drive delivery.

### 3.5 Execution Leasing & Crash Recovery

To prevent multiple server workers from executing or resuming the same investigation simultaneously:
- **Lease Attributes**: `lease_owner` (unique worker/instance token) and `lease_expires_at` (UTC expiry timestamp).
- **Lease Acquisition & Renewal**: Workers acquire leases with a configurable TTL (e.g., 300 seconds). In PostgreSQL, acquisition is guarded by OCC:
  ```sql
  UPDATE investigations
  SET lease_owner = %s, lease_expires_at = %s, version = version + 1
  WHERE investigation_id = %s AND version = %s
    AND (lease_expires_at IS NULL OR lease_expires_at < NOW())
  ```
- **Active Lease Protection**: While `is_lease_active()` evaluates to true, competing requests to resume or acquire receive `409 Conflict`.
- **Crash Recovery**: If a worker process terminates abruptly while an investigation is `RUNNING`, the lease naturally expires once `now >= lease_expires_at`. Subsequent workers or API resume requests detect that the lease is expired, claim ownership via OCC, restore the persisted state, and resume execution from the first uncompleted pipeline stage without re-running finished stages.

### 3.6 HTTP API Architecture & Route Contracts

The Sentinel 2.0 API is built on Starlette and served via Uvicorn (`api/app.py`, `api/routes.py`, `api/models.py`):
- `GET /health`: Returns service health, persistence backend in use (`filesystem` or `postgres`), and outbox backlog status.
- `POST /investigations`: Initiates an investigation given an `incident_id`. Validates input paths.
  - **Synchronous Mode (Default)**: Blocks until pipeline completes, returning `200 OK` with full investigation state.
  - **Asynchronous Mode (`Prefer: respond-async` or `?async=true`)**: Validates input, persists initial `RUNNING` state with lease, schedules execution in a background task, and returns `202 Accepted` with `AcceptedInvestigationResponse` (`investigation_id`, `status: RUNNING`, `message`, `status_url`, `events_url`) and `Location` header.
- `GET /investigations`: Lists investigations with pagination (`limit`, `offset`) and optional status filters.
- `GET /investigations/{id}`: Retrieves complete state and summary for a single investigation (`200 OK` or `404 Not Found`). Immediately queryable following 202 acceptance.
- `POST /investigations/{id}/resume`: Re-enters an interrupted or incomplete investigation (`PENDING`, `PARTIAL`, `FAILED`, or `RUNNING` with an expired lease). Rejects active unexpired leases with `409 Conflict`.
  - Supports synchronous execution (`200 OK`) and opt-in async execution (`202 Accepted` with `Prefer: respond-async` or `?async=true`).
- `POST /investigations/{id}/approval`: Records a human decision (`decision: APPROVED | REJECTED`, `approver`, `notes`).
  > **Crucial Safety Rule**: Approval records the human decision in `ApprovalRecord` **ONLY**. It **NEVER** applies code patches to disk or modifies git state.
- `GET /investigations/{id}/events`: Content-negotiated event retrieval endpoint supporting JSON snapshots and live SSE streaming.

#### Asynchronous Dispatch Lifecycle & Limitations
1. **Durable Ownership & Persistence Pre-Condition (Before `202 Accepted`)**:
   - **Async Creation**: Sentinel atomically registers a process-local slot, generates a unique worker identifier (`worker_id`), acquires an execution lease on `initial_state`, and durably persists `status = RUNNING` before returning `202 Accepted`. The exact same `worker_id` is passed directly to the background `IncidentOrchestrator`, ensuring continuous agreement across persisted lease, active registry, and executing worker with zero handoff race window.
   - **Async Resume**: Sentinel validates resumability, acquires durable execution ownership in the repository under PostgreSQL optimistic concurrency control (OCC via `version` token assertion), transitions state to `RUNNING`, and saves the updated lease before returning `202 Accepted`. Competing resume requests across processes on PostgreSQL encounter `ConcurrencyError` and return `409 Conflict`, never `202`. If persistence or lease acquisition fails, the process-local registry slot is immediately cleared and an error response (`409`, `503`, or `500`) is returned.
   - **Immediate Queryability & Streaming**: Because durable ownership and state are committed before the response, clients can immediately query `status_url` (`GET /investigations/{id}`) and connect to `events_url` (`GET /investigations/{id}/events` via SSE) without race conditions.
2. **Background Execution**: Investigation work executes via Starlette `BackgroundTask` running within the server thread pool under the matching worker ID, preserving all previously completed stages. Process-local registry slots are released in a strict `finally` block upon completion or failure.
3. **Failure State Transition**: Unhandled exceptions in background tasks persist `status = FAILED` and release the execution lease, preventing orphaned `RUNNING` states.
4. **Process-Exit Boundary & Backend Concurrency Guarantees**:
   - In-process background tasks do not constitute an external distributed task queue. If the API process terminates unexpectedly during execution, the investigation lease naturally expires after its TTL (default 300s). Once expired, the investigation can be safely resumed via `POST /investigations/{id}/resume`.
   - **PostgreSQL**: Guarantees cross-process mutual exclusion via row-level locks and OCC `version` increments on lease acquisition and state save.
   - **Filesystem**: Guarantees atomic file writes and in-process mutual exclusion via threading locks, but cannot provide cross-process OCC race protection.

### 3.7 JSON Event History Snapshot & Live SSE Streaming

The event history endpoint (`GET /investigations/{id}/events`) provides two distinct modes based on the `Accept` header:

#### 1. JSON History Snapshot (`Accept: application/json`)
- Returns `InvestigationEventsResponse`: `investigation_id`, `total_events`, `backend_supports_event_history`, and `events: [...]`.
- On PostgreSQL, queries all events from `event_outbox` in deterministic order: `(created_at ASC, event_id ASC)`.
- On Filesystem, returns `200 OK` with `backend_supports_event_history: false` and an empty event array, truthfully reflecting that filesystem storage lacks a durable event log.

#### 2. Live Server-Sent Events (SSE) (`Accept: text/event-stream`)
- Emits real-time SSE frames using Starlette `StreamingResponse`.
- **Frame Format**:
  ```
  id: evt_01J9X...
  event: StageCompleted
  data: {"event_id": "evt_...", "stage": "verification", ...}
  ```
- **Cursor Ordering & Pagination**: Clients provide a cursor via `Last-Event-ID` header or `?last_event_id` query parameter. Cursors are resolved against persisted history. If an unresolvable cursor is supplied, the server does not fail with HTTP 400; instead, it returns HTTP 200, emits an informative SSE comment notice (`: notice: cursor '{last_event_id}' could not be resolved; delivering from beginning\n\n`), and restarts delivery from the beginning so that no events are omitted.
- Keyset pagination strictly guarantees `(created_at ASC, event_id ASC)` ordering across 200-event chunks without in-memory buffering or duplicate delivery.
- **Live Polling & Reconnection**: After historical events are caught up, the generator polls for new events at `poll_interval` (default 0.5s). Periodic keepalives (`: keep-alive\n\n`) prevent proxy timeouts.
- **Terminal Draining**: When an investigation reaches a terminal state (`COMPLETED`, `FAILED`, `PARTIAL`), the stream drains all pending persisted events before closing the connection cleanly. No synthetic `event: done` frame is emitted; the stream is concluded cleanly after delivering all persisted domain events.
- **Disconnect Safety**: Regularly checks client socket connection (`request.is_disconnected()`) to abort generator loops without leaking database pool connections.

### 3.8 Process-Local vs. Durable Behaviour Matrix

| Capability | Filesystem Backend (`SENTINEL_PERSISTENCE_BACKEND=filesystem`) | PostgreSQL Backend (`SENTINEL_PERSISTENCE_BACKEND=postgres`) |
|---|---|---|
| **Storage Substrate** | Local JSON files in `.sentinel/` | PostgreSQL 16 `investigations` & `event_outbox` |
| **Concurrency Safety** | Process-local file operations only | Transactional Optimistic Concurrency Control (`version`) |
| **Multi-Worker API** | Unsafe (risk of overwritten state files) | Safe (OCC blocks conflicting concurrent writes) |
| **Execution Leases** | Memory/file level; no distributed lock | Atomic OCC lease claims with lease expiry recovery |
| **Event Outbox** | Not supported | Transactional atomic state + event insertion |
| **Event Delivery Worker**| In-memory direct publish only | Dedicated background worker: atomic multi-worker claims, durable DB-scheduled retry backoff, claim-expiry crash recovery (Mission 16) |
| **Event History API** | `backend_supports_event_history = false` | Fully supported via durable `event_outbox` queries |
| **SSE Streaming** | Streams in-memory events during active run | Replays durable history + live streams from outbox |
| **Crash Recovery** | Manual resume; risk of unexpired lease block | Automatic crash recovery after TTL expiry via OCC |

---

## 4. CI/CD Architecture (Mission 14 Phase 4)

### 4.1 GitHub Actions Workflow

The `.github/workflows/ci.yml` workflow runs on every push to `main` and every pull request. It enforces both offline correctness and live PostgreSQL integration.

```
┌────────────────────────────────────────────────────────────────┐
│  Push / Pull Request                                           │
└───────────────────────┬────────────────────────────────────────┘
                        │
          ┌─────────────┴──────────────┐
          │                            │
          ▼                            ▼
┌─────────────────┐          ┌─────────────────────────────┐
│  offline job    │          │  postgres job               │
│  (ubuntu)       │          │  (ubuntu + postgres:16)     │
│                 │          │                             │
│  pip install    │          │  pip install                │
│  pytest         │          │  wait for pg_isready        │
│  (no DB)        │          │  schema apply (CLI)         │
│  validate_inc.  │          │  schema verify (CLI)        │
│  git diff       │          │  pytest (full suite + DB)   │
│  --check        │          │  assert skips ≤ 2           │
│                 │          │  validate_incidents.py      │
│  Verified:      │          │  git diff --check           │
│  654p, 4s, 1w   │          │                             │
└─────────────────┘          │  Verified (Linux + PG):     │
                             │  743 passed, 0 skipped      │
                             └─────────────────────────────┘
```

**Key properties:**
- **Truthful**: The `postgres` job fails if more than 2 tests skip on Linux CI; this enforces that PostgreSQL integration tests actually execute rather than silently skip. Skip count is parsed from `--junitxml` output — no redundant second test run.
- **Schema ownership**: The CI runner invokes `python -m core.persistence.schema apply` (not Docker entrypoint scripts), ensuring the same authoritative migration runner is used in CI and locally.
- **Offline exclusions**: The `offline` job excludes exactly four PostgreSQL-only modules. `test_persistence_contract.py` is intentionally **not** excluded because it contains only offline `TestFilesystemRepositoryContract` tests with no database dependency. `tests/test_pg_retry.py` is also **not** excluded: its 21 offline retry/wiring tests run in the offline job, while its 4 live integration tests skip gracefully and execute in the `postgres` job.
- **Symlink note**: The 2 Windows-symlink tests use a runtime probe (`_can_symlink()`) that attempts `Path.symlink_to()` in a temp directory. On Linux this succeeds without elevated privileges, so both tests execute and pass. Expected skip count on the `postgres` CI job: 0, with ≤2 as a conservative margin.
- **Mission 16 test delta (verified)**: The 4 new offline worker tests raise the `offline` job to 654 passed / 4 skipped, and the 6 new live outbox tests raise the `postgres` job to 743 passed / 0 skipped — both confirmed in [run 39277794918](https://github.com/groot34/sentinel/actions/runs/39277794918) (commit `aba70e1`); the diagram above shows that verified Mission 16 run.
- **Meaningful `git diff --check`**: Uses `fetch-depth: 0` so full history is available for PR base SHAs and HEAD~1 on pushes. On push events, checks `HEAD~1..HEAD`. On pull-request events, checks `${{ github.event.pull_request.base.sha }}...HEAD`. This validates the whitespace hygiene of actual committed changes, not just the always-clean CI working tree.

### 4.2 Schema Migration CLI

The authoritative migration runner (`core.persistence.schema`) has three sub-commands used in CI and locally:

| Command | Action |
|---|---|
| `python -m core.persistence.schema apply --url <url>` | Discover and apply pending migrations; verify schema on completion |
| `python -m core.persistence.schema verify --url <url>` | Verify all required tables, columns, and operational indexes |
| `python -m core.persistence.schema status --url <url>` | List applied migration versions from `schema_migrations` table |

### 4.3 Developer Workflow (Makefile Targets)

| Target | Command | Description |
|---|---|---|
| `make db-up` | `docker compose up -d postgres` + health poll | Start PostgreSQL 16 container |
| `make db-down` | `docker compose stop postgres` | Stop container; volume preserved |
| `make db-migrate` | `schema apply` + `schema verify` | Bootstrap or migrate schema |
| `make db-status` | `schema status` | Show applied migration versions |
| `make test-postgres` | `pytest` on 6 PG test modules | Run live integration tests only |
| `make test-all` | Full `pytest` + incident validation | Complete suite with PostgreSQL |
| `make test` | `pytest tests/` | Standard offline unit suite |

### 4.4 Transient PostgreSQL Failure Recovery (Mission 15)

**Retry boundary**: retry is applied only inside `PostgresRepository.save()` — the
single method that wraps **one complete transaction** (row lock
`SELECT ... FOR UPDATE`, state write, and atomic outbox inserts). The extracted
`_save_transaction()` performs only database work, so re-running the whole
transaction cannot repeat external side effects. No generic retry wrapper is
applied to arbitrary repository methods.

| Error | SQLSTATE | Behaviour |
|---|---|---|
| `serialization_failure` | `40001` | Retried — entire transaction, fresh connection per attempt |
| `deadlock_detected` | `40P01` | Retried — the whole transaction is re-run from a fresh connection, so safe here |
| OCC conflict (`ConcurrencyError`) | — | **Never retried**; propagates as `409 Conflict` |
| Constraint violations | `23505`/`23503`/… | **Never retried**; wrapped as `PersistenceError` |
| Validation / safety failures | — | **Never retried** |

**Isolation caveat**: Sentinel sets no explicit isolation level, so the server
default (**READ COMMITTED**) applies. Under READ COMMITTED, `40001` is not
naturally produced by row-lock contention (which surfaces as OCC
`ConcurrencyError`); `40P01` deadlocks can occur. This module therefore does not
claim serialisable isolation or phantom-read prevention — an explicit
`SERIALIZABLE` level was deliberately not introduced.

**Configuration (conservative defaults)**:

| Env var | Default | Meaning |
|---|---|---|
| `SENTINEL_PG_RETRY_MAX_ATTEMPTS` | `5` | Total attempts (≥ 1) |
| `SENTINEL_PG_RETRY_BASE_DELAY` | `0.05` s | Exponential backoff base |
| `SENTINEL_PG_RETRY_MAX_DELAY` | `1.0` s | Backoff ceiling (≤ 30 s) |

Backoff is exponential with full jitter (uniform in `[0, ceiling]`), bounded by
`max_delay_seconds`, to avoid synchronized retry storms. Malformed/out-of-range
env values are clamped, never crash repository construction.

**State integrity across attempts**: each attempt restores `state.version` to the
captured entry version before acquiring the lock, so retries never
double-increment the OCC token; outbox events are inserted only within the
committed attempt, so no duplicate events are persisted.

**Exhaustion**: when the budget is exhausted, `RetryExhaustedError` (subclass of
`PersistenceError`) is raised, chaining the last transient error. The existing API
handler maps it to **`503 persistence_unavailable`**. The in-memory state version
is restored on exhaustion.

**Testing seam**: because READ COMMITTED does not naturally emit `40001`, a
deterministic, test-only fault-injection seam
(`core.persistence.retry.inject_transient_fault`) raises synthetic
`SerializationFailure` at the transaction boundary. It is activated only by test
code; no hook is installed in production, so `maybe_inject_fault` is a no-op there.

> **Status**: Implemented and validated. The 21 offline retry tests pass locally;
> the 4 live PostgreSQL 16 integration tests executed and passed in CI's `postgres`
> job ([run 39048469043](https://github.com/groot34/sentinel/actions/runs/39048469043),
> commit `fa4f1c7` — 733 passed, 0 skipped). Live validation was performed in CI only
> (no local PostgreSQL instance was available).

### 4.5 Concurrent & Crash-Safe Outbox Delivery (Mission 16)

**Problem addressed**: the pre-Mission-16 worker read pending rows (a
non-claiming `SELECT`) and then delivered them. Two worker processes could read
the same batch, failed deliveries slept in-process (losing retry timing on
restart), and a swallowed `UniqueViolation` left the transaction aborted so a
valid state change could not commit.

**Schema** — `core/persistence/sql/003_outbox_delivery_claim.sql` (additive and
idempotent; `ADD COLUMN IF NOT EXISTS` / `CREATE INDEX IF NOT EXISTS`):

| Column | Type | Purpose |
|---|---|---|
| `claimed_by` | `VARCHAR(128)` | Worker identity holding the current claim |
| `claimed_at` | `TIMESTAMPTZ` | When the claim was taken |
| `claim_expires_at` | `TIMESTAMPTZ` | Visibility timeout for the claim |
| `next_attempt_at` | `TIMESTAMPTZ NOT NULL DEFAULT NOW()` | Earliest claimable time (durable retry scheduling) |

Plus index `idx_event_outbox_claim (status, next_attempt_at, created_at)`.

**Atomic claiming** — `PostgresRepository.claim_pending()`:

1. `SELECT event_id ... WHERE status = 'pending' AND next_attempt_at <= NOW() ORDER BY created_at ASC, event_id ASC LIMIT %s FOR UPDATE SKIP LOCKED`.
2. `UPDATE event_outbox SET status = 'processing', claimed_by = …, claimed_at = NOW(), claim_expires_at = NOW() + make_interval(secs => %s) WHERE event_id = ANY(%s::text[]) RETURNING …`.

`FOR UPDATE SKIP LOCKED` makes concurrent claimers skip rows locked by another
in-flight claim, so two workers never own the same event. `UPDATE ... RETURNING`
does **not** guarantee row order, so the deterministic `(created_at, event_id)`
order is restored from the locked id list.

**Durable retry & crash recovery** — `schedule_retry()` persists
`next_attempt_at`, increments `retry_count`, records `error_message`, and clears
the claim; `release_expired_claims()` returns claims past `claim_expires_at` to
`pending`. `mark_processed()` / `mark_failed()` clear the claim. The worker never
sleeps inside the batch loop; backoff is bounded exponential with full jitter
(defaults `base=2.0s`, `max=60.0s`), computed from an injectable clock and rng.

**Guarantee** — durable, concurrency-safe claims with **at-least-once** delivery.
Because the destination is an in-process `EventBus`, exactly-once **external**
delivery is **not** claimed; consumers must be idempotent and key on `event_id`.

**Tests** — 4 new offline worker tests (`tests/test_event_worker.py`, now 11) and
6 new live PostgreSQL integration tests (`tests/test_outbox.py`): disjoint
concurrent claims, stale-claim recovery, future-retry and completed-event
exclusion, deterministic ordering for tied timestamps, duplicate-ID
non-abortion, and terminal failure retention.

> **Status**: validated. GitHub Actions [run 39277794918](https://github.com/groot34/sentinel/actions/runs/39277794918)
> (commit `aba70e1`) passed both jobs — `offline` 654 passed / 4 skipped / 1 warning and
> `postgres` (PostgreSQL 16) 743 passed / 0 skipped / 3 warnings (`Total skipped: 0`), so all
> 6 live outbox tests executed; migrations `001`–`003` applied and schema verification passed;
> incident validation 51/51 in both jobs. Locally (Windows, no database): full suite 695 passed /
> 48 skipped / 1 warning; offline-job subset 652 passed / 6 skipped; incident validation 51/51.
