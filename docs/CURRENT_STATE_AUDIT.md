# Sentinel Repository State Audit

**Audit Date**: 2026-10-19<br>
**Branch**: `main`<br>
**Base Commit**: `1d55db8` (*docs: complete Mission 15 validation status*)<br>
**Working Tree Status**: Mission 16 changes present in the working tree (uncommitted and unpushed); see §5/§6

---

## 1. Executive Summary & Verification Baseline

This audit establishes the verified state of the Sentinel repository following the implementation and verification of Sentinel 2.0 Mission 14, Phases 2–4 (PostgreSQL Reliability Harness: Schema Bootstrapping, Live Concurrency Tests, and CI/Developer Workflow), Mission 15 (PostgreSQL Serialization Failure Recovery), whose live PostgreSQL behaviour is validated in CI, and Mission 16 (Concurrent & Crash-Safe Outbox Delivery), whose implementation is complete and locally tested but whose live PostgreSQL validation is pending CI. All findings are derived directly from the active codebase, automated test executions, and verifiable Git history.

### Current Test Suite & Verification Results

- **Pytest Suite (offline, local Windows / Docker unavailable, Mission 16)**: **695 passed, 48 skipped, 1 warning** (743 collected in ~13 s).
- **Pytest Suite (GitHub Actions [run 39048469043](https://github.com/groot34/sentinel/actions/runs/39048469043), commit `fa4f1c7`, Mission 15)**: `postgres` job (PostgreSQL 16) **733 passed, 0 skipped, 3 warnings**; `offline` job **650 passed, 4 skipped, 1 warning**. Mission 16 adds 10 tests (4 offline worker + 6 live outbox); its live PostgreSQL run is pending.
- **Incident Dataset Integrity Suite** (`tests/validate_incidents.py`): **51 / 51 passed** (100%) — run in both CI jobs.
- **`git diff --check`**: Exit code 0 (clean — no trailing whitespace; only benign LF→CRLF notices on Windows).

#### Explanation of Skipped Tests & Warnings
1. **Windows Symlink Security Tests (2 skipped)**: `tests/test_api.py::test_create_investigation_rejects_symlink_inside_root` and `test_create_investigation_rejects_symlink_escaping_root` are skipped because standard Windows user accounts without Developer Mode cannot create filesystem symbolic links. These tests **pass on Linux CI** (symlink creation does not require elevated privileges), so CI reports 0 or fewer skips for these.
2. **PostgreSQL Live Integration Tests (46 skipped locally)**: All guarded by `@pytest.mark.skipif(not POSTGRES_AVAILABLE, ...)` where `POSTGRES_AVAILABLE` probes `SENTINEL_DATABASE_URL` at test collection time. Skipped locally because Docker Desktop is not running on the development machine. Exact breakdown:
   - 4 in `tests/test_postgres_concurrency.py` (OCC competing writes, expired lease race, concurrent async resume, outbox rollback)
   - 8 in `tests/test_outbox.py` (6 from Mission 16: concurrent disjoint claims, stale-claim recovery, future-retry/completed exclusion, tied-timestamp ordering, duplicate-ID non-abortion, terminal-failure retention; plus 2 Mission 11/14: PostgreSQL outbox uniqueness + atomic persistence)
   - 2 in `tests/test_schema_migration.py` (live bootstrap and repeat idempotency)
   - 28 in `tests/test_postgres_persistence.py` (18 contract tests inherited from `PersistenceContractTests` + 10 PostgreSQL-specific integration tests)
   - 4 in `tests/test_pg_retry.py` (Mission 15: injected `40001` then real commit; exhausted retries leave no partial state; OCC conflict distinct from retryable error; state + outbox atomic under retry)
   - All 46 execute on CI; the `postgres` job reports **0 skipped** (verified in run 39048469043 for the 40 Mission-15-era tests; the 6 Mission 16 tests await CI).
3. **Deprecation Warning (1 warning)**: Starlette deprecation notice in `tests/test_api.py` regarding `httpx` integration with `starlette.testclient.TestClient`.

> **Important**: Live PostgreSQL validation for Mission 15 is complete. GitHub Actions [run 39048469043](https://github.com/groot34/sentinel/actions/runs/39048469043) (commit `fa4f1c7`) passed both jobs: the `postgres` job provisioned the `postgres:16-alpine` container, applied the schema, and reported **733 passed / 0 skipped** — including all 40 PostgreSQL integration tests and Mission 15's 4 retry tests. The docs commit `1d55db8` also passed green in [run 39049346329](https://github.com/groot34/sentinel/actions/runs/39049346329) with identical results; both runs are genuine and mutually consistent. Mission 16's live PostgreSQL validation is **pending CI** (Docker is unavailable on the local development machine).

---

## 2. Implemented Capabilities: Sentinel 1.0 vs. Sentinel 2.0

### Sentinel 1.0: Deterministic Multi-Agent Verification Pipeline
Sentinel 1.0 established the multi-agent incident diagnosis and verification framework:
- **8-Stage Pipeline**: Logs Agent, Metrics Agent, Code Agent, Evidence Fusion, Hypothesis Engine, Verification Agent, Fix Proposal Agent, and Human Approval Gate coordinated by `agents/orchestrator.py`.
- **Deterministic Falsification**: The Verification Agent executes zero LLM calls, using pure Python AST analysis, log scanning, metric anomaly checks, and invariant evaluation to confirm or reject hypotheses.
- **Human Approval Safety Boundary**: The Approval Gate enforces a strict state machine (`PROPOSED → PENDING_APPROVAL → APPROVED | REJECTED`). Non-interactive runs auto-reject by default. Approval records the decision ONLY; patches are never automatically applied.
- **Benchmark Performance (Milestone 10)**:
  - Evaluated against a single-shot baseline across 10 canonical incidents using locked `openai/gpt-oss-120b` (Groq API).
  - Diagnostic Accuracy: Baseline 10/10 (100%), Sentinel 10/10 (100%).
  - Verified Root Causes: Baseline 0/10 (0%), Sentinel 10/10 (100%).
  - Hypotheses: 31 generated, 30 CONFIRMED, 1 REJECTED.
  - LLM Calls: 58 benchmark calls across all 10 incidents.
  - **Documented Incident 10 Limitation**: In `inc_10_multi_symptom_cascade`, diagnosis and verification succeeded (4 CONFIRMED hypotheses), but the fix-proposal stage was rate-limited on the Groq free-tier daily token limit, yielding 0 fix proposals with an overall pipeline status of `PARTIAL`.

### Sentinel 2.0: Enterprise Distributed & Event-Driven Architecture
Sentinel 2.0 evolved the single-run CLI into an enterprise-grade, durable, distributed investigation platform across Missions 01 through 15:

| Mission | Commit | Date | Core Deliverables & Implemented Modules |
|---|---|---|---|
| **Mission 01** | `888c35f` | 2026-09-05 | `InvestigationState`, typed domain models (`core/domain/models.py`), bidirectional `.to_dict()`/`.from_dict()` serialization. |
| **Mission 02** | `cc1e0c8` | 2026-09-07 | Orchestrator migration to `InvestigationState`; stage lifecycle tracking (`start_stage`, `complete_stage`, etc.), token telemetry propagation. |
| **Mission 03** | `72d9983` | 2026-09-09 | `PersistenceRepository` ABC, `FilesystemRepository` with atomic `.tmp` + `os.replace` writes and safety validations, `orchestrator.resume()`. |
| **Mission 04** | `10b4e30` | 2026-09-10 | Domain Event Infrastructure (`core/events/`): 8 immutable events, `InMemoryEventBus`, `EventRecorder`. Enforced invariant: State Mutation → Persistence → Event Emission. |
| **Mission 05** | `db7dd83` | 2026-09-12 | `PostgresRepository` (`core/persistence/postgres.py`), hybrid single-table schema (`001_initial_schema.sql`), OCC with integer `version`, repository factory. |
| **Mission 06** | `9c28339` | 2026-09-13 | Tool Abstraction Layer (`core/tools/`): ToolRegistry, tool models, execution context, path safety, and builtin log/metric/code tools. |
| **Mission 07** | `31baaeb` | 2026-09-17 | LLM Provider Abstraction Layer (`core/llm/`): BaseLLMClient interface, GroqLLMClient, factory, model configs, token telemetry. |
| **Mission 08** | `c6f6926` | 2026-09-17 | API layer (`api/app.py`, `api/routes.py`, `api/models.py`, `api/errors.py`) with Starlette: `POST /investigations`, `GET /investigations`, `GET /investigations/{id}`. |
| **Mission 09** | `d7a7d96`, `59f9100` | 2026-09-19 | CLI server entry point (`api/__main__.py` in `d7a7d96`); `Makefile` launcher targets (`make serve`, `make worker`) and documentation in `REPRODUCE.md` (`59f9100`). |
| **Mission 10** | `c3a4db5`, `c58c9de` | 2026-09-19 | Investigation Resume & Human Approval API: route mounting in `api/app.py` (`c3a4db5`); endpoint handlers (`api/routes.py`), request models (`api/models.py`), domain state methods (`core/domain/state.py`), and documentation in `c58c9de`. |
| **Mission 11** | `a7291bc` | 2026-09-30 | Transactional Outbox & Event Delivery: `002_outbox_schema.sql`, atomic state+event transactions, `OutboxRepository`, `EventDeliveryWorker` background service (`api/event_worker/`). |
| **Mission 12A**| `7ce43bc` | 2026-10-02 | Execution Leasing & Crash Recovery: `lease_owner`, `lease_expires_at`, OCC lease claims, `is_lease_active()`, safe recovery of crashed `RUNNING` investigations. |
| **Mission 12B**| `ac676ed` | 2026-10-08 | Investigation Event History API: `GET /investigations/{id}/events` with `Accept: application/json`, payload allowlisting, truthful `backend_supports_event_history=False` on filesystem. |
| **Mission 12C**| `1506904` | 2026-10-10 | Live Investigation Event Streaming via SSE: `GET /investigations/{id}/events` with `Accept: text/event-stream`, keyset pagination `(created_at ASC, event_id ASC)`, `Last-Event-ID` resume, keepalives, terminal stream draining. |
| **Mission 13** | `8d8e7d6` | 2026-10-13 | Async Investigation Dispatch & Background Execution: opt-in `Prefer: respond-async` / `?async=true` returning `202 Accepted` with `AcceptedInvestigationResponse`, immediate queryability and SSE connectivity, durable lease ownership acquired before 202 acceptance with PostgreSQL OCC protection (`ConcurrencyError` -> 409), continuous worker ID ownership throughout dispatch and execution, Starlette `BackgroundTask` execution, process-local slot registry, and failure state handling. |
| **Mission 14** | `1d3b2e4`, `a5e535a` | 2026-10-15 | PostgreSQL Reliability Harness: Phase 2 — authoritative migration runner (`core/persistence/schema.py`), `schema_migrations` tracking table, checksum tampering detection, strict pre-existing schema adoption; Phase 3 — live concurrency tests (OCC, expired lease race, concurrent async resume, outbox atomicity); Phase 4 — `.github/workflows/ci.yml` (two-job, PostgreSQL service), Makefile `db-up/db-down/db-migrate/db-status/test-postgres/test-all` targets, all six documents updated. |
| **Mission 15** | `16a6ea8` (stabilised in `fa4f1c7`) | 2026-10-17 | PostgreSQL Serialization Failure Recovery: bounded retry of transient transaction failures (`40001`, `40P01`) wrapping the whole `PostgresRepository.save()` transaction with a fresh connection per attempt; exponential backoff with full jitter (`SENTINEL_PG_RETRY_*`, clamped); OCC `ConcurrencyError` and permanent errors never retried; `RetryExhaustedError` → `503 persistence_unavailable`; deterministic test-only fault-injection seam; `tests/test_pg_retry.py` (21 offline + 4 live). Validated green in GitHub Actions run 39048469043. |

---

## 3. Subsystem Architecture & Current Behaviour

### 3.1 Persistence & Optimistic Concurrency Control (OCC)
- **Filesystem Backend**: Persists state as formatted JSON in `.sentinel/<investigation_id>/state.json` via atomic rename. Process-local locking only. Does not support outbox or durable event history (`backend_supports_event_history: false`).
- **PostgreSQL Backend**: Uses `psycopg-pool` to manage PostgreSQL connections. Stores relational operational metadata alongside `state_payload JSONB`. OCC ensures that updates match `version = %s` and increment `version = version + 1`, raising `ConcurrencyError` on conflicting writes. Supports durable outbox and reports `backend_supports_event_history: true`.

### 3.2 Transactional Outbox & Event Delivery Worker
- State updates and outbox events are committed in a single atomic database transaction via `PostgresRepository.save(state, events)`. Duplicate event IDs use `INSERT ... ON CONFLICT (event_id) DO NOTHING`, so a duplicate can never abort a valid state change (Mission 16).
- The `EventDeliveryWorker` (`python -m api.event_worker` or `make worker`) runs a claim → deliver → finalise cycle. Each poll first releases expired claims (crash recovery), then atomically claims a batch via `claim_pending()` (locking eligible `status = 'pending' AND next_attempt_at <= NOW()` rows with `FOR UPDATE SKIP LOCKED` in `(created_at ASC, event_id ASC)` order). It dispatches events to registered event buses and marks success with `mark_processed()`, or durably reschedules with `schedule_retry()` (persisted `next_attempt_at`, bounded jittered exponential backoff) and marks terminal failure with `mark_failed()` after the attempt limit. The earlier implementation read rows with a non-claiming query (`processed_at IS NULL AND retry_count < max_retries`, ordered by `created_at ASC`) and recorded `error_message`; that predicate and the `error_message` column name are superseded by the claim protocol while `error_message` is retained.

### 3.3 Execution Leasing & Crash Recovery
- Investigations carry lease metadata: `lease_owner: Optional[str]` and `lease_expires_at: Optional[datetime]`.
- Active leases protect investigations from conflicting concurrent runs or premature resumes (`is_lease_active()` returns true → `409 Conflict`).
- If a server worker crashes during an investigation, its lease expires after the TTL (default 300s). Once expired, `is_lease_active()` evaluates to false, allowing any worker to acquire the lease via OCC, restore state, and resume from the first incomplete stage.

### 3.4 API Endpoints Summary

| Endpoint | Method | Status Codes | Description |
|---|---|---|---|
| `/health` | `GET` | 200 | Returns system status, persistence backend, and outbox queue stats. |
| `/investigations` | `POST` | 200, 202, 400, 409, 500 | Runs an investigation for an `incident_id`. Default: synchronous (`200 OK`). Opt-in async via `Prefer: respond-async` or `?async=true` (`202 Accepted`). |
| `/investigations` | `GET` | 200 | Paginated list of investigations with optional status filter. |
| `/investigations/{id}` | `GET` | 200, 404 | Retrieves complete investigation state and summary. Immediately queryable after 202 acceptance. |
| `/investigations/{id}/resume` | `POST` | 200, 202, 400, 404, 409 | Resumes an interrupted investigation. Rejects active leases with 409 Conflict. Default: synchronous (`200 OK`). Opt-in async (`202 Accepted`). |
| `/investigations/{id}/approval` | `POST` | 200, 400, 404, 409 | Records human approval/rejection decision. Patch is never auto-applied. |
| `/investigations/{id}/events` | `GET` | 200, 400, 404 | Dual-mode: JSON snapshot (`application/json`) or live SSE stream (`text/event-stream`). Immediately connectable after 202 acceptance. |

### 3.5 Event History & SSE Streaming Semantics
- **Content Negotiation**: Driven strictly by the `Accept` request header.
- **JSON Snapshot**: Returns `InvestigationEventsResponse` with events ordered by `(created_at ASC, event_id ASC)`. Filesystem returns `backend_supports_event_history: false` and empty event list.
- **Live SSE Streaming**:
  - Delivers real-time events formatted as `id: ...\nevent: ...\ndata: ...\n\n`.
  - Keyset cursor pagination supports `Last-Event-ID` header or `?last_event_id` query parameter. Unresolvable cursor IDs return HTTP 200 with an SSE comment notice and restart streaming from the beginning so no events are lost (never HTTP 400).
  - Polls PostgreSQL outbox at 0.5s intervals, sending periodic `: keep-alive\n\n` comments every 15s to maintain proxy connections.
  - On terminal states (`COMPLETED`, `FAILED`, `PARTIAL`), ensures all pending persisted events in the outbox are drained before closing cleanly without emitting synthetic frames.
  - Checks `request.is_disconnected()` to terminate streams cleanly when clients disconnect.

### 3.6 Transient PostgreSQL Failure Recovery (Mission 15)
- `PostgresRepository.save()` wraps **one complete transaction** (row lock, state write, atomic outbox inserts); transient PostgreSQL contention errors re-run the entire transaction with a fresh connection.
- **Retryable** SQLSTATEs: `40001` (`serialization_failure`) and `40P01` (`deadlock_detected`). **Not retried**: OCC `ConcurrencyError` (`409`), constraint violations, validation/safety failures. Classification unwraps `__cause__`/`__context__`.
- Bounded exponential backoff with full jitter; defaults `max_attempts=5`, `base=0.05s`, `max=1.0s` (ceiling 30s), env-overridable via `SENTINEL_PG_RETRY_*` (clamped).
- Each attempt restores the OCC `version` to its entry value (no double-increment); outbox events are inserted only in the committed attempt (no duplicates).
- Exhaustion raises `RetryExhaustedError` (subclass of `PersistenceError`) → API `503 persistence_unavailable`, with the in-memory version restored.
- **Isolation caveat / limit**: no explicit isolation level is configured (server default **READ COMMITTED**), which does not naturally produce `40001`; a deterministic test-only fault-injection seam exercises retries. No serialisable-isolation or phantom-read guarantee is claimed.
- **Validation status**: 21 offline tests pass locally; the 4 live PostgreSQL 16 integration tests in `tests/test_pg_retry.py` executed and passed in CI's `postgres` job ([run 39048469043](https://github.com/groot34/sentinel/actions/runs/39048469043), 733 passed / 0 skipped). They were **not** run locally (Docker daemon unavailable).

### 3.7 Concurrent & Crash-Safe Outbox Delivery (Mission 16)
- **Migration**: `core/persistence/sql/003_outbox_delivery_claim.sql` (additive, idempotent) adds `claimed_by`, `claimed_at`, `claim_expires_at`, and `next_attempt_at` (`NOT NULL DEFAULT NOW()`) to `event_outbox`, plus index `idx_event_outbox_claim (status, next_attempt_at, created_at)`. `core/persistence/schema.py` validates these in `EXPECTED_TABLES` while keeping the 002 adoption check tolerant of a legacy 002-era outbox (the pre-003 column/index set), so existing databases upgrade in place.
- **Atomic claiming**: `PostgresRepository.claim_pending(worker_id, limit, claim_ttl_seconds)` locks eligible rows with `SELECT ... FOR UPDATE SKIP LOCKED` in `(created_at ASC, event_id ASC)` order, then claims exactly those rows via `UPDATE ... WHERE event_id = ANY(%s::text[])`. Concurrent workers claim **disjoint** sets.
- **Durable retry scheduling**: `schedule_retry(event_id, next_attempt_at, error_message)` persists the next eligible attempt time, increments `retry_count`, records `error_message`, and clears the claim. The worker computes bounded exponential backoff with full jitter and never sleeps inside the batch loop.
- **Crash recovery**: `release_expired_claims()` returns claims past `claim_expires_at` (default TTL 300 s) to `pending`; `mark_processed()` / `mark_failed()` clear the claim. Completed and terminally failed events are never reclaimed.
- **Duplicate-event fix**: `_save_transaction()` and `save_events()` use `INSERT ... ON CONFLICT (event_id) DO NOTHING` instead of a swallowed `UniqueViolation`, so a duplicate event ID no longer aborts the surrounding transaction. Mission 15 retry classification is unchanged.
- **Guarantee**: durable, concurrency-safe claims with **at-least-once** delivery; exactly-once **external** delivery is **not** claimed (in-process `EventBus` destination; consumers must be idempotent and key on `event_id`).
- **Validation status**: implementation complete. The 4 new offline worker tests (`tests/test_event_worker.py`, now 11) pass locally; the 6 new live PostgreSQL integration tests (`tests/test_outbox.py`) are **pending CI** (Docker unavailable locally). Local offline suite: 695 passed / 48 skipped / 1 warning.

---

## 4. Documentation Discrepancies Resolved in this Audit

Prior to this reconciliation, documentation had drifted significantly across the repository:
1. **ARCHITECTURE.md**: Stopped at Sentinel 1.0 specifications; completely lacked documentation for Sentinel 2.0 domain state, OCC versioning, transactional outbox, execution leasing, HTTP API, and SSE streaming.
2. **CURRENT_STATE_AUDIT.md**: Dated 2026-08-29, referencing commit `a2185ef`, 62 tests, and reporting implemented components as "PARTIAL" or "stubs".
3. **README.md**: Reported a stale test count of 315 tests (actual: 609 passed, 32 skipped) and omitted all Sentinel 2.0 API, outbox, and SSE streaming capabilities.
4. **CHANGELOG.md**: Stopped at Mission 05 (`2.0.0-mission05`), missing 9 major completed milestones (Missions 06 through 12C), and retained an obsolete `[Unreleased]` block from 1.0.
5. **REPRODUCE.md**: Stated that running investigations could not be distinguished from interrupted ones without leasing (superseded by Mission 12A leasing), and omitted the events API / SSE streaming curl commands.
6. **TODO.md**: Mistakenly labelled Transactional Outbox as "Mission 06" and omitted tracking for Missions 07 through 12C.

---

## 5. Remaining Engineering Work (Post-Mission 16)

The following represents genuine upcoming engineering tasks, clearly separated from completed work:
1. **Distributed Task Queue Integration**: Mission 13 implemented in-process asynchronous dispatch via Starlette `BackgroundTask`. Extending to durable external queues (Celery, Redis Streams, Temporal, BullMQ) across distributed worker pools remains a future enterprise enhancement.
2. **External Event Bus Integrations**: Mission 16 made `EventDeliveryWorker` safe and crash-tolerant across concurrent worker processes, but it still publishes to the in-process bus. Integrating Redis, Kafka, or webhook publishers for external consumers remains future work; exactly-once external delivery would require consumer-side idempotency or a transactional broker.
3. **Explicit `SERIALIZABLE` isolation (optional)**: Mission 15 handles `40001`/`40P01` defensively but Sentinel still runs at the server-default READ COMMITTED level; a future mission could adopt explicit serialisable isolation with the accompanying phantom-read guarantees and tests.
4. **Live PostgreSQL validation of Mission 16**: the 6 new live outbox integration tests are pending CI (Docker unavailable locally).

---

## 6. Historical Reference: Audit from 2026-08-29 (Commit `a2185ef`)

*(Preserved for historical traceability)*

At commit `a2185ef` (2026-08-29), the repository contained:
- Initial repository skeleton with 6 JSON Schema contracts (`schemas/*.json`).
- Synthetic incident dataset of 10 scenarios with `tests/validate_incidents.py` (51 tests passing).
- Stubs for agents and baseline investigator.
- 62 total passing tests (11 schema tests + 51 incident checks).
- No LLM client, orchestrator pipeline, persistence layer, or API server had yet been built.
