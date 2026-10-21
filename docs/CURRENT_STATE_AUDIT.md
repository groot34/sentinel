# Sentinel Repository State Audit

**Audit Date**: 2026-10-21<br>
**Branch**: `main`<br>
**Base Commit**: `f458a2b` (*docs: record Mission 16 CI validation*)<br>
**Working Tree Status**: Mission 17 and Mission 18 changes present but **uncommitted** — Mission 17 modified 5 files (`README.md`, `api/app.py`, `api/models.py`, `api/routes.py`, `docs/ARCHITECTURE.md`) and added `tests/test_investigation_result_api.py`; Mission 18 modified `agents/orchestrator.py`, `api/app.py`, `api/errors.py`, `README.md`, `REPRODUCE.md`, `TODO.md`, `CHANGELOG.md`, `docs/ARCHITECTURE.md`, `docs/CURRENT_STATE_AUDIT.md` and added `agents/bundle_validator.py` + `tests/test_bundle_validator.py`. The last committed state is Mission 16 (`aba70e1`), verified green in GitHub Actions run [39277794918](https://github.com/groot34/sentinel/actions/runs/39277794918). Missions 17 and 18 have **local validation only** — see §1, §3.8, and §3.9.

---

## 1. Executive Summary & Verification Baseline

This audit establishes the verified state of the Sentinel repository following the implementation and verification of Sentinel 2.0 Mission 14, Phases 2–4 (PostgreSQL Reliability Harness: Schema Bootstrapping, Live Concurrency Tests, and CI/Developer Workflow), Mission 15 (PostgreSQL Serialization Failure Recovery), whose live PostgreSQL behaviour is validated in CI, Mission 16 (Concurrent & Crash-Safe Outbox Delivery), which is complete and validated by live PostgreSQL 16 CI, Mission 17 (Investigation Result API), which is implemented and **locally validated only** (no CI run yet), and Mission 18 (Incident Bundle Contract & Preflight Validation), also implemented and **locally validated only**. All findings are derived directly from the active codebase, automated test executions, and verifiable Git history.

### Current Test Suite & Verification Results

- **Pytest Suite (offline, local Windows / Docker unavailable, Mission 18)**: **799 passed, 54 skipped, 1 warning** (853 collected — +98 vs Mission 17's 755).
- **Pytest Suite (offline, local Windows / Docker unavailable, Mission 17)**: **707 passed, 48 skipped, 1 warning** (755 collected — +12 vs Mission 16's 743).
- **Pytest Suite (offline, local Windows / Docker unavailable, Mission 16)**: **695 passed, 48 skipped, 1 warning** (743 collected in ~13 s).
- **Pytest Suite (GitHub Actions [run 39277794918](https://github.com/groot34/sentinel/actions/runs/39277794918), commit `aba70e1`, Mission 16)**: `postgres` job (PostgreSQL 16) **743 passed, 0 skipped, 3 warnings** (`Total skipped: 0`, so all 6 Mission 16 live outbox tests executed); `offline` job **654 passed, 4 skipped, 1 warning**. Migrations `001`–`003` applied and schema verification passed. *(This is the latest verified CI run; **no CI run exists for Missions 17 or 18 yet**.)*
- **Pytest Suite (GitHub Actions [run 39048469043](https://github.com/groot34/sentinel/actions/runs/39048469043), commit `fa4f1c7`, Mission 15)**: `postgres` job (PostgreSQL 16) **733 passed, 0 skipped, 3 warnings**; `offline` job **650 passed, 4 skipped, 1 warning**.
- **Incident Dataset Integrity Suite** (`tests/validate_incidents.py`): **51 / 51 passed** (100%) — run in both CI jobs.
- **`git diff --check`**: Exit code 0 (clean — no trailing whitespace; only benign LF→CRLF notices on Windows).

#### Explanation of Skipped Tests & Warnings
1. **Windows Symlink Security Tests (5 skipped)**: `tests/test_api.py::test_create_investigation_rejects_symlink_inside_root` and `test_create_investigation_rejects_symlink_escaping_root`, plus three Mission 18 tests in `tests/test_bundle_validator.py` (`test_symlinked_bundle_entry_escaping_root_is_rejected`, `test_symlinked_bundle_entry_inside_root_is_accepted`, `test_symlinked_bundle_root_is_rejected`), are skipped because standard Windows user accounts without Developer Mode cannot create filesystem symbolic links. These tests **pass on Linux CI** (symlink creation does not require elevated privileges), so CI reports fewer skips for these.
2. **PostgreSQL Live Integration Tests (46 skipped locally)**: All guarded by `@pytest.mark.skipif(not POSTGRES_AVAILABLE, ...)` where `POSTGRES_AVAILABLE` probes `SENTINEL_DATABASE_URL` at test collection time. Skipped locally because Docker Desktop is not running on the development machine. Exact breakdown:
   - 4 in `tests/test_postgres_concurrency.py` (OCC competing writes, expired lease race, concurrent async resume, outbox rollback)
   - 8 in `tests/test_outbox.py` (6 from Mission 16: concurrent disjoint claims, stale-claim recovery, future-retry/completed exclusion, tied-timestamp ordering, duplicate-ID non-abortion, terminal-failure retention; plus 2 Mission 11/14: PostgreSQL outbox uniqueness + atomic persistence)
   - 2 in `tests/test_schema_migration.py` (live bootstrap and repeat idempotency)
   - 28 in `tests/test_postgres_persistence.py` (18 contract tests inherited from `PersistenceContractTests` + 10 PostgreSQL-specific integration tests)
   - 4 in `tests/test_pg_retry.py` (Mission 15: injected `40001` then real commit; exhausted retries leave no partial state; OCC conflict distinct from retryable error; state + outbox atomic under retry)
   - All 46 execute on CI; the `postgres` job reports **0 skipped** (verified in [run 39277794918](https://github.com/groot34/sentinel/actions/runs/39277794918) for Mission 16 — 743 passed / 0 skipped, and previously in run 39048469043 for the 40 Mission-15-era tests).
3. **Deprecation Warning (1 warning)**: Starlette deprecation notice in `tests/test_api.py` regarding `httpx` integration with `starlette.testclient.TestClient`.

> **Important**: Live PostgreSQL validation for Mission 15 is complete. GitHub Actions [run 39048469043](https://github.com/groot34/sentinel/actions/runs/39048469043) (commit `fa4f1c7`) passed both jobs: the `postgres` job provisioned the `postgres:16-alpine` container, applied the schema, and reported **733 passed / 0 skipped** — including all 40 PostgreSQL integration tests and Mission 15's 4 retry tests. The docs commit `1d55db8` also passed green in [run 39049346329](https://github.com/groot34/sentinel/actions/runs/39049346329) with identical results; both runs are genuine and mutually consistent. Mission 16's live PostgreSQL validation is **complete**: [run 39277794918](https://github.com/groot34/sentinel/actions/runs/39277794918) (commit `aba70e1`) passed both jobs — `postgres` (PostgreSQL 16) 743 passed / 0 skipped (`Total skipped: 0`, all 6 live outbox tests executed) and `offline` 654 passed / 4 skipped; migrations `001`–`003` applied and schema verification passed.

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
Sentinel 2.0 evolved the single-run CLI into an enterprise-grade, durable, distributed investigation platform across Missions 01 through 17:

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
| **Mission 16** | `aba70e1` | 2026-10-19 | Concurrent & Crash-Safe Outbox Delivery: additive migration `003_outbox_delivery_claim.sql`; atomic multi-worker claiming via `SELECT ... FOR UPDATE SKIP LOCKED`; durable DB-scheduled retry (`next_attempt_at`) with claim-expiry crash recovery; `INSERT ... ON CONFLICT (event_id) DO NOTHING`; `EventDeliveryWorker` rewritten around claim → deliver → finalise with injectable clock/rng. Validated green in GitHub Actions run 39277794918. |
| **Mission 17** | *uncommitted* | 2026-10-21 | Investigation Result API: read-only `GET /investigations/{id}/result` exposing an allowlisted projection of the diagnosis (hypotheses + verdicts, evidence with `file:line` references, per-check verification outcomes, fix proposals with unified-diff `patch`). Seven `extra="forbid"` Pydantic models in `api/models.py`; defensive projection helpers in `api/routes.py`; 12 tests in `tests/test_investigation_result_api.py`. **Local validation only; no CI run yet.** |

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
| `/investigations` | `POST` | 200, 202, 400, 409, 500 | Runs an investigation for an `incident_id`. Default: synchronous (`200 OK`). Opt-in async via `Prefer: respond-async` or `?async=true` (`202 Accepted`). A `400 invalid_incident_bundle` is returned when the bundle fails the Mission 18 preflight contract before any stage runs. |
| `/investigations` | `GET` | 200 | Paginated list of investigations with optional status filter. |
| `/investigations/{id}` | `GET` | 200, 404 | Retrieves complete investigation state and summary. Immediately queryable after 202 acceptance. |
| `/investigations/{id}/resume` | `POST` | 200, 202, 400, 404, 409 | Resumes an interrupted investigation. Rejects active leases with 409 Conflict. Default: synchronous (`200 OK`). Opt-in async (`202 Accepted`). |
| `/investigations/{id}/approval` | `POST` | 200, 400, 404, 409 | Records human approval/rejection decision. Patch is never auto-applied. |
| `/investigations/{id}/events` | `GET` | 200, 400, 404 | Dual-mode: JSON snapshot (`application/json`) or live SSE stream (`text/event-stream`). Immediately connectable after 202 acceptance. |
| `/investigations/{id}/result` | `GET` | 200, 400, 404, 503 | Read-only, allowlisted projection of the diagnosis: hypotheses (+ verdicts), evidence (+ `file:line` references), per-check verification outcomes, and fix proposals (+ unified-diff `patch`). Never mutates state or applies patches. |

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
- **Validation status**: complete and validated. The 4 new offline worker tests (`tests/test_event_worker.py`, now 11) pass locally; the 6 live PostgreSQL integration tests (`tests/test_outbox.py`) executed and passed in CI's `postgres` job ([run 39277794918](https://github.com/groot34/sentinel/actions/runs/39277794918), 743 passed / 0 skipped, `Total skipped: 0`). Local offline suite: 695 passed / 48 skipped / 1 warning.

### 3.8 Investigation Result Projection (Mission 17)
- **Endpoint**: `GET /investigations/{id}/result` — the read-only "result plane" that returns the diagnosis `GET /investigations/{id}` deliberately omits. Registered in `api/app.py` ahead of the `/events` route.
- **Allowlisted models**: seven Pydantic v2 models in `api/models.py` — `InvestigationResultResponse`, `HypothesisResultResponse`, `EvidenceResultResponse`, `VerificationResultResponse`, `VerificationCheckResponse`, `ProposalResultResponse`, `FileChangeResponse` — each declared with `ConfigDict(extra="forbid")`.
- **Projection source**: hypotheses/evidence/proposals are read from their persisted state collections; per-check verification outcomes are read from `stages["verification"].output`, the only persisted representation retaining each check's discrete `result` and `reference` (the normalised `verification_results` fold that information into free text).
- **Defensive mapping**: `_project_*` helpers in `api/routes.py` read only named attributes/dict keys; absent, partial, or malformed input yields empty lists / `None` rather than a fabricated value or a `500`. `result_available` is `true` when any of the four sections is populated.
- **Non-exposure guarantees**: the raw state payload, stage outputs, free-form `metadata`, secrets, `before`/`after` file bodies, and unrelated state fields are never returned. `reference` values are emitted verbatim (incident-relative `file:line`); only free-form check `detail` is path-sanitised via `sanitize_error_message()`.
- **Read-only**: the handler calls `repository.load()` only — it never mutates investigation state and never applies patches. Returned `proposal_id` values are accepted directly by `POST /investigations/{id}/approval`.
- **Status codes**: `200 OK` (including in-progress/partial investigations with empty sections), `400` invalid ID, `404` not found, `503` repository unavailable — mirroring the existing detail endpoint.
- **Visibility backend scope**: backend-agnostic by construction — `FilesystemRepository` and `PostgresRepository` both reconstruct `InvestigationState` via `from_dict()`, so the projection reads the same shape.
- **Validation status**: implementation complete, **local validation only**. 12 focused tests in `tests/test_investigation_result_api.py` pass locally against the real app; full offline suite 707 passed / 48 skipped / 1 warning; incident validation 51/51; `git diff --check` clean. **No CI run exists for Mission 17 yet**, and **PostgreSQL integration was not run locally** (Docker unavailable) — the backend-agnostic claim rests on code inspection.

### 3.9 Incident Bundle Contract & Preflight Validation (Mission 18)
- **Contract owner**: `agents/bundle_validator.py` — deterministic, read-only, zero LLM calls, no new dependencies. The only required entry is the incident directory (named `<incident_id>`); `logs/application.log`, `metrics/metrics.csv`, `git_diff.patch`, `service/`, and `docker-compose.yml` are all optional; `ground_truth.md` is evaluation-only and never read at runtime.
- **API surface**: `validate_incident_bundle(incident_dir, expected_id=None)` → frozen `BundleValidationResult` (detected `evidence_sources`, severity-tagged `BundleIssue`s with stable codes, `ok`, `error_messages()` / `warning_messages()`); `require_valid_incident_bundle(...)` raises `FileNotFoundError` / `NotADirectoryError` / `InvalidIncidentBundleError`; CLI `python -m agents.bundle_validator <dir> [--incident-id ID]` exits `0` valid / `1` invalid.
- **Fatal conditions**: missing path, non-directory path, invalid or mismatched incident ID, symlinked bundle root or entry symlink escaping the root, an entry whose type contradicts the contract (`not_a_file` / `not_a_directory`), a declared channel directory (`logs/`, `metrics/`) whose expected file is absent (`required_file_missing`), a present-but-empty/malformed `metrics/metrics.csv` (`metrics_csv_empty` / `metrics_csv_invalid`, messages naming the metric and the 1-indexed file row), and a present-but-invalid metrics timestamp (`metrics_timestamp_invalid` — a `timestamp` value that is not an ISO-8601 UTC instant ending in `Z`, e.g. `2026-08-28T14:00:00Z`).
- **Non-fatal (preserved partial evidence)**: any evidence source absent; empty log file (warning `empty_log_file`); evidence-free bundle (warning `no_evidence_sources`). The existing empty-directory and non-standard-filename fixtures remain valid.
- **Where it runs**: `IncidentOrchestrator._validate_incident_dir()` calls `require_valid_incident_bundle()` at the top of both `investigate()` and `resume()`, before `_run_pipeline()` — therefore before any stage and before the LLM client is constructed, so an invalid bundle never spends Groq credits. `api/errors.py` + `api/app.py` map `InvalidIncidentBundleError` to `400 invalid_incident_bundle`; the pre-existing `api/routes.py` containment checks are unchanged.
- **Read isolation**: containment is verified **before** any evidence file is opened — a canonical evidence path (`logs/application.log` or `metrics/metrics.csv`) that resolves outside the bundle root is reported as `bundle_escapes_root` but is never read or parsed, so escaped content cannot leak into the `400` detail. The validator opens only those two canonical files by relative path; it never enumerates the bundle and never opens `ground_truth.md`, `results_baseline.csv`, or `baseline_summary.json`. It never writes to the bundle and never applies patches. *Residual limitation (accepted):* the bundle is validated before the pipeline re-reads it, so a symlink swapped in between the preflight check and the read is not re-checked (a narrow TOCTOU window) — documented rather than closed by architectural change.
- **Validation status**: implementation complete, **local validation only**. 98 focused tests in `tests/test_bundle_validator.py` (92 passed / 6 symlink skips); the CLI reports `VALID` for all 10 canonical fixtures; full offline suite 799 passed / 54 skipped / 1 warning (853 collected); incident validation 51/51; `git diff --check` clean. **No CI run exists for Mission 18 yet**, and PostgreSQL was **not** exercised locally.

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

## 5. Remaining Engineering Work (Post-Mission 18)

The following represents genuine upcoming engineering tasks, clearly separated from completed work:
1. ~~**Documented Incident Bundle Contract (product priority)**~~ — **completed in Mission 18**: the canonical input contract (required/optional entries, formats, incident-ID and bundle-path handling, fatal vs. legitimate partial-evidence conditions, and a minimal example) is documented in [README §6.2](../README.md) and [docs/ARCHITECTURE.md §4.7](ARCHITECTURE.md), and enforced before any LLM call by `agents/bundle_validator.py`. The consumer-facing *diagnosis* contract remains exposed by `GET /investigations/{id}/result` (Mission 17).
2. **CI validation for Missions 17 & 18**: commit and push the Mission 17 and Mission 18 changes and confirm both GitHub Actions jobs stay green; no CI run exists for either mission yet.
3. **Distributed Task Queue Integration**: Mission 13 implemented in-process asynchronous dispatch via Starlette `BackgroundTask`. Extending to durable external queues (Celery, Redis Streams, Temporal, BullMQ) across distributed worker pools remains a future enterprise enhancement.
4. **External Event Bus Integrations**: Mission 16 made `EventDeliveryWorker` safe and crash-tolerant across concurrent worker processes, but it still publishes to the in-process bus. Integrating Redis, Kafka, or webhook publishers for external consumers remains future work; exactly-once external delivery would require consumer-side idempotency or a transactional broker.
5. **Explicit `SERIALIZABLE` isolation (optional)**: Mission 15 handles `40001`/`40P01` defensively but Sentinel still runs at the server-default READ COMMITTED level; a future mission could adopt explicit serialisable isolation with the accompanying phantom-read guarantees and tests.

---

## 6. Historical Reference: Audit from 2026-08-29 (Commit `a2185ef`)

*(Preserved for historical traceability)*

At commit `a2185ef` (2026-08-29), the repository contained:
- Initial repository skeleton with 6 JSON Schema contracts (`schemas/*.json`).
- Synthetic incident dataset of 10 scenarios with `tests/validate_incidents.py` (51 tests passing).
- Stubs for agents and baseline investigator.
- 62 total passing tests (11 schema tests + 51 incident checks).
- No LLM client, orchestrator pipeline, persistence layer, or API server had yet been built.
