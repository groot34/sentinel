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
| Automated tests | — | **884 passed, 55 skipped** offline (Mission 19); **853 passed, 0 skipped** live PostgreSQL 16 CI (run 39635055973, Missions 17 & 18) |
| Incident validation checks | — | **51 passed** |
| Patches auto-applied | — | **0** |

Sentinel's demonstrated advantage is **verification rigor**, not raw diagnostic accuracy.
Both systems diagnose all 10 incidents correctly. Only Sentinel can prove it.
*(Note: without PostgreSQL or Windows symlink privileges, 55 tests skip — 46 PostgreSQL live integration tests (including Mission 15's 4 retry tests and Mission 16's 6 outbox claim/recovery tests) plus 9 Windows symlink tests (2 API path-containment tests + 6 bundle-validator symlink/escape tests + 1 ingestion symlinked-target test), giving 884 passed / 55 skipped offline. With Docker (`make db-up && make db-migrate`), all PostgreSQL integration tests execute. Mission 15 was verified green in GitHub Actions on both [run 39048469043](https://github.com/groot34/sentinel/actions/runs/39048469043) (commit `fa4f1c7`) and [run 39049346329](https://github.com/groot34/sentinel/actions/runs/39049346329) (commit `1d55db8`): `offline` job 650 passed / 4 skipped, `postgres:16-alpine` job 733 passed / 0 skipped. Mission 16 was verified green in GitHub Actions [run 39277794918](https://github.com/groot34/sentinel/actions/runs/39277794918) (commit `aba70e1`): `offline` job 654 passed / 4 skipped, `postgres:16-alpine` job 743 passed / 0 skipped, with skip enforcement reporting `Total skipped: 0` so all 6 live outbox tests executed. Missions 17 and 18 (12 result-endpoint tests + 98 bundle-validator tests) were verified green in GitHub Actions [run 39635055973](https://github.com/groot34/sentinel/actions/runs/39635055973) (commit `07a72d5`): `postgres:16-alpine` job 853 passed / 0 skipped. Mission 19 adds 86 incident-ingestion API tests, taking the suite from 853 to 939 collected; Mission 19 has **local validation only** — no CI run yet, and PostgreSQL was **not** tested locally.)*

---

## 1. Problem

> **CI Status**: Two-job GitHub Actions workflow enforces both offline correctness and live PostgreSQL integration on every push and pull request. See `.github/workflows/ci.yml`.


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
                                     │ • Atomic FOR UPDATE SKIP LOCKED   │
                                     │   claims (multi-worker safe)      │
                                     │ • At-least-once outbox delivery   │
                                     │ • Durable DB-scheduled retries    │
                                     │ • Claim-expiry crash recovery     │
                                     └───────────────────────────────────┘
```

- **Domain State**: Typed `InvestigationState` with integer `version` field for optimistic concurrency control (OCC).
- **Execution Leases**: Leases protect active investigations against concurrent worker collisions. Crashed workers' leases expire after TTL, allowing safe crash recovery.
- **Transactional Outbox**: Atomic database transactions commit state and domain events together in PostgreSQL. Duplicate event IDs are handled with `ON CONFLICT DO NOTHING`, so they never abort a valid state change.
- **Event Delivery Worker**: Background daemon ensuring reliable delivery to event subscribers. Workers atomically claim disjoint batches (`FOR UPDATE SKIP LOCKED`), retry with durable database-scheduled backoff, and recover abandoned claims after a crash. Delivery is **at-least-once**; consumers must be idempotent and key on `event_id`.
- **REST & SSE API**: Full investigation lifecycle management, human approval recording, and live streaming via Server-Sent Events.

### 6.2 Incident Bundle Contract

An **incident bundle** is a directory whose name is the incident ID. Commands accept it by
path (`python -m agents.orchestrator --incident-dir incidents/<incident_id>`) and the API
accepts it by ID (`POST /investigations {"incident_id": "<incident_id>"}`, resolved under
`--incidents-root`, default `incidents/`).

There are two ways to get a bundle under the incident root: place the directory on the server
filesystem yourself, or — when the opt-in ingestion endpoint is enabled —
`POST /incidents` to submit the text files as JSON (see
[§10.9 Ingest an Incident Bundle](#9-ingest-an-incident-bundle-mission-19--opt-in-disabled-by-default)).
Ingestion writes exactly the same bundle layout and enforces a strict subset of this contract.

```
incidents/<incident_id>/
├── logs/
│   └── application.log      # optional — log evidence
├── metrics/
│   └── metrics.csv          # optional — metric evidence
├── git_diff.patch           # optional — unified diff evidence
├── service/                 # optional — source tree evidence
│   ├── app.py
│   └── tests/test_service.py
├── docker-compose.yml       # optional — deployment context
└── ground_truth.md          # evaluation only — NEVER read at runtime
```

| Entry | Required | Format | Notes |
|---|---|---|---|
| incident directory | **yes** | directory named `<incident_id>` | The directory itself is the only required entry |
| `logs/application.log` | no | one log line per line | Lines opening with an ISO-8601 timestamp (`2026-08-28T14:00:00Z`) carry a timestamp; level tokens `INFO`/`WARN`/`WARNING`/`ERROR`/`FATAL`/`DEBUG`/`TRACE`/`CRITICAL` and `[service]` markers are parsed. Decoded with `errors="ignore"`, so it is Unicode-tolerant but never fatal |
| `metrics/metrics.csv` | no | wide CSV | Header row required; a `timestamp` column is required (ISO-8601 `…Z`); all remaining columns must be numeric. Series are short (typically 5–9 samples). Row references are 1-indexed **file lines including the header**, so `metrics/metrics.csv:row 2` is the first data row |
| `git_diff.patch` | no | unified diff | References are `git_diff.patch:hunk N` when added lines interleave context |
| `service/` | no | Python source tree | `__pycache__`, `tests/`, and `.pytest_cache` are skipped; `ground_truth.md` / `ground_truth.json` are never read |
| `docker-compose.yml` | no | YAML | Consumed only if present; not evidence on its own |
| `ground_truth.md` | evaluation only | markdown | Present in the 10 canonical fixtures for scoring. **Runtime code never reads it** |

**Incident IDs.** A valid ID matches `^[a-zA-Z0-9][a-zA-Z0-9_-]{0,127}$` and may not contain
`/`, `\`, `..`, or `:`. The API additionally requires the resolved bundle directory's parent to be
the permitted incident root and its name to equal the incident ID, so `../x`, `sub/id`, and
symlinked bundle directories are rejected with `400`.

**Fatal vs. partial.** A bundle is *not* required to carry every evidence source — the pipeline
is designed for partial evidence. These conditions are **fatal** (the run never starts, and no
LLM credits are spent):

| Fatal condition | Surfaced as |
|---|---|
| Bundle path does not exist | `FileNotFoundError` (CLI) / `404` when missing under the incident root (API) |
| Bundle path is not a directory | `NotADirectoryError` |
| Directory name is not a valid incident ID, or differs from the expected ID | `invalid_incident_id` / `incident_id_mismatch` → `400 invalid_incident_bundle` |
| Bundle directory is a symlink, or an entry symlink escapes the bundle root | `bundle_symlink` / `bundle_escapes_root` |
| A channel directory (`logs/`, `metrics/`) exists but its expected file is absent | `required_file_missing` |
| An entry's type contradicts the contract (e.g. `service/` as a file) | `not_a_directory` / `not_a_file` |
| `metrics/metrics.csv` is present but empty, headerless, missing its `timestamp` column, or has a non-numeric/missing value | `metrics_csv_empty` / `metrics_csv_invalid` (message names the metric and the offending row) |
| `metrics/metrics.csv` has a `timestamp` value that is not an ISO-8601 UTC instant ending in `Z` (e.g. `2026-08-28T14:00:00Z`) | `metrics_timestamp_invalid` (message names the offending 1-indexed file row) |

These conditions are **legitimate partial evidence** and are explicitly *not* fatal: any of
`logs/application.log`, `metrics/metrics.csv`, `service/`, `git_diff.patch`, or
`docker-compose.yml` absent; an empty log file (warning `empty_log_file`); a bundle with no
evidence at all (warning `no_evidence_sources`). In every case the affected agent returns an
empty result and the pipeline continues.

**Preflight validation.** The contract is enforced deterministically by
`agents/bundle_validator.py` (zero LLM calls, read-only — it opens only
`logs/application.log` and `metrics/metrics.csv` and never enumerates the bundle). Containment is
verified **before** any evidence file is opened, so a canonical evidence path that resolves outside
the bundle root is reported as `bundle_escapes_root` but is never read or parsed.
`IncidentOrchestrator._validate_incident_dir()` calls it at the top of both `investigate()`
and `resume()`, **before any stage — and therefore before any Groq call**. The API maps a
violation to `400 invalid_incident_bundle`. *Residual limitation:* the bundle is validated before
the pipeline re-reads it, so a symlink swapped in between the check and the read is not re-checked
(a narrow TOCTOU window) — documented rather than closed by architectural change. You can check a
bundle without starting an investigation:

```bash
python -m agents.bundle_validator incidents/inc_01_n_plus_one_query --incident-id inc_01_n_plus_one_query
# Bundle:        incidents\inc_01_n_plus_one_query
# Incident ID:   inc_01_n_plus_one_query
# Evidence:      logs/application.log, metrics/metrics.csv, git_diff.patch, service
# Result:        VALID
```

Exit code `0` means valid (warnings allowed); exit code `1` means at least one fatal error.

**Minimal example.** A bundle with log evidence only is valid — this is the smallest useful
incident:

```
incidents/my_incident/
└── logs/
    └── application.log
```

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
# Verified in CI (run 39048469043, Mission 15): 733 passed, 0 skipped with live PostgreSQL 16.
# Verified locally without a database (Mission 19): 884 passed, 55 skipped, 1 warning in ~18s
# (55 skipped: 46 PostgreSQL live integration tests skipped when SENTINEL_DATABASE_URL is
#  unset/unreachable — including Mission 15's 4 transient-failure retry tests and Mission 16's
#  6 outbox claim/recovery tests; 9 Windows symlink tests skipped for non-admin accounts
#  without Developer Mode — 2 in tests/test_api.py, 6 in tests/test_bundle_validator.py and
#  1 in tests/test_incident_ingestion_api.py.
#  Mission 14 added 18 tests beyond the 652/32 Mission 13 baseline; Mission 15 added 25
#  tests in tests/test_pg_retry.py (21 offline, 4 live PostgreSQL); Mission 16 added 10 tests
#  — 4 offline worker tests in tests/test_event_worker.py and 6 live PostgreSQL outbox tests in
#  tests/test_outbox.py; Mission 17 added 12 result-endpoint tests in
#  tests/test_investigation_result_api.py; Mission 18 added 98 bundle-validator tests in
#  tests/test_bundle_validator.py; Mission 19 added 86 incident-ingestion API tests in
#  tests/test_incident_ingestion_api.py, 85 passing + 1 symlink test skipped on Windows.)
# With make db-up && make db-migrate, all PostgreSQL tests execute locally (0 skipped).
# Mission 16 verified in CI (run 39277794918, commit aba70e1): offline 654 passed / 4 skipped;
#  postgres job (PostgreSQL 16) 743 passed / 0 skipped (Total skipped: 0) — all 6 live outbox tests ran.
# Missions 17, 18 and 19 have local validation only — no CI run yet; PostgreSQL was not tested locally.

# Run the incident dataset invariant suite
python tests/validate_incidents.py
# Verified result: 51/51 passed across all 10 canonical incidents

# Validate an incident bundle against the documented contract (no LLM, no API key)
python -m agents.bundle_validator incidents/inc_01_n_plus_one_query --incident-id inc_01_n_plus_one_query
# Verified result: "Result: VALID" with evidence logs/application.log, metrics/metrics.csv, git_diff.patch, service
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

### Configuration

The launcher calls `load_dotenv()` before reading any environment variable, so values in `.env`
are applied automatically (shell values take precedence).

| Variable | Default | Purpose |
|---|---|---|
| `SENTINEL_PERSISTENCE_BACKEND` | `filesystem` | `filesystem` or `postgres` |
| `SENTINEL_PERSISTENCE_ROOT` | `.sentinel_persistence` | Root dir for the filesystem backend |
| `SENTINEL_DATABASE_URL` | — | PostgreSQL connection URL (`postgres` backend only) |
| `GROQ_API_KEY` / `GROQ_MODEL` | — / `openai/gpt-oss-120b` | Required when investigations are submitted |
| `SENTINEL_ALLOW_INCIDENT_INGESTION` | `false` | **Opt-in** switch for `POST /incidents` (Mission 19). Any value other than `1`/`true`/`yes`/`on` leaves it disabled |
| `SENTINEL_INGEST_MAX_FILES` | `200` | Max files per ingested bundle |
| `SENTINEL_INGEST_MAX_FILE_BYTES` | `2097152` | Max UTF-8 bytes per ingested file |
| `SENTINEL_INGEST_MAX_TOTAL_BYTES` | `20971520` | Max total UTF-8 bytes per ingested bundle |
| `SENTINEL_INGEST_MAX_REQUEST_BYTES` | `25165824` | Max raw JSON request body size |

### Core API Endpoints & Examples

#### 1. System Health Check
```bash
curl -s http://127.0.0.1:8000/health
# {"status":"ok","service":"sentinel","version":"2.0"}
```
Returns the allowlisted liveness payload only (`status`, `service`, `version`); it does not report
persistence backend or outbox depth.

#### 2. Create & Run an Investigation

**Synchronous Execution (Default, blocks until pipeline completes):**
```bash
curl -s -X POST http://127.0.0.1:8000/investigations \
  -H "Content-Type: application/json" \
  -d '{"incident_id": "inc_01_n_plus_one_query"}'
```

**Asynchronous Execution (Opt-in via `Prefer: respond-async` or `?async=true`):**
```bash
curl -i -X POST http://127.0.0.1:8000/investigations \
  -H "Content-Type: application/json" \
  -H "Prefer: respond-async" \
  -d '{"incident_id": "inc_01_n_plus_one_query"}'
```
Returns `202 Accepted` immediately with `AcceptedInvestigationResponse`:
```json
{
  "investigation_id": "inc_01_n_plus_one_query",
  "status": "RUNNING",
  "message": "Investigation 'inc_01_n_plus_one_query' accepted for asynchronous execution. Poll status_url or connect to events_url to track progress.",
  "status_url": "http://127.0.0.1:8000/investigations/inc_01_n_plus_one_query",
  "events_url": "http://127.0.0.1:8000/investigations/inc_01_n_plus_one_query/events"
}
```
Response headers include:
- `Preference-Applied: respond-async`
- `Location: http://127.0.0.1:8000/investigations/inc_01_n_plus_one_query`

Initial state is persisted and lease claimed before the `202 Accepted` response is returned. Clients can immediately poll `status_url` or connect to `events_url` (SSE stream) to observe live progress as the pipeline executes in the background.

#### 3. List Investigations
```bash
curl -s http://127.0.0.1:8000/investigations
# {"investigations":["inc_01_n_plus_one_query", ...]}
```
Returns every stored investigation ID via `repository.list()`. The endpoint does **not** support
pagination (`limit`/`offset` query parameters are ignored).

#### 4. Retrieve Investigation Details
```bash
curl -s http://127.0.0.1:8000/investigations/<investigation_id>
```

#### 5. Resume an Investigation
```bash
# Synchronous resume (default): blocks until resumed pipeline finishes
curl -s -X POST http://127.0.0.1:8000/investigations/<investigation_id>/resume

# Asynchronous resume (opt-in): returns 202 Accepted and runs in background
curl -s -X POST http://127.0.0.1:8000/investigations/<investigation_id>/resume \
  -H "Prefer: respond-async"
```

#### 6. Record Human Approval Decision
```bash
# Record an explicit human decision on generated fix proposals
# IMPORTANT: This records the decision ONLY; it NEVER modifies code files or applies patches
curl -s -X POST http://127.0.0.1:8000/investigations/<investigation_id>/approval \
  -H "Content-Type: application/json" \
  -d '{"proposal_id": "FIX-001", "decision": "approved", "reviewer": "sre-oncall@example.com", "notes": "Verified against staging"}'
```
The request body is strictly `{proposal_id, decision, reviewer, notes?}`; unknown fields are rejected
(`400`). `decision` accepts `approved` / `rejected` and `proposal_id` values come from
`GET /investigations/{id}/result`.

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

#### 8. Retrieve Investigation Result

The `GET /investigations/{id}/result` endpoint returns a read-only, allowlisted projection of the investigation's diagnosis — the part `GET /investigations/{id}` intentionally omits.

```bash
curl -s http://127.0.0.1:8000/investigations/<investigation_id>/result
```

Returns `200 OK` with `InvestigationResultResponse`:

- `hypotheses`: `hypothesis_id`, `claim`, linked `evidence_ids`, `verdict` (`CONFIRMED` / `REJECTED` / `INCONCLUSIVE`, or `null` if not yet verified).
- `evidence`: `evidence_id`, `source`, `type`, `reference` (incident-relative `file:line`), `description`, `excerpt`.
- `verification`: per-hypothesis `verdict` plus individual `checks` with `check_id`, `result` (`PASS` / `FAIL` / `INCONCLUSIVE`) and `reference`.
- `proposals`: `proposal_id`, `summary`, `rationale`, unified-diff `patch`, `risks`, `rollback_plan`.

`result_available` is `true` only when at least one section is populated. In-progress or partial investigations return empty sections rather than an error, and the returned `proposal_id` values can be passed straight to `POST /investigations/{id}/approval`. Internal state, raw stage outputs, `before`/`after` file bodies and evidence `metadata` are never exposed.

#### 9. Ingest an Incident Bundle (Mission 19 — opt-in, disabled by default)

`POST /incidents` lets a client submit a **text-only** incident bundle as JSON so it no longer
needs direct write access to the server filesystem. It is **disabled by default** and must be
enabled explicitly (see [Configuration](#configuration)); enabling it does **not** add
authentication, so a public deployment must add its own access control.

```bash
# 1) Enable once (server env): SENTINEL_ALLOW_INCIDENT_INGESTION=true
# 2) Ingest a bundle (paths are the allowlisted incident-relative paths)
curl -s -X POST http://127.0.0.1:8000/incidents \
  -H "Content-Type: application/json" \
  -d '{
    "incident_id": "inc_acme_oom",
    "files": {
      "logs/application.log": "2026-08-28T14:00:00Z ERROR pool exhausted\n",
      "metrics/metrics.csv": "timestamp,mem_mb\n2026-08-28T14:00:00Z,1024\n",
      "service/app.py": "def handler():\n    pass\n"
    }
  }'
# 201 Created
# Location: /investigations/inc_acme_oom
# {"incident_id":"inc_acme_oom","bundle_path":"incidents/inc_acme_oom",
#  "evidence_sources":["logs/application.log","metrics/metrics.csv","service"]}
```

The response carries the `incident_id`, an incident-relative `bundle_path`, the submitted
`evidence_sources`, and a `Location` header pointing at the existing investigation resource.
**Ingestion never starts an investigation** — the client continues with the normal workflow:

```bash
# 3) Start the investigation (existing endpoint; sync or async)
curl -s -X POST http://127.0.0.1:8000/investigations \
  -H "Content-Type: application/json" -d '{"incident_id": "inc_acme_oom"}'

# 4) Poll or stream, then read the diagnosis
curl -s http://127.0.0.1:8000/investigations/inc_acme_oom
curl -N http://127.0.0.1:8000/investigations/inc_acme_oom/events -H "Accept: text/event-stream"
curl -s http://127.0.0.1:8000/investigations/inc_acme_oom/result
```

**Request contract.** A strict JSON object with `incident_id` (canonical ID rules) and `files`
(a **non-empty** object mapping relative POSIX paths to UTF-8 strings). An optional boolean
`overwrite` field exists for forward compatibility but **must be omitted or `false`**:
replacement is not implemented, so `overwrite=true` is rejected with `409 overwrite_not_supported`
(see *Honest limitations*). Unknown fields, non-string contents, duplicate JSON keys, and
malformed bodies are rejected.

**Allowed paths (strict allowlist).** Only `logs/application.log`, `metrics/metrics.csv`,
`git_diff.patch`, `docker-compose.yml`, and any file beneath `service/` may be submitted.
Absolute paths, `..`, backslashes, Windows drive prefixes, NUL bytes, empty path segments, paths
longer than 512 characters or deeper than 16 segments, reserved evaluation artefacts
(`ground_truth.md`, `ground_truth.json`, `results_baseline.csv`, `baseline_summary.json`), and any
path outside the allowlist are rejected with `400 invalid_file_path`.

**Resource limits** (environment-configurable; UTF-8 **bytes**, not characters):

| Limit | Default | Environment variable |
|---|---|---|
| Maximum files | 200 | `SENTINEL_INGEST_MAX_FILES` |
| Maximum size per file | 2 MiB | `SENTINEL_INGEST_MAX_FILE_BYTES` |
| Maximum total content | 20 MiB | `SENTINEL_INGEST_MAX_TOTAL_BYTES` |
| Maximum raw request body | 24 MiB | `SENTINEL_INGEST_MAX_REQUEST_BYTES` |

Limits are checked against the parsed content **before** any staging file is written. The raw
request-body limit is enforced **while the body is being read**: the request stream is consumed
chunk by chunk and reading stops the moment the limit is exceeded, so an oversized or chunked body
is never fully buffered or parsed. `Content-Length` is never trusted as authoritative — it may be
absent (chunked) or understated — so the limit is always re-checked against the bytes actually
received. Exceeding any limit returns `413 payload_too_large`.

**Error responses.**

| Status | `error` | When |
|---|---|---|
| `404` | `ingestion_disabled` | Endpoint not enabled (default). No filesystem access occurs |
| `400` | `invalid_json` | Malformed body, non-object body, or duplicate JSON key |
| `400` | `invalid_ingest_request` | Request shape violation (unknown field, empty/absent `files`, non-string content, non-boolean `overwrite`, invalid `incident_id`) |
| `400` | `invalid_file_path` | Path policy violation (see allowlist above) |
| `400` | `invalid_incident_bundle` | Bundle fails the Mission 18 preflight validator |
| `409` | `conflict` | Incident already exists (replacement unsupported), a concurrent ingestion for the same ID is in flight, or the incident is running / actively leased |
| `409` | `overwrite_not_supported` | `overwrite=true` was requested. Rejected before any staging file is written |
| `413` | `payload_too_large` | A configured size or count limit was exceeded |
| `503` | `storage_unavailable` | Incident root missing/unwritable, or a staging/publish write failed |

Error bodies never include file contents, secrets, evaluation ground truth, or absolute server
filesystem paths (messages pass through `sanitize_error_message`).

**Safety & atomicity.** Files are written as regular UTF-8 text files beneath a fresh, unique
staging directory inside the incident root, with containment verified before every write and
exclusive-create semantics so no client-controlled path or symlink is ever followed. The staged
bundle must then pass `require_valid_incident_bundle(staged_root, expected_id=<incident_id>)`
before it is published. Any failure removes the staging tree and publishes nothing, so a client
never observes a partially written bundle. Publication is a **single atomic `rename`** that only
ever creates a new incident directory: an existing entry at the target path — regular directory,
file, or symlink — is reported as `409 conflict` and left completely untouched. The rename is the
sole publish step, so it is the publish — not any in-process lock — that makes two concurrent
ingestions of the same new ID exclusive **across processes**: the loser's rename fails against the
now-existing directory and is mapped to a conflict. This holds unconditionally because a published
bundle always contains at least one file (the service rejects an empty bundle), so the target is
never the empty directory that POSIX `rename` would overwrite; a losing publish never mixes with or
mutates the winner's files.

**Overwrite is not supported.** `overwrite=true` is rejected with `409 overwrite_not_supported`
before any staging file is written. There is no portable *crash-safe*, cross-process directory-swap
primitive: a best-effort "move aside, move in, restore on failure" swap could permanently destroy
the previous valid bundle if the process died mid-replacement. Rather than claim a guarantee it
cannot honour, Sentinel refuses replacement. To re-ingest an incident, submit a **new**
`incident_id`.

**Honest limitations.**
- An incident is created once. A duplicate `incident_id` conflicts (`409`); replacement requires a
  genuinely crash-safe protocol that does not exist yet, so it is declined instead of approximated.
- Coordination with running investigations is **process-local** (the same `_ACTIVE_LOCK` /
  `_ACTIVE_INVESTIGATIONS` registry used by `POST /investigations`) plus the persisted execution
  lease. Publishing a bundle for an incident that is running or actively leased is refused
  (`409 conflict`). Across multiple Uvicorn worker processes only the durable lease is visible; the
  single-rename publish keeps a cross-process duplicate from corrupting the bundle (the second
  rename fails and conflicts). A published bundle is never an empty directory (the service refuses
  an empty bundle), so POSIX `rename` can never replace a published target; the exclusive-publish
  guarantee therefore does not rely on the in-process lock or on the two submissions being
  byte-identical.
- Enabling the endpoint without authentication permits anyone who can reach the server to publish
  incidents. Keep the default bind address (`127.0.0.1`) or add real authentication/authorization in
  front of it.

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
| **Event Worker** | Not applicable | Background daemon with atomic multi-worker claims, durable DB-scheduled retry backoff, and claim-expiry crash recovery (Mission 16) |
| **Transient Failure Recovery** | Not applicable | Bounded retry of the whole `save()` transaction on SQLSTATE `40001` / `40P01` (Mission 15) |

### 11.1 Transient PostgreSQL Failure Recovery (Mission 15)

`PostgresRepository.save()` retries the **entire** transaction on transient
PostgreSQL contention errors — SQLSTATE `40001` (`serialization_failure`) and
`40P01` (`deadlock_detected`) — with a conservative, bounded exponential backoff
plus full jitter.

- **Retried**: `40001`, `40P01` only (whole transaction, fresh connection per attempt).
- **Never retried**: OCC `ConcurrencyError` (`409 Conflict`), constraint violations, validation/safety failures.
- **Defaults**: `SENTINEL_PG_RETRY_MAX_ATTEMPTS=5`, `SENTINEL_PG_RETRY_BASE_DELAY=0.05` s, `SENTINEL_PG_RETRY_MAX_DELAY=1.0` s (ceiling ≤ 30 s). Invalid values are clamped, never crash construction.
- **Exhaustion**: raises `RetryExhaustedError` (subclass of `PersistenceError`) → API `503 persistence_unavailable`; the in-memory state version is restored.
- **Boundary**: retry applies only to `save()`; re-running it cannot duplicate external side effects, and OCC/lease/outbox atomicity are preserved. Each attempt restores `state.version` to its entry value, so retries never double-increment the version or duplicate outbox events.
- **Isolation caveat**: no explicit isolation level is set (server default **READ COMMITTED**), so no serialisable/phantom-read guarantee is claimed. Because READ COMMITTED does not naturally emit `40001`, tests use a deterministic fault-injection seam (`core.persistence.retry.inject_transient_fault`, a no-op in production) alongside live PostgreSQL integration tests.
- **Verified**: 21 offline retry tests pass locally; the 4 live PostgreSQL 16 integration tests executed and passed in CI's `postgres` job ([run 39048469043](https://github.com/groot34/sentinel/actions/runs/39048469043), 733 passed / 0 skipped).

### 11.2 Concurrent & Crash-Safe Outbox Delivery (Mission 16)

Mission 16 replaces the previous read-then-deliver outbox loop with an atomic,
crash-safe claim protocol so that multiple worker processes can deliver events
without double-claiming or losing work.

- **Atomic claims**: `PostgresRepository.claim_pending()` locks eligible rows in
  canonical order with `SELECT ... FOR UPDATE SKIP LOCKED`, then claims exactly
  those rows. Concurrent workers therefore receive **disjoint** batches; locks
  are held until commit. Ordering is deterministic: `(created_at ASC, event_id ASC)`.
- **Durable retry scheduling**: each claim carries a persisted `next_attempt_at`.
  Failed deliveries are rescheduled with bounded exponential backoff and full
  jitter (defaults `base=2.0s`, `max=60.0s`) written to the database — **no
  sleeping occurs inside the batch loop**, and retry timing survives worker
  restarts. Events with a future `next_attempt_at` are not claimable.
- **Crash recovery / visibility timeout**: a claim carries `claim_expires_at`
  (default TTL 300s). `release_expired_claims()` returns abandoned claims to
  `pending` so another worker can reclaim them after a crash. Completed
  (`processed`) and terminally `failed` events are never reclaimed.
- **Terminal failure**: after the attempt limit (default 5), the event is marked
  `failed` and the error is retained.
- **Duplicate events**: state-plus-event persistence uses
  `INSERT ... ON CONFLICT (event_id) DO NOTHING`, so a duplicate event ID can no
  longer abort an otherwise valid transaction.
- **Delivery guarantee**: durable, concurrency-safe claims with **at-least-once**
  delivery. The destination is an in-process `EventBus`, so exactly-once
  **external** delivery is **not** claimed; consumers must be idempotent and key
  on `event_id`.
- **Schema**: `core/persistence/sql/003_outbox_delivery_claim.sql` (additive and
  idempotent) adds `claimed_by`, `claimed_at`, `claim_expires_at`, and
  `next_attempt_at` to `event_outbox`, plus `idx_event_outbox_claim`.
- **Validation**: validated in CI ([run 39277794918](https://github.com/groot34/sentinel/actions/runs/39277794918),
  commit `aba70e1`): `offline` job 654 passed / 4 skipped; `postgres` job (PostgreSQL 16)
  743 passed / 0 skipped (`Total skipped: 0`), so all 6 live outbox tests executed.

---

## 12. Safety Invariants & Boundary Guarantees

1. **Zero Auto-Applied Patches**: Human approval records the human decision in `ApprovalRecord` **ONLY**. Sentinel never applies patches, runs shell commands to modify source, or executes unverified code.
2. **Ground Truth Isolation**: The Sentinel pipeline and API runtime **never** read `ground_truth.md` or baseline evaluation results. Only the post-run evaluation harness accesses ground truth.
3. **Secret Masking**: All exception handlers, logs, and telemetry sanitize API keys to prevent secret leakage.
4. **Deterministic Verification**: Hypotheses are falsified using pure Python AST analysis, log scanning, and invariant checking with zero LLM inference.
5. **Path-Confined Ingestion**: `POST /incidents` is disabled by default; when enabled it writes only regular UTF-8 text files beneath a freshly created staging directory inside the incident root, with containment verified before every write and no client-supplied path or symlink ever followed. Failed ingestions publish nothing. Absolute server paths, file contents, and secrets are never returned in error responses.

---

## 13. Documentation & Verification References

- **[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)** — Comprehensive architecture specification for Sentinel 1.0 (multi-agent verification engine) and Sentinel 2.0 (distributed state, OCC, outbox, leasing, ingestion, and SSE).
- **[docs/CURRENT_STATE_AUDIT.md](docs/CURRENT_STATE_AUDIT.md)** — Exhaustive repository state audit, current test counts, verified capabilities, and unresolved items.
- **[CHANGELOG.md](CHANGELOG.md)** — Full chronological release history from initial skeleton through Sentinel 2.0 Mission 19.
- **[REPRODUCE.md](REPRODUCE.md)** — Complete guide for setting up, running offline tests, launching the API server, querying endpoints, and executing benchmark evaluations.
- **[TODO.md](TODO.md)** — Roadmap milestone tracker documenting all completed missions and upcoming post-Mission 19 engineering work.
