# Ground Truth: Incident 06 (Connection Pool Exhaustion from Missing Close)

## Root Cause
Commit `6c7d8e` added date validation in `ReportGenerator.generate_report()`. If `date_filter` is invalid, a `ValueError` is raised after `conn = pool.acquire()` but before `conn.release()`. Because there is no `try...finally` block or context manager, every invalid user request permanently leaks a database connection. After 10 invalid requests, the entire pool is exhausted, locking out all valid traffic.

## Causal Chain
1. Code Change: Validation check raises exception between acquire() and release().
2. Invalid Requests: Malformed requests trigger unhandled `ValueError`.
3. Connection Leak: Pool active count steadily increases to 10/10.
4. Total Outage: Subsequent valid report requests fail with `TimeoutError: QueuePool limit reached`.

## Distractor
Postgres autovacuum process logged startup in database maintenance logs.

## Minimal Fix
Use a `try...finally: conn.release()` block or wrap connection acquisition in a context manager (`with pool.acquire() as conn:`).

## Detection Test
`tests/test_service.py::test_report_generator_exception_leak` proves that connection active count does not reset when exception occurs.
