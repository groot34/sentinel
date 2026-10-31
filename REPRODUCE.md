# Reproduction Guide

This guide provides step-by-step instructions to reproduce Sentinel evaluation results on synthetic incident bundles.

## Prerequisites
- Python 3.11+
- Virtual environment (`venv` or `conda`)
- Free **Groq API Key** — get one at https://console.groq.com (no credit card required)

## Setup
```bash
# 1. Clone repository and navigate to root
git clone https://github.com/groot34/sentinel.git
cd sentinel-incident-investigator

# 2. Create and activate virtual environment
python -m venv .venv
source .venv/bin/activate  # On Windows: .venv\Scripts\activate

# 3. Install dependencies
pip install -r requirements.txt

# 4. Configure environment
cp .env.example .env
# Edit .env and set:  GROQ_API_KEY=gsk_your_key_here
```

## Running Tests (No API Key Required)
```bash
# Run full unit and offline test suite — zero real Groq API calls (all LLM calls mocked)
pytest
# Verified outcome (local, Windows, no database, Mission 20): 947 passed, 55 skipped, 1 warning in ~14s
# (55 skipped: 46 integration tests requiring a live PostgreSQL instance skipped when
#  SENTINEL_DATABASE_URL is unset/unreachable — including Mission 15's 4 transient-failure
#  retry tests and Mission 16's 6 outbox claim/recovery tests; 9 symlink tests skipped on
#  Windows without Developer Mode — 2 in tests/test_api.py, 6 in tests/test_bundle_validator.py
#  and 1 in tests/test_incident_ingestion_api.py.)
# Verified in CI (run 39048469043, Mission 15): the postgres job executes every PostgreSQL
#  test and reported 733 passed, 0 skipped; the offline job reported 650 passed, 4 skipped.
#  Mission 16 verified in CI (run 39277794918, commit aba70e1): offline 654 passed / 4 skipped;
#  postgres 743 passed / 0 skipped (Total skipped: 0) — all 6 live outbox tests executed.
#  Missions 17 (12 result-endpoint tests) and 18 (98 bundle-validator tests) verified green in
#  CI (run 39635055973, commit 07a72d5): postgres 853 passed / 0 skipped.
#  Mission 19 (86 incident-ingestion API tests: 85 passed + 1 skipped symlink) has local
#  validation only — no CI run yet; PostgreSQL integration was NOT run locally.
#  Mission 20 (63 API-key authentication tests in tests/test_api_auth.py, all offline) also has
#  local validation only — no CI run yet. Adding it took the offline suite from 884 to 947 passed.

# Run the 51 incident dataset invariant validation checks
python tests/validate_incidents.py
# Verified outcome: 51 passed in ~0.5s
```

## Transient Failure Recovery Tests (Mission 15)

Bounded transient PostgreSQL failure recovery (`40001` / `40P01`) lives in
`core/persistence/retry.py` and is covered by `tests/test_pg_retry.py`. The 21
offline tests (classification, retry engine, backoff bounds, repository wiring,
fault-injection seam) require no database; the 4 integration tests require a live
PostgreSQL instance.

```bash
# Offline retry-engine + repository-wiring tests (no database, no real sleeps)
pytest tests/test_pg_retry.py -v
# Verified outcome (no database): 21 passed, 4 skipped

# Live PostgreSQL 16 integration (requires make db-up && make db-migrate)
SENTINEL_DATABASE_URL=postgresql://sentinel:sentinel_dev_password@localhost:5432/sentinel_db \
  pytest tests/test_pg_retry.py -v
# With PostgreSQL available, the 4 integration tests execute:
#   - injected 40001 then real commit (exactly one event persisted; version unchanged by retries)
#   - exhausted retries leave no partial state or orphaned events
#   - OCC conflict stays distinguishable from retryable errors
#   - state and outbox commit atomically under an injected retry
```

All 4 integration tests executed and passed in CI's `postgres` job
([run 39048469043](https://github.com/groot34/sentinel/actions/runs/39048469043),
733 passed / 0 skipped). They were **not** run locally — Docker is unavailable on the
development machine, so the offline tests are the only ones executed locally.

Retry configuration (conservative defaults, env-overridable; malformed values are clamped):
`SENTINEL_PG_RETRY_MAX_ATTEMPTS=5`, `SENTINEL_PG_RETRY_BASE_DELAY=0.05`,
`SENTINEL_PG_RETRY_MAX_DELAY=1.0`.

> Note: Sentinel does not set an explicit isolation level, so the server default
> **READ COMMITTED** applies and `40001` is exercised via a deterministic
> fault-injection seam rather than relying on isolation level. No serialisable or
> phantom-read guarantee is claimed.

## Concurrent & Crash-Safe Outbox Delivery Tests (Mission 16)

Atomic, crash-safe outbox claims live in `PostgresRepository`
(`core/persistence/postgres.py`) with the worker in
`api/event_worker/worker.py`. The state-plus-event migration is
`core/persistence/sql/003_outbox_delivery_claim.sql`. The offline worker tests
require no database; the outbox integration tests require a live PostgreSQL
instance and are skipped otherwise.

```bash
# Offline event-worker tests (no database, no real sleeps — clock/rng are injected)
pytest tests/test_event_worker.py -v
# Verified outcome (no database): 11 passed

# Live PostgreSQL 16 outbox integration (requires make db-up && make db-migrate)
SENTINEL_DATABASE_URL=postgresql://sentinel:sentinel_dev_password@localhost:5432/sentinel_db \
  pytest tests/test_outbox.py -v
# With PostgreSQL available, the outbox integration tests execute:
#   - concurrent workers claim disjoint event sets (threading.Barrier, separate pools)
#   - expired claims are recovered after a simulated worker crash (visibility timeout)
#   - future-retry and completed events are excluded from claims
#   - deterministic (created_at, event_id) ordering for tied timestamps
#   - duplicate event IDs do not abort valid state-plus-event persistence
#   - terminal failure after the attempt limit retains the error
```

**Delivery guarantee**: durable, concurrency-safe claims with **at-least-once**
delivery. Because the destination is an in-process `EventBus`, exactly-once
**external** delivery is **not** claimed; consumers must be idempotent and key on
`event_id`.

**Validation status**: complete. The 4 new offline worker tests pass locally; the 6 new
live PostgreSQL integration tests executed and passed in CI's `postgres` job
([run 39277794918](https://github.com/groot34/sentinel/actions/runs/39277794918),
743 passed / 0 skipped, `Total skipped: 0`). Locally (Docker unavailable) the offline
suite is the only one that runs.

## Investigation Result API Tests (Mission 17)

The read-only `GET /investigations/{id}/result` projection lives in `api/routes.py`
with allowlisted response models in `api/models.py`. Its tests are offline only —
no database and no Groq calls are required.

```bash
# Focused result-endpoint tests (real app via TestClient; no database, no network)
pytest tests/test_investigation_result_api.py -v
# Verified outcome (no database): 12 passed
```

They cover full diagnosis exposure, interoperability with `POST /investigations/{id}/approval`,
in-progress / partial / malformed-verification handling, `404`/`400`/`503` error behaviour,
internal-state non-leakage (exact allowlisted key sets; `before`/`after` absent), and
preservation of the pre-existing `GET /investigations/{id}` contract.

**Validation status**: implementation complete, **local validation only**. All 12 tests pass
locally and the full offline suite is 707 passed / 48 skipped / 1 warning (at the time of
Mission 17; after Mission 18 the suite is 799 passed / 54 skipped). **No CI run exists
for Mission 17 yet**, and the endpoint's PostgreSQL path was **not** exercised locally
(Docker unavailable) — the projection is backend-agnostic because both repositories
reconstruct `InvestigationState` via `from_dict()`.

## Incident Bundle Contract & Preflight Validation (Mission 18)

The canonical input contract and the deterministic preflight validator live in
`agents/bundle_validator.py`. The full contract is documented in
[README §6.2](README.md#62-incident-bundle-contract) and
[docs/ARCHITECTURE.md §4.7](docs/ARCHITECTURE.md#47-incident-bundle-contract--preflight-validation-mission-18).
Validation is read-only, free of LLM calls, and runs before the first pipeline stage.

```bash
# Validate one bundle (no API key, no database, no Groq credits)
python -m agents.bundle_validator incidents/inc_01_n_plus_one_query --incident-id inc_01_n_plus_one_query
# Verified output:
#   Bundle:        incidents\inc_01_n_plus_one_query
#   Incident ID:   inc_01_n_plus_one_query
#   Evidence:      logs/application.log, metrics/metrics.csv, git_diff.patch, service
#   Result:        VALID
# Exit code 0 = valid (warnings allowed); exit code 1 = at least one fatal error.

# Focused validator tests (offline; no database, no network, no Groq)
pytest tests/test_bundle_validator.py -v
# Verified outcome (no database): 92 passed, 6 skipped (symlink tests need
# Windows Developer Mode / non-admin privileges)
```

They cover the eight target categories: valid canonical bundles, missing required files, empty
or malformed files, legitimate partial-evidence bundles, invalid incident IDs and path traversal,
clear actionable error messages (file + offending row), validation occurring before any LLM call
(`get_llm_client` is asserted never to be constructed), and no source mutation / no access to
`ground_truth.md` (recorded file-open tracing plus an AST-style literal scan).

**Validation status**: implementation complete, **local validation only**. Focused tests 92 passed /
6 skipped locally; full offline suite 799 passed / 54 skipped / 1 warning (853 collected); incident
validation 51/51. Missions 17 & 18 are CI-green in [run 39635055973](https://github.com/groot34/sentinel/actions/runs/39635055973)
(commit `07a72d5`, `postgres` 853 passed / 0 skipped); PostgreSQL was **not** exercised locally.

## Safe Incident Bundle Ingestion (Mission 19)

The opt-in ingestion endpoint lets a client submit a text-only incident bundle as JSON-inline
content instead of writing directly to the server filesystem. The flow and all limits live in
`agents/bundle_ingest.py`; the request/response schemas are in `api/models.py`. The full contract
is documented in [README §9](README.md#9-http-api) and
[docs/ARCHITECTURE.md §4.8](docs/ARCHITECTURE.md#48-safe-incident-bundle-ingestion-mission-19).

**Disabled by default.** The endpoint returns `404 ingestion_disabled` and writes nothing unless
explicitly enabled. Enable it (and note this is *not* authentication) with:

```bash
# Enable ingestion for this server process (disabled unless set to 1/true/yes/on)
SENTINEL_ALLOW_INCIDENT_INGESTION=1 python -m api
```

Ingest a bundle, then start an investigation against the ingested incident:

```bash
# 1. Ingest the bundle (text files only; relative POSIX paths)
curl -i -X POST http://127.0.0.1:8000/incidents \
  -H "Content-Type: application/json" \
  -d '{
    "incident_id": "inc_local_demo",
    "files": {
      "logs/application.log": "2026-08-28T14:00:00Z ERROR timeout\n",
      "metrics/metrics.csv": "timestamp,cpu\n2026-08-28T14:00:00Z,0.95\n"
    }
  }'
# -> 201 Created, Location: /investigations/inc_local_demo
#    {"incident_id":"inc_local_demo","bundle_path":"incidents/inc_local_demo",
#     "evidence_sources":["logs/application.log","metrics/metrics.csv"]}

# 2. Start an investigation with the existing endpoint
curl -s -X POST http://127.0.0.1:8000/investigations \
  -H "Content-Type: application/json" \
  -d '{"incident_id": "inc_local_demo"}'

# 3. Retrieve the diagnosis with the Mission 17 result endpoint
curl -s http://127.0.0.1:8000/investigations/inc_local_demo/result
```

Allowed paths: `logs/application.log`, `metrics/metrics.csv`, `git_diff.patch`,
`docker-compose.yml`, and any file beneath `service/`. Absolute paths, `..`, backslashes, drive
prefixes, NUL bytes, empty segments, over-long/over-deep paths, reserved evaluation files
(`ground_truth.md`/`.json`, `results_baseline.csv`, `baseline_summary.json`), and anything outside
the allowlist are rejected with `400`.

`overwrite` must be omitted or `false`. Replacement is **not supported**: `overwrite=true` is
rejected with `409 overwrite_not_supported` before any file is staged. A duplicate `incident_id`
also conflicts (`409`). To re-ingest, submit a new `incident_id`.

Limits (env-overridable; counted in UTF-8 bytes; excess returns `413 payload_too_large` before any
staging file is created): `SENTINEL_INGEST_MAX_FILES` (200), `SENTINEL_INGEST_MAX_FILE_BYTES`
(2 MiB), `SENTINEL_INGEST_MAX_TOTAL_BYTES` (20 MiB), `SENTINEL_INGEST_MAX_REQUEST_BYTES` (24 MiB).

```bash
# Focused ingestion tests (offline; real app via TestClient, isolated temp dirs; no network)
pytest tests/test_incident_ingestion_api.py -v
# Verified outcome (no database): 85 passed, 1 skipped (symlink test needs Developer Mode)
```

They cover the disabled-by-default gate and explicit opt-in, exact file contents, every
path-policy rejection, malformed/empty/duplicate-key bodies, invalid IDs and non-string values,
per-file/total/file-count limits (including multibyte UTF-8), the request-body limit enforced while
streaming — with a truthful `Content-Length`, with a *misleading* understated `Content-Length`, and
with chunked bodies that omit it, plus a valid within-limit request — preflight failures with
staging cleanup, `overwrite=true` rejection with byte-for-byte preservation of the existing bundle, a
defense-in-depth containment check, duplicate rejection (never touching the existing bundle),
concurrent same-ID submissions with **different** valid content (API-level and a cross-process
simulation relying only on the atomic rename), refusal to publish for an actively investigated or
leased incident, a symlinked-target refusal, injected storage failures, no absolute-path leakage, an
empty-bundle refusal at the service boundary, and compatibility of the existing investigation routes.

**Validation status**: implementation complete, **local validation only**. Focused tests 85 passed +
1 skipped; full offline suite 884 passed / 55 skipped / 1 warning (939 collected); incident
validation 51/51. **No CI run exists for Mission 19 yet**, and PostgreSQL was **not** exercised
locally.

> **Overwrite decision (honest):** a new incident is published with a single atomic `os.rename` and
> never leaves a partial bundle visible. Replacement is deliberately **unsupported** — there is no
> portable crash-safe, cross-process directory swap, so `overwrite=true` is refused (`409`) rather
> than risking the previous valid bundle with a best-effort swap. Across multiple Uvicorn workers
> the single rename still prevents corruption (the loser conflicts). Enabling the endpoint performs
> **unauthenticated** writes to `incidents_root`.

## API-Key Authentication (Mission 20)

An opt-in, static API-key guard protects every state-changing HTTP route. It lives in
`api/auth.py` as a small **pure ASGI middleware** (not Starlette's `BaseHTTPMiddleware`) so SSE
streaming and background-task dispatch are preserved. See
[docs/ARCHITECTURE.md §4.9](docs/ARCHITECTURE.md#49-api-key-authentication-mission-20) and
[README §API-Key Authentication](README.md#api-key-authentication-mission-20).

**Disabled by default** for backward compatibility. Enable it with `SENTINEL_API_AUTH_ENABLED`
and a non-empty `SENTINEL_API_KEY`:

```bash
# Enable the guard for this server process (disabled unless set to 1/true/yes/on)
SENTINEL_API_AUTH_ENABLED=1 SENTINEL_API_KEY="$(cat /run/secrets/sentinel_api_key)" python -m api
```

When enabled, **all methods other than `GET`/`HEAD`/`OPTIONS` are protected** (so future mutating
routes are covered automatically). `SENTINEL_API_AUTH_PROTECT_READS=true` additionally protects
`GET`/`HEAD`/`OPTIONS`, except `/health` and `/ready`, which stay public for those safe methods (a non-safe method on either path is still protected). Send the key as a bearer token:

```bash
# Authorised request (mutating routes need the header when auth is enabled)
curl -s -X POST http://127.0.0.1:8000/investigations \
  -H "Authorization: Bearer $SENTINEL_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"incident_id": "inc_01_n_plus_one_query"}'

# Missing/invalid credentials -> 401 with WWW-Authenticate: Bearer
curl -i -X POST http://127.0.0.1:8000/investigations -d '{}'
# HTTP/1.1 401 Unauthorized
# WWW-Authenticate: Bearer
# {"error":"unauthorized","detail":"Missing or invalid API credentials."}
```

The key is compared with `hmac.compare_digest` over UTF-8 bytes, and one generic `401` is returned
for both missing and invalid credentials (no distinguishing oracle). Exactly one `Authorization`
header is required — duplicate headers are rejected rather than trusting the first value. An
unrecognised non-empty `SENTINEL_API_AUTH_ENABLED` value is treated as disabled but logged
(sanitised), so a typo cannot silently disable the guard. Enabling authentication is
**independent** of incident ingestion: `SENTINEL_ALLOW_INCIDENT_INGESTION` still governs
`POST /incidents`. If authentication is enabled and the key is missing/blank, `create_app` raises
and `python -m api` prints a sanitised error and exits non-zero — the server never starts
half-configured.

Limitations (honest): this is a single **static** key shared by all callers — no per-user identity,
roles, rotation, or rate limiting; and browser `EventSource` cannot set an `Authorization` header,
so protecting the SSE stream requires a client that can send headers (e.g. `fetch`-based SSE or a
proxy).

```bash
# Focused authentication tests (offline; real middleware + app; no network, no database)
pytest tests/test_api_auth.py -v
# Verified outcome (no database): 63 passed
```

They cover default-disabled compatibility, missing/wrong/empty/malformed-scheme/non-ASCII
credentials, valid credentials on sync + async investigation creation, resume/approval routes,
ingestion independence, fail-closed construction (argument and environment) plus non-zero startup
exit without credential leakage, secret non-leakage in responses/logs, `hmac.compare_digest` usage,
read-protection off/on with safe-method `/health` and `/ready` public, SSE streaming intact, a route-coverage regression
test proving every non-safe method is protected by default, rejection before a protected request
body is consumed, indistinguishable missing-vs-wrong responses, duplicate `Authorization` header
rejection, the sanitised warning for an unrecognised `SENTINEL_API_AUTH_ENABLED` value, accepted
truthy values, `HEAD`/`OPTIONS` handling, header-name case handling, and that `app.state.auth_config`
does not retain the secret.

**Validation status**: implementation complete, **local validation only**. Focused tests 63 passed;
full offline suite 947 passed / 55 skipped / 1 warning (1002 collected); incident validation 51/51.
**No CI run exists for Mission 20 yet**, and PostgreSQL was **not** exercised locally.

## PostgreSQL Integration Testing

All live PostgreSQL integration tests are guarded by `@pytest.mark.skipif(not POSTGRES_AVAILABLE, ...)`
and skip gracefully when no database is reachable. To execute them locally:

### Prerequisites
- Docker Desktop (or Docker Engine) running
- Python 3.11+ with `requirements.txt` installed

### Workflow
```bash
# 1. Start PostgreSQL 16 and wait for it to be healthy
make db-up

# 2. Bootstrap the schema using the authoritative migration runner
make db-migrate
# Output: "Successfully applied 3 migration(s): 001_initial_schema, 002_outbox_schema, 003_outbox_delivery_claim"
#         "Schema verification passed: 3 tables verified."

# 3. Inspect which migrations are recorded in the tracking table
make db-status

# 4. Run live PostgreSQL integration tests only
make test-postgres
# With a DB available locally: all 46 PG tests execute (none skipped).
# CI (run 39048469043, Mission 15) confirms the postgres job at 733 passed, 0 skipped;
#  Mission 16 (run 39277794918) confirms the postgres job at 743 passed, 0 skipped — all 6 live outbox tests executed.

# 5. Run the complete suite (offline + live PostgreSQL + incident validation)
make test-all
# Locally on Windows without a DB (verified, Mission 20): 947 passed, 55 skipped, 1 warning.
# With a local DB (expected): 993 passed, 9 skipped (Windows symlink only), 1 warning.

# 6. Stop the container (data volume preserved for next run)
make db-down
```

### Overriding the database URL
```bash
export SENTINEL_DATABASE_URL=postgresql://user:pass@host:5432/dbname
make test-postgres
# Or pass inline:
PG_URL=postgresql://user:pass@host:5432/dbname make test-postgres
```

### Schema migration CLI
The authoritative migration runner can be invoked directly:
```bash
# Apply pending migrations
python -m core.persistence.schema apply --url "$SENTINEL_DATABASE_URL"

# Verify schema completeness (all tables, columns, indexes)
python -m core.persistence.schema verify --url "$SENTINEL_DATABASE_URL"

# Show recorded migration versions
python -m core.persistence.schema status --url "$SENTINEL_DATABASE_URL"
```

### CI / GitHub Actions
The `.github/workflows/ci.yml` workflow runs two jobs on every push and PR:
- **`offline`** — full unit and mock suite with no database; validates incident dataset; `git diff --check`.
- **`postgres`** — starts `postgres:16-alpine`, bootstraps the schema, runs the full test suite with `SENTINEL_DATABASE_URL` set. Fails if any required PostgreSQL integration test skips.

> **Verified live run**: GitHub Actions [run 39048469043](https://github.com/groot34/sentinel/actions/runs/39048469043)
> (commit `fa4f1c7`) completed green. The `postgres` job reported **733 passed, 0 skipped** against
> PostgreSQL 16 — covering all 40 guarded PostgreSQL integration tests, including the 4 Mission 15
> transient-failure retry tests. The `offline` job reported **650 passed, 4 skipped**. Live PostgreSQL
> validation was performed **in CI only**; Docker is unavailable on the local development machine, so
> no local PostgreSQL testing is claimed.
>
> **Run-number reconciliation**: the later documentation commit `1d55db8` also passed green in
> [run 39049346329](https://github.com/groot34/sentinel/actions/runs/39049346329) with identical
> results (`postgres` 733 passed / 0 skipped; `offline` 650 passed / 4 skipped). Both runs are
> genuine and mutually consistent — `39048469043` is the run for the code commit `fa4f1c7`, and
> `39049346329` is the run for the follow-up documentation commit.
>
> **Mission 16** adds 10 tests (4 offline worker tests + 6 live PostgreSQL outbox tests). Its live
> PostgreSQL validation is complete: GitHub Actions [run 39277794918](https://github.com/groot34/sentinel/actions/runs/39277794918)
> (commit `aba70e1`) passed both jobs — `postgres` (PostgreSQL 16) **743 passed, 0 skipped**
> (`Total skipped: 0`; all 6 live outbox tests executed) and `offline` **654 passed, 4 skipped**;
> migrations `001`–`003` applied and schema verification passed; incident validation 51/51 in both jobs.


## Starting the API Server

The Sentinel API server exposes the investigation pipeline over HTTP using
the Starlette/Uvicorn stack (both included in `requirements.txt`).

```bash
# Start the server with default settings (filesystem backend, localhost:8000)
python -m api

# Or via Makefile
make serve
```

The server binds to `127.0.0.1:8000` by default and is not exposed to the
network.

### Configuration

The launcher calls `load_dotenv()` before reading any environment variable,
so values in `.env` are applied automatically if the file exists. Variables
already set in the shell take precedence (python-dotenv does not override
them).

Copy `.env.example` to `.env` and configure before starting:

```bash
cp .env.example .env
# Required for investigation requests:
#   GROQ_API_KEY=gsk_your_key_here
#   GROQ_MODEL=openai/gpt-oss-120b   (already the default)
#
# Runtime configuration:
#   LOG_LEVEL=INFO                  # api + worker application log level (DEBUG/INFO/WARNING/ERROR/CRITICAL; invalid values fall back to INFO)
#   INCIDENTS_DIR=incidents         # incident bundles directory (overridden by --incidents-root)
#
# Persistence (filesystem is the default, no extra config needed):
#   SENTINEL_PERSISTENCE_BACKEND=filesystem
#   SENTINEL_PERSISTENCE_ROOT=.sentinel_persistence
#
# PostgreSQL backend:
#   SENTINEL_PERSISTENCE_BACKEND=postgres
#   SENTINEL_DATABASE_URL=postgresql://sentinel:password@localhost:5432/sentinel_db
#
# Incident-bundle ingestion (POST /incidents) is DISABLED by default:
#   SENTINEL_ALLOW_INCIDENT_INGESTION=1        # enable (1/true/yes/on); NOT authentication
#   SENTINEL_INGEST_MAX_FILES=200              # per-submission file count
#   SENTINEL_INGEST_MAX_FILE_BYTES=2097152     # 2 MiB per file (UTF-8 bytes)
#   SENTINEL_INGEST_MAX_TOTAL_BYTES=20971520   # 20 MiB total content (UTF-8 bytes)
#   SENTINEL_INGEST_MAX_REQUEST_BYTES=25165824 # 24 MiB request body
#
# API-key authentication is DISABLED by default and independent of ingestion:
#   SENTINEL_API_AUTH_ENABLED=1                # enable the guard (1/true/yes/on)
#   SENTINEL_API_KEY=change-me                 # REQUIRED and non-blank when enabled (never commit a real key)
#   SENTINEL_API_AUTH_PROTECT_READS=false      # also protect GET/HEAD/OPTIONS (except safe-method /health and /ready)
#
# Readiness probe (Mission 21):
#   SENTINEL_READINESS_TIMEOUT_SECONDS=2.0     # HTTP deadline for GET /ready (invalid values fall back to 2.0; valid positive values capped at 30.0)
```

### CLI options

| Option | Default | Description |
|--------|---------|-------------|
| `--host` | `127.0.0.1` | Bind address |
| `--port` | `8000` | Listen port |
| `--log-level` | `info` | Uvicorn log level (`debug`, `info`, `warning`, `error`) |
| `--incidents-root` | `INCIDENTS_DIR`, else `incidents` | Path to incident bundles directory (overrides the `INCIDENTS_DIR` environment variable) |

### Health check

```bash
curl http://127.0.0.1:8000/health
# {"status":"ok","service":"sentinel","version":"2.0"}
```

### Readiness check (Mission 21)

```bash
# Public for safe methods even under SENTINEL_API_AUTH_PROTECT_READS=true
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8000/ready
# 200
curl -s http://127.0.0.1:8000/ready
# {"status":"ready","checks":{"persistence":"ok"}}
# -> 503 {"status":"not_ready","checks":{"persistence":"unavailable"}} when persistence is unreachable
```
`/ready` runs a real, read-only persistence probe (bounded by `SENTINEL_READINESS_TIMEOUT_SECONDS`,
default `2.0`; valid positive values capped at `30.0`) on a two-worker executor with fail-fast
admission control; the body is a fixed allowlist and never exposes exceptions, paths, credentials or
DSNs. The timeout bounds the **HTTP response deadline only**, not necessarily the lifetime of a
blocked filesystem or PostgreSQL operation — such work is not strictly cancellable and can outlive the
deadline, but it remains constrained by the readiness admission capacity (two slots).

## Event Delivery Worker

The event delivery worker atomically claims pending events from the
transactional outbox and delivers them through the EventBus. This requires
PostgreSQL persistence.

```bash
# Start the event worker (requires PostgreSQL backend configured)
python -m api.event_worker

# The worker will:
# 1. Release expired claims (crash recovery) and atomically claim a batch of
#    eligible events using SELECT ... FOR UPDATE SKIP LOCKED, so concurrent
#    worker processes claim disjoint sets in deterministic (created_at, event_id) order.
# 2. Deliver each claimed event to in-memory EventBus subscribers.
# 3. Mark successful deliveries as processed.
# 4. Durably reschedule failed events (bounded exponential backoff with jitter),
#    writing next_attempt_at to the database — no sleeping inside the batch loop.
# 5. Mark events terminally failed after the attempt limit, retaining the error.
```

**Delivery guarantee:** durable, concurrency-safe claims with **at-least-once**
delivery. The destination is an in-process `EventBus`, so exactly-once
**external** delivery is **not** claimed; consumers must be idempotent and key on
`event_id`.

**Configuration:**
- `SENTINEL_DATABASE_URL` must be set (PostgreSQL required)
- Worker uses the same `event_outbox` table as the API server
- Claim visibility timeout defaults to 300 s; retry attempt limit defaults to 5
- No additional environment variables needed

**Note:** The worker is optional. If not running, events will accumulate in the
outbox table and can be delivered later when the worker starts.

### Submit an investigation (synchronous)

```bash
curl -s -X POST http://127.0.0.1:8000/investigations \
  -H "Content-Type: application/json" \
  -d '{"incident_id": "inc_01_n_plus_one_query"}'
```

> **Note:** Synchronous `POST /investigations` blocks until the full pipeline completes.
> Ensure `GROQ_API_KEY` is set and the incident directory exists under `incidents/`.
> The human approval gate is always active; proposed patches are never applied automatically.

### Submit an investigation (asynchronous)

Clients can opt into asynchronous investigation execution by supplying the standard
RFC 7240 `Prefer: respond-async` request header or `?async=true` query parameter:

```bash
# Opt-in via Prefer: respond-async header
curl -i -X POST http://127.0.0.1:8000/investigations \
  -H "Content-Type: application/json" \
  -H "Prefer: respond-async" \
  -d '{"incident_id": "inc_01_n_plus_one_query"}'

# Or via query parameter
curl -i -X POST "http://127.0.0.1:8000/investigations?async=true" \
  -H "Content-Type: application/json" \
  -d '{"incident_id": "inc_01_n_plus_one_query"}'
```

**202 Accepted Response:**
```http
HTTP/1.1 202 Accepted
Preference-Applied: respond-async
Location: http://127.0.0.1:8000/investigations/inc_01_n_plus_one_query
Content-Type: application/json

{
  "investigation_id": "inc_01_n_plus_one_query",
  "status": "RUNNING",
  "message": "Investigation 'inc_01_n_plus_one_query' accepted for asynchronous execution. Poll status_url or connect to events_url to track progress.",
  "status_url": "http://127.0.0.1:8000/investigations/inc_01_n_plus_one_query",
  "events_url": "http://127.0.0.1:8000/investigations/inc_01_n_plus_one_query/events"
}
```

> **Async Dispatch & Lifecycle Guarantees:**
> - Initial `InvestigationState` is durably persisted and execution lease acquired under a unique `worker_id` before returning `202 Accepted`.
> - On async resume, durable execution lease is acquired under PostgreSQL OCC before returning `202 Accepted`; competing resume claims return `409 Conflict`, never `202`.
> - The investigation is immediately queryable via `status_url` (`GET /investigations/{id}`).
> - Clients can immediately connect to `events_url` (`GET /investigations/{id}/events` with `Accept: text/event-stream`) without race conditions.
> - Background work executes via Starlette `BackgroundTask` in the server thread pool under the matching `worker_id`, preserving completed stages.
> - If an unhandled exception occurs in the background, state transitions to `FAILED`, the lease is released, and diagnostic errors are persisted.

### List and retrieve investigations

```bash
# List all stored investigation IDs
curl http://127.0.0.1:8000/investigations

# Retrieve a specific investigation state (including recorded approvals)
curl http://127.0.0.1:8000/investigations/inc_01_n_plus_one_query
```

### Retrieve the investigation result (diagnosis)

```bash
# Read-only, allowlisted projection of the diagnosis: hypotheses (+ verdicts),
# evidence (+ file:line references), per-check verification outcomes, and fix proposals.
curl -s http://127.0.0.1:8000/investigations/inc_01_n_plus_one_query/result
```

> **Result endpoint guarantees (`GET /investigations/{id}/result`):**
> - **Read-only.** It loads state and returns a projection; it never mutates investigation state and never applies patches.
> - **Allowlisted.** Only the declared fields are returned; the raw state payload, stage outputs, `metadata`, secrets, and `before`/`after` file bodies are never exposed.
> - **Traceable.** `reference` values are emitted verbatim as incident-relative `file:line` (e.g. `service/app.py:42`).
> - **Safe when incomplete.** In-progress or partial investigations return `200 OK` with empty sections and `result_available: false` rather than an error.
> - **Actionable.** The returned `proposal_id` values are accepted directly by `POST /investigations/{id}/approval`.
> - **Status codes.** `200` (incl. empty/in-progress), `400` invalid ID, `404` not found, `503` repository unavailable.

### Resume an interrupted/failed investigation

```bash
# Synchronous resume (default): blocks until resumed pipeline finishes
curl -s -X POST http://127.0.0.1:8000/investigations/inc_01_n_plus_one_query/resume

# Asynchronous resume (opt-in): returns 202 Accepted immediately and runs in background
curl -i -X POST http://127.0.0.1:8000/investigations/inc_01_n_plus_one_query/resume \
  -H "Prefer: respond-async"
```

> **Resume Rules & Lease-Based Crash Recovery:**
> - **Allowed statuses:** `FAILED`, `PARTIAL`, `PENDING`, and crashed `RUNNING` investigations whose execution lease has expired (`now >= lease_expires_at`).
> - **Preservation of Progress:** Resuming re-injects outputs from previously succeeded stages and executes only remaining stages.
> - **Active Lease Protection:** If an investigation is `RUNNING` and holds an active unexpired lease (`is_lease_active() == True`), resume returns `409 Conflict`.
> - **Terminal Conflict:** Investigations in `COMPLETED` status return `409 Conflict` (already concluded).

### Record human approval for a fix proposal

```bash
# Record an explicit human review decision on a proposed fix
curl -s -X POST http://127.0.0.1:8000/investigations/inc_01_n_plus_one_query/approval \
  -H "Content-Type: application/json" \
  -d '{
    "proposal_id": "FIX-001",
    "decision": "approved",
    "reviewer": "oncall-lead",
    "notes": "Verified root cause in staging environment."
  }'
```

> **Approval & Decision Integrity Rules:**
> - Non-interactive investigation runs generate automated placeholder rejections (`approved_by: "human"` with default rejection note).
> - Calling `POST /investigations/{id}/approval` records the genuine human decision (`approved` or `rejected`) with reviewer name and optional notes.
> - **Prior human decisions cannot be overwritten:** If an explicit human review is already recorded, subsequent approval calls return `409 Conflict`.
> - **Safety Guarantee:** Recording an approval records the decision metadata in durable state only. It **never** automatically applies patches, modifies repository files, executes shell commands, or deploys code.

### Investigation Event History & Live SSE Streaming

The `GET /investigations/{id}/events` endpoint provides deterministic access to investigation domain events via HTTP content negotiation (`Accept` header):

#### 1. JSON History Snapshot (`Accept: application/json`)

```bash
# Fetch complete historical event log as a JSON snapshot
curl -s http://127.0.0.1:8000/investigations/inc_01_n_plus_one_query/events \
  -H "Accept: application/json"
```

**JSON Response Schema (`EventHistoryResponse`):**
```json
{
  "investigation_id": "inc_01_n_plus_one_query",
  "total": 5,
  "events": [
    {
      "event_id": "evt_01J9XABCDEF1234567890",
      "investigation_id": "inc_01_n_plus_one_query",
      "event_type": "InvestigationStarted",
      "timestamp": "2026-10-10T12:00:00Z",
      "created_at": "2026-10-10T12:00:00Z",
      "processed_at": "2026-10-10T12:00:01Z",
      "status": "processed",
      "retry_count": 0,
      "error_message": null,
      "payload": {
        "event_id": "evt_01J9XABCDEF1234567890",
        "event_type": "InvestigationStarted",
        "timestamp": "2026-10-10T12:00:00Z",
        "investigation_id": "inc_01_n_plus_one_query",
        "initial_stage": "logs"
      }
    }
  ],
  "backend_supports_event_history": true
}
```

> **Backend Capability Note:**
> - When using `SENTINEL_PERSISTENCE_BACKEND=postgres`, event history is read from `event_outbox` in deterministic `(created_at ASC, event_id ASC)` order (`backend_supports_event_history: true`).
> - When using `SENTINEL_PERSISTENCE_BACKEND=filesystem`, the endpoint truthfully returns `backend_supports_event_history: false` and an empty `events: []` list.

#### 2. Live Server-Sent Events (SSE) Stream (`Accept: text/event-stream`)

```bash
# Connect to live SSE event stream
curl -N http://127.0.0.1:8000/investigations/inc_01_n_plus_one_query/events \
  -H "Accept: text/event-stream"

# Resume SSE stream after a previously seen event cursor
curl -N http://127.0.0.1:8000/investigations/inc_01_n_plus_one_query/events \
  -H "Accept: text/event-stream" \
  -H "Last-Event-ID: evt_01J9XABCDEF1234567890"

# Or via query parameter:
curl -N "http://127.0.0.1:8000/investigations/inc_01_n_plus_one_query/events?last_event_id=evt_01J9XABCDEF1234567890" \
  -H "Accept: text/event-stream"
```

**Wire Protocol & Stream Semantics:**
- **Wire Format**:
  ```
  id: evt_01J9XABCDEF1234567890
  event: StageCompleted
  data: {"event_id":"evt_01J9X...","event_type":"StageCompleted","stage":"logs","llm_calls":1,...}

  ```
- **Cursor Resolution & Keyset Pagination**: Uses keyset pagination querying batches of 200 events strictly after `(created_at, event_id)`. If an unresolvable cursor ID is supplied, the server returns HTTP 200, emits an explicit SSE comment notice, and restarts streaming from the beginning so no events are lost (never failing with HTTP 400).
- **Keepalives**: Emits periodic `: keep-alive\n\n` comments every 15s during idle polling intervals (0.5s default poll) to prevent intermediate proxy timeouts.
- **Terminal Stream Draining**: When an investigation transitions to a terminal state (`COMPLETED`, `FAILED`, `PARTIAL`), the stream continuously polls until all pending persisted events are drained, then closes the stream cleanly without emitting synthetic frames.
- **Client Disconnection**: The streaming generator actively checks `request.is_disconnected()` between polling cycles to release database connections and terminate gracefully.
- **Filesystem Backend**: If called when filesystem persistence is active, streams an informative notice comment and closes cleanly.

### Operational limitations

- **Localhost only by default.** Use `--host` to override (do not expose to the
  network without additional security controls).
- **Single-process concurrency.** Process-local active locking (`_ACTIVE_LOCK`) is
  not shared across multiple Uvicorn worker processes.
- **Optimistic Concurrency Control (OCC).** When using the PostgreSQL backend, concurrent
  state modifications raise `ConcurrencyError` mapped to HTTP `409 Conflict`.
- **Synchronous requests.** Each investigation occupies the server for the full pipeline
  duration. Concurrent requests for the same incident return `409 Conflict`.
- **Authentication is opt-in and static.** Without `SENTINEL_API_AUTH_ENABLED`, every route is
  unauthenticated (including the mutating endpoints and, if enabled, ingestion). When enabled it is
  a single shared key with no identity, roles, rotation, or rate limiting.

---

## Running Logs Agent (Evidence Only)
```bash
# Incident 01 — deterministic log tools + one Groq summarisation call
python -m agents.logs_agent incidents/inc_01_n_plus_one_query \
  --output eval/sample_runs/logs_agent_inc_01.json

# Incident 10 — multi-symptom case (observations only, not a root-cause diagnosis)
python -m agents.logs_agent incidents/inc_10_multi_symptom_cascade \
  --output eval/sample_runs/logs_agent_inc_10.json
```

Requires `GROQ_API_KEY` and locked model `openai/gpt-oss-120b`. The Logs Agent does not read `ground_truth.md` and does not run the Sentinel benchmark.

## Running Metrics Agent (Evidence Only)
```bash
# Incident 01 — database/latency metrics
python -m agents.metrics_agent incidents/inc_01_n_plus_one_query \
  --output eval/sample_runs/metrics_agent_inc_01.json

# Incident 03 — Kafka/consumer-lag metrics
python -m agents.metrics_agent incidents/inc_03_consumer_lag \
  --output eval/sample_runs/metrics_agent_inc_03.json

# Incident 10 — multi-symptom cascade (observations only, not a root-cause diagnosis)
python -m agents.metrics_agent incidents/inc_10_multi_symptom_cascade \
  --output eval/sample_runs/metrics_agent_inc_10.json
```

Requires `GROQ_API_KEY` and locked model `openai/gpt-oss-120b`. The Metrics Agent does not read `ground_truth.md` and does not run the Sentinel benchmark.

## Running Code Agent (Evidence Only)
```bash
# Incident 01 — N+1 query pattern inside serializer loop
python -m agents.code_agent incidents/inc_01_n_plus_one_query \
  --output eval/sample_runs/code_agent_inc_01.json

# Incident 04 — memory-related unbounded registry code change
python -m agents.code_agent incidents/inc_04_memory_leak \
  --output eval/sample_runs/code_agent_inc_04.json

# Incident 07 — aggressive retry behaviour (10 retries, zero backoff)
python -m agents.code_agent incidents/inc_07_retry_storm \
  --output eval/sample_runs/code_agent_inc_07.json

# Incident 10 — hard multi-symptom cascade (observations only, not a root-cause diagnosis)
python -m agents.code_agent incidents/inc_10_multi_symptom_cascade \
  --output eval/sample_runs/code_agent_inc_10.json
```

Requires `GROQ_API_KEY` and locked model `openai/gpt-oss-120b`. The Code Agent does not read `ground_truth.md` and does not run the Sentinel benchmark.

## Running Single Baseline Diagnosis
```bash
# Diagnose one incident (requires GROQ_API_KEY in .env)
python -m baseline.baseline_agent incidents/inc_01_n_plus_one_query

# Save output to file
python -m baseline.baseline_agent incidents/inc_01_n_plus_one_query \
  --output eval/sample_runs/baseline_sample_inc_01.json
```

## Running Full Baseline Evaluation (All 10 Incidents)
```bash
# Full evaluation — sleeps 3s between incidents to respect free-tier rate limits
python -m eval.run_eval --mode baseline --sleep 3

# Run only a specific incident
python -m eval.run_eval --mode baseline --incident inc_01_n_plus_one_query

# Run incidents 1–5 only
python -m eval.run_eval --mode baseline --start 1 --end 5 --sleep 3

# Re-run everything from scratch (discard cached results)
python -m eval.run_eval --mode baseline --no-resume --sleep 3
```

> **Rate limit note**: The harness saves results after each incident. If a rate-limit error occurs,
> re-run the same command — already-completed incidents are automatically skipped.

Results are written to:
- `eval/results/baseline/<incident_id>.json` — per-incident raw diagnosis (no ground truth)
- `eval/results_baseline.csv` — full results with correctness verdicts
- `eval/baseline_summary.json` — accuracy, token usage, and model lock

## Running the Hypothesis Engine (requires Groq)
```bash
# Generate hypotheses from pre-extracted evidence bundles
python agents/hypothesis_engine.py \
  --incident-id inc_01_n_plus_one_query \
  --logs eval/sample_runs/logs_agent_inc_01.json \
  --metrics eval/sample_runs/metrics_agent_inc_01.json \
  --code eval/sample_runs/code_agent_inc_01.json \
  --out eval/sample_runs/hypothesis_inc_01.json
```

Rules: one Groq call only; 1–4 hypotheses; every evidence_id must be present in inputs.

## Running the Verification Agent (No Groq)
```bash
# Verify hypotheses against the incident bundle using deterministic read-only checks
python agents/verification_agent.py \
  incidents/inc_10_multi_symptom_cascade \
  --hypotheses eval/sample_runs/hypothesis_inc_10.json \
  --logs eval/sample_runs/logs_agent_inc_10.json \
  --metrics eval/sample_runs/metrics_agent_inc_10.json \
  --code eval/sample_runs/code_agent_inc_10.json \
  --out eval/sample_runs/hypothesis_verification_inc_10.json
```

Verification is pure Python: AST scanning, log/metric CSV parsing, git-diff line counting, and referenced-excerpt grounding. No mutations, no shell execution, no LLM. Each hypothesis verdict is strictly computed from check PASS/FAIL counts.

## Running the Full Hypothesis + Verification Chain (Incidents 01/04/07/10)
```bash
# Produces hypothesis_*.json and hypothesis_verification_*.json under eval/sample_runs/.
# Reuses existing evidence samples when present; generates missing logs/metrics samples first.
# Requires GROQ_API_KEY only when evidence samples or hypotheses need generation.
python run_hypothesis_verification.py
```

## Running Advanced Sentinel (Full Orchestrator Pipeline)
```bash
python -m eval.run_sentinel_eval --sleep 2 --start 1 --end 10
```


## Running the Sentinel Orchestrator (Full Pipeline)

```bash
# Run the complete Sentinel investigation pipeline on one incident.
# Non-interactive mode: approval gate auto-rejects all proposals.
# --sleep 3 respects the free-tier RPM rate limit between LLM-heavy stages.
# --cache-dir enables resumability: completed stages are reused on reruns.

python -m agents.orchestrator incidents/inc_01_n_plus_one_query \
  --non-interactive --sleep 3 \
  --cache-dir eval/results/sentinel \
  --output eval/sample_runs/orchestrator/orchestrator_inc_01.json

# Re-running after a rate limit: cached stages are reused automatically.
# Only stages that failed (or were never run) are re-attempted.

python -m agents.orchestrator incidents/inc_04_memory_leak \
  --non-interactive --sleep 3 \
  --cache-dir eval/results/sentinel \
  --output eval/sample_runs/orchestrator/orchestrator_inc_04.json
```

> **Rate limit note:** The free-tier 200k tokens/day (TPD) limit is easily reached when running 4
> large incidents back-to-back. Use `--cache-dir` to avoid re-running completed stages. If the fix-
> proposal stage fails with a TPD error, wait for the daily reset and rerun — all earlier stages
> will be restored from cache at zero token cost.

## Running the Final Comparative Benchmark (Milestone 10 — Sentinel vs Baseline, All 10 Incidents)

```bash
# Produces:
#   eval/results/sentinel/<incident_id>/sentinel_final.json  (per-incident result)
#   eval/final_comparison.csv          (10 rows, baseline vs sentinel per incident)
#   eval/final_summary.json            (aggregate numbers + safety + integrity block)
#
# Resumable: existing sentinel_final.json are reused.
# Rate-limit safe: sleeps between LLM-heavy stages, preserves completed results.
# Uses CorrectnessEvaluator only AFTER the pipeline has finished (ground-truth isolation).
# Sentinel runtime never reads baseline results or ground truth.

python -m eval.run_sentinel_eval --sleep 2 --start 1 --end 10

# Single-incident check
python -m eval.run_sentinel_eval --incident inc_10_multi_symptom_cascade

# Ignore cache and force a fresh pipeline run for one incident
python -m eval.run_sentinel_eval --incident inc_10_multi_symptom_cascade --force-rerun
```

**Post-benchmark integrity normalization (ensures baseline numbers come from the locked
`baseline_summary.json` rather than being recomputed from potentially-incomplete CSV rows):**

```bash
python scripts/_normalize_final_benchmark.py
```

### Final Artifact Structure
```
eval/
├── results_baseline.csv          ← LOCKED (10 rows; 10/10 correct from commit e999b2a)
├── baseline_summary.json         ← LOCKED authoritative baseline aggregator
├── final_comparison.csv          ← 10 rows; baseline_vs_sentinel per incident
├── final_summary.json            ← aggregate numbers, accuracy_Δ, verification stats, safety
└── results/sentinel/<inc_id>/
    ├── logs.json                  ← Logs Agent evidence output (cached)
    ├── metrics.json               ← Metrics Agent evidence output (cached)
    ├── code.json                  ← Code Agent evidence output (cached)
    ├── evidence_fusion.json       ← Fusion stage (zero Groq)
    ├── hypotheses.json            ← Hypothesis Engine (1× Groq)
    ├── verification.json          ← Verification Agent (zero Groq)
    └── sentinel_final.json        ← Final result + evaluator verdict for this incident
```

### Benchmark Integrity Rules (Milestone 10)
1. **Incidents never touched.** `git diff -- incidents/` must be empty after the run.
2. **Ground-truth isolated.** Only `eval.evaluator.CorrectnessEvaluator` opens `ground_truth.md` and only *after* the pipeline finishes.
3. **Baseline isolated.** Sentinel runtime never opens `results_baseline.csv`, `baseline_summary.json`, or `eval/results/baseline/`.
4. **Accuracy denominator = 10** for both systems. Failures/PARTIAL status are NOT removed.
5. **Approval gate auto-rejection is NOT an investigation failure** (per §5). Incident 10 reports `PARTIAL` status because the fix-proposal stage was rate-limited; the diagnosis verdict still counts because hypotheses + verification produced a CONFIRMED root cause.
6. **All Sentinel verification is Groq-free.** Verification = 0 LLM calls (pure AST + CSV + git-diff + excerpt grounding).
