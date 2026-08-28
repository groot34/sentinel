# Ground Truth: Incident 10 (Complex Multi-Symptom Cascade - HARD CASE)

## Underlying Root Cause
Commit `f0e1d2` applied migration `012_drop_legacy_ledger_index.sql`, which mistakenly dropped the composite index `idx_ledger_account_entry_date ON ledger_entries (account_id, entry_date DESC)`.

## Complete Causal Chain
1. Root Cause: Composite database index was dropped in migration.
2. Immediate Behavior: Ledger balance queries degraded from 3.2ms to 1850ms due to full table scans across 5 million rows.
3. Amplification: Client applications timed out after 1000ms and initiated unbacked retries, surging query volume from 10 req/s to 120 req/s.
4. Resource Saturation: Long-running sequential scans combined with retry surge quickly consumed all 15 connection pool slots (`active_conns = 15/15`).
5. Healthcheck Failure: The `/api/v1/health` endpoint required a database connection to pass readiness checks and failed with 500 status codes.
6. Pod Restart Cascade: Kubernetes killed the pods due to failed readiness probes, resulting in a total service outage (502 Bad Gateway).

## Distractor & Why Naive Models Fail
- **Distractor 1 (Kubernetes Pod Restarts)**: A naive model will diagnose this as a "Kubernetes Pod OOM or unhealthy node disk pressure issue".
- **Distractor 2 (Connection Pool Exhaustion)**: A simple model will diagnose this as "Connection pool size too small (15 is insufficient)".
- **Distractor 3 (Aggressive Client Retries)**: A simple model will blame the "Client retry policy".
*True Root Cause*: All subsequent symptoms are downstream effects of the **dropped composite index**.

## Minimal Fix
Recreate the composite index on `ledger_entries(account_id, entry_date DESC)`:
`CREATE INDEX CONCURRENTLY idx_ledger_account_entry_date ON ledger_entries (account_id, entry_date DESC);`

## Detection Test
`tests/test_service.py::test_ledger_balance_check` validates basic balance computation.
