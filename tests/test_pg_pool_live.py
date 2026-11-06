"""Mission 23 Task 2 — live PostgreSQL connection-pool verification.

Exercises the configurable pool built by ``PostgresRepository`` against a real
PostgreSQL server instead of a mock:

* sizing is applied and the configured maximum is never exceeded,
* connections are reused rather than recreated,
* acquisition under saturation respects the configured timeout and the pool
  recovers afterwards without leaking or wedging,
* concurrent repository operations stay within the maximum and return every
  connection, including on the client- and database-error paths,
* pool shutdown closes the underlying server sessions and is safe to repeat.

Determinism: contention is created with explicit connection holds and
``threading.Barrier`` synchronisation plus bounded acquisition timeouts; no test
depends on an unbounded sleep or on a bare ``time.sleep`` for correctness. The
module skips gracefully when PostgreSQL is unreachable, matching the
conventions in ``tests/test_postgres_persistence.py``.
"""

from __future__ import annotations

import os
import threading
import time
from typing import Any, Callable, List

import psycopg
import pytest
from psycopg.conninfo import make_conninfo
from psycopg_pool import ConnectionPool, PoolClosed, PoolTimeout

from core.domain.models import IncidentStatus
from core.domain.state import InvestigationState
from core.persistence.factory import get_repository
from core.persistence.postgres import (
    ENV_POOL_MAX_SIZE,
    ENV_POOL_MIN_SIZE,
    PostgresRepository,
)
from core.persistence.repository import PersistenceError

PG_URL = os.getenv(
    "SENTINEL_DATABASE_URL",
    "postgresql://sentinel:sentinel_dev_password@localhost:5432/sentinel_db",
)


def _is_postgres_available(url: str) -> bool:
    """Check if the live PostgreSQL database is reachable."""
    try:
        with psycopg.connect(url, connect_timeout=1) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1;")
                return cur.fetchone() == (1,)
    except Exception:
        return False


POSTGRES_AVAILABLE = _is_postgres_available(PG_URL)

pytestmark = pytest.mark.skipif(
    not POSTGRES_AVAILABLE, reason="Live PostgreSQL not available at " + PG_URL
)


# ===========================================================================
# Helpers
# ===========================================================================

def _tagged_conninfo(application_name: str) -> str:
    """Return the test URL tagged with ``application_name``.

    The tag is unique per test so ``pg_stat_activity`` can count exactly the
    backends a single pool owns, without being confused by other clients.
    """
    return make_conninfo(PG_URL, application_name=application_name)


def _count_backends(application_name: str) -> int:
    """Count live server sessions carrying the given ``application_name``."""
    with psycopg.connect(PG_URL, connect_timeout=5) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT count(*) FROM pg_stat_activity "
                "WHERE application_name = %s AND pid <> pg_backend_pid();",
                (application_name,),
            )
            return cur.fetchone()[0]


def _wait_until(
    predicate: Callable[[], bool], timeout: float = 5.0, interval: float = 0.05
) -> bool:
    """Poll *predicate* until it holds or *timeout* elapses (bounded wait)."""
    deadline = time.monotonic() + timeout
    while True:
        if predicate():
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(interval)


def _clean_records(investigation_id: str) -> None:
    """Remove test rows from ``investigations`` and ``event_outbox``."""
    try:
        with psycopg.connect(PG_URL, connect_timeout=5) as conn:
            with conn.transaction():
                with conn.cursor() as cur:
                    cur.execute(
                        "DELETE FROM investigations WHERE investigation_id = %s;",
                        (investigation_id,),
                    )
                    cur.execute(
                        "DELETE FROM event_outbox WHERE investigation_id = %s;",
                        (investigation_id,),
                    )
    except Exception:
        pass


@pytest.fixture(scope="module", autouse=True)
def _ensure_schema():
    """Apply migrations once for the module; skipped when PostgreSQL is absent."""
    if not POSTGRES_AVAILABLE:
        yield
        return
    with PostgresRepository(PG_URL) as repo:
        repo.initialize_schema()
        repo.initialize_outbox_schema()
    yield


# ===========================================================================
# Task 1 — live pool configuration
# ===========================================================================

class TestLivePoolConfiguration:
    """Sizing, reuse and maximum enforcement against a live server."""

    def test_configured_min_and_max_are_applied(self):
        repo = PostgresRepository(
            _tagged_conninfo("sentinel_pool_sizing_test"),
            min_size=1,
            max_size=2,
            timeout=5.0,
        )
        try:
            repo.probe()  # force at least one real connection
            stats = repo.pool.get_stats()
            assert repo.pool.min_size == 1
            assert repo.pool.max_size == 2
            assert stats["pool_min"] == 1
            assert stats["pool_max"] == 2
            assert stats["pool_size"] <= 2
            assert stats["pool_available"] >= 1
        finally:
            repo.close()

    def test_connections_are_reused_not_recreated(self):
        app_name = "sentinel_pool_reuse_test"
        repo = PostgresRepository(
            _tagged_conninfo(app_name),
            min_size=1,
            max_size=2,
            timeout=5.0,
        )
        pool = repo.pool
        try:
            # Wait for the pool to reach its steady (min_size) state before
            # asking for a connection, so a not-yet-ready pool cannot grow on
            # the first request and blur the reuse signal.
            pool.wait(timeout=5.0)
            created_baseline = pool.get_stats()["connections_num"]

            first = pool.getconn(timeout=5.0)
            first_pid = first.info.backend_pid
            pool.putconn(first)

            second = pool.getconn(timeout=5.0)
            second_pid = second.info.backend_pid
            pool.putconn(second)

            # The very same connection object (and server session) came back.
            assert second is first
            assert second_pid == first_pid
            # Reuse created no additional physical connection ...
            assert pool.get_stats()["connections_num"] == created_baseline
            # ... and exactly one server session is live for this pool.
            assert _wait_until(lambda: _count_backends(app_name) == 1)
        finally:
            repo.close()

    def test_pool_never_exceeds_configured_maximum(self):
        repo = PostgresRepository(
            _tagged_conninfo("sentinel_pool_max_test"),
            min_size=1,
            max_size=2,
            timeout=5.0,
        )
        pool = repo.pool
        held: List[Any] = []
        try:
            held.append(pool.getconn(timeout=5.0))
            held.append(pool.getconn(timeout=5.0))
            stats = pool.get_stats()
            assert stats["pool_size"] == 2
            assert stats["pool_available"] == 0

            # A third acquisition cannot create a third connection; it must
            # fail within the small per-call timeout rather than grow the pool.
            with pytest.raises(PoolTimeout):
                pool.getconn(timeout=0.5)

            stats = pool.get_stats()
            assert stats["pool_size"] == 2, "pool grew beyond its configured maximum"
            assert stats["requests_errors"] >= 1
        finally:
            for conn in held:
                pool.putconn(conn)
            repo.close()


# ===========================================================================
# Task 2 — connection exhaustion and recovery
# ===========================================================================

class TestLivePoolSaturationAndRecovery:
    """Exhaustion respects the configured timeout; the pool recovers cleanly."""

    def test_acquisition_timeout_is_enforced_then_pool_recovers(self):
        configured_timeout = 0.6
        repo = PostgresRepository(
            _tagged_conninfo("sentinel_pool_timeout_test"),
            min_size=1,
            max_size=2,
            timeout=configured_timeout,
        )
        pool = repo.pool
        held: List[Any] = []
        try:
            held.append(pool.getconn(timeout=5.0))
            held.append(pool.getconn(timeout=5.0))
            assert pool.get_stats()["pool_available"] == 0

            # Default acquisition (timeout=None) must use the configured timeout.
            started = time.monotonic()
            with pytest.raises(PoolTimeout):
                pool.getconn()
            elapsed = time.monotonic() - started
            assert elapsed >= configured_timeout * 0.9, "returned before the configured timeout"
            assert elapsed < 5.0, "waited far longer than the configured timeout"
        finally:
            for conn in held:
                pool.putconn(conn)

        # Releasing the holds restores capacity: the pool is not wedged and
        # leaked no connections.
        assert pool.get_stats()["pool_available"] == 2
        recovered = pool.getconn(timeout=5.0)
        try:
            with recovered.cursor() as cur:
                cur.execute("SELECT 1;")
                assert cur.fetchone() == (1,)
        finally:
            pool.putconn(recovered)
            repo.close()

    def test_waiter_is_served_when_a_connection_is_released(self):
        repo = PostgresRepository(
            _tagged_conninfo("sentinel_pool_waiter_test"),
            min_size=1,
            max_size=2,
            timeout=10.0,
        )
        pool = repo.pool
        held: List[Any] = []
        acquired: List[Any] = []
        try:
            held.append(pool.getconn(timeout=5.0))
            held.append(pool.getconn(timeout=5.0))

            def _waiter() -> None:
                try:
                    acquired.append(pool.getconn(timeout=10.0))
                except BaseException as exc:  # noqa: BLE001 - surfaced by assertion
                    acquired.append(exc)

            waiter = threading.Thread(target=_waiter)
            waiter.start()

            # Deterministic readiness: wait until the pool reports exactly one
            # queued request instead of sleeping for a fixed duration.
            assert _wait_until(lambda: pool.get_stats()["requests_waiting"] >= 1)
            assert acquired == [], "waiter was served while the pool was saturated"

            pool.putconn(held.pop())  # hand one slot to the queued waiter
            waiter.join(timeout=5.0)
            assert not waiter.is_alive(), "waiter did not receive the released connection"

            assert len(acquired) == 1
            assert not isinstance(acquired[0], BaseException), acquired[0]
        finally:
            for conn in held:
                pool.putconn(conn)
            for conn in acquired:
                if not isinstance(conn, BaseException):
                    pool.putconn(conn)
            repo.close()


# ===========================================================================
# Task 3 — concurrent usage
# ===========================================================================

class TestLivePoolConcurrentUsage:
    """Concurrent repository operations through one deliberately small pool."""

    def test_concurrent_repository_operations_respect_max_and_return_connections(self):
        pool_max = 3
        repo = PostgresRepository(
            _tagged_conninfo("sentinel_pool_concurrency_test"),
            min_size=1,
            max_size=pool_max,
            timeout=20.0,
        )
        inv_id = "pool_live_concurrent"
        _clean_records(inv_id)
        try:
            repo.save(
                InvestigationState(incident_id=inv_id, status=IncidentStatus.RUNNING)
            )

            worker_count = 6
            barrier = threading.Barrier(worker_count)
            errors: List[BaseException] = []
            observed_pool_sizes: List[int] = []
            lock = threading.Lock()

            def _worker() -> None:
                try:
                    barrier.wait(timeout=10.0)  # start all workers together
                    for _ in range(4):
                        assert repo.exists(inv_id) is True
                        loaded = repo.load(inv_id)
                        assert loaded is not None and loaded.version == 1
                        repo.probe()
                        with lock:
                            observed_pool_sizes.append(
                                repo.pool.get_stats()["pool_size"]
                            )
                except BaseException as exc:  # noqa: BLE001 - re-surfaced below
                    with lock:
                        errors.append(exc)

            threads = [
                threading.Thread(target=_worker) for _ in range(worker_count)
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=30.0)

            assert all(
                not thread.is_alive() for thread in threads
            ), "concurrent workers exceeded the bounded test timeout"
            assert errors == [], f"concurrent repository operations failed: {errors!r}"

            # The maximum was respected under genuine contention ...
            assert observed_pool_sizes
            assert max(observed_pool_sizes) <= pool_max
            # ... and every connection was returned to the pool afterwards.
            stats = repo.pool.get_stats()
            assert stats["pool_size"] <= pool_max
            assert stats["pool_available"] == stats["pool_size"]
            assert stats["requests_waiting"] == 0
        finally:
            _clean_records(inv_id)
            repo.close()

    def test_connections_are_returned_after_client_and_database_errors(self):
        repo = PostgresRepository(
            _tagged_conninfo("sentinel_pool_error_return_test"),
            min_size=1,
            max_size=2,
            timeout=5.0,
        )
        pool = repo.pool
        try:
            repo.probe()

            # (a) A client-side exception raised while holding a pooled connection.
            with pytest.raises(RuntimeError, match="client boom"):
                with pool.connection(timeout=5.0):
                    raise RuntimeError("client boom")

            # (b) A genuine database error raised while holding a pooled connection.
            with pytest.raises(psycopg.Error):
                with pool.connection(timeout=5.0) as conn:
                    with conn.cursor() as cur:
                        cur.execute("SELECT * FROM sentinel_pool_missing_table_xyz;")

            # Both connections went back to the pool: nothing stays checked out.
            stats = pool.get_stats()
            assert stats["pool_available"] == stats["pool_size"]
            assert stats["requests_waiting"] == 0

            # The pool is still healthy and usable through the repository.
            repo.probe()
            assert repo.exists("pool_live_nonexistent_row") is False
        finally:
            repo.close()


# ===========================================================================
# Task 4 — shutdown and cleanup
# ===========================================================================

class TestLivePoolShutdownAndCleanup:
    """Owned-pool shutdown releases the underlying server sessions."""

    def test_owned_pool_shutdown_closes_server_connections(self):
        app_name = "sentinel_pool_shutdown_test"
        repo = PostgresRepository(
            _tagged_conninfo(app_name), min_size=1, max_size=2, timeout=5.0
        )
        pool = repo.pool
        try:
            repo.probe()
            assert _wait_until(
                lambda: _count_backends(app_name) >= 1
            ), "expected a live pooled backend"
            assert pool.closed is False
        finally:
            repo.close()

        assert pool.closed is True
        assert _wait_until(
            lambda: _count_backends(app_name) == 0
        ), "pool left server sessions open after shutdown"
        with pytest.raises(PoolClosed):
            pool.getconn(timeout=1.0)

    def test_repeated_shutdown_is_safe(self):
        app_name = "sentinel_pool_repeat_shutdown_test"
        repo = PostgresRepository(
            _tagged_conninfo(app_name), min_size=1, max_size=1, timeout=5.0
        )
        try:
            repo.probe()
        finally:
            repo.close()

        assert repo.pool.closed is True
        # A second close is a no-op (mirrors the app's single-shot shutdown) and
        # must not raise.
        repo.close()
        assert repo.pool.closed is True
        assert _wait_until(lambda: _count_backends(app_name) == 0)

    def test_injected_pool_is_not_closed_by_repository(self):
        """A repository only closes pools it created (matches app lifecycle)."""
        pool = ConnectionPool(
            conninfo=_tagged_conninfo("sentinel_pool_injected_test"),
            min_size=1,
            max_size=2,
            timeout=5.0,
            open=True,
        )
        try:
            repo = PostgresRepository(pool)
            repo.probe()
            repo.close()
            assert pool.closed is False
            # Still usable by its owner after the repository released it.
            with pool.connection(timeout=5.0) as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT 1;")
                    assert cur.fetchone() == (1,)
        finally:
            pool.close()


# ===========================================================================
# Invalid configuration fails safely
# ===========================================================================

class TestLivePoolConfigurationValidation:
    """Invalid configuration fails safely before any connection is attempted."""

    def test_malformed_pool_setting_is_rejected_before_connecting(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        # Point at an unreachable endpoint: if the failure were a connection
        # error (not config validation) the test would see a different error.
        monkeypatch.setenv(
            "SENTINEL_DATABASE_URL",
            "postgresql://sentinel:secret@127.0.0.1:1/sentinel_db",
        )
        monkeypatch.setenv(ENV_POOL_MIN_SIZE, "not-an-integer")

        with pytest.raises(PersistenceError) as excinfo:
            get_repository(backend="postgres")

        message = str(excinfo.value)
        assert ENV_POOL_MIN_SIZE in message
        assert "postgresql://" not in message  # never echoes the DSN/credential

    def test_inconsistent_bounds_are_rejected_against_the_live_url(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.setenv(ENV_POOL_MIN_SIZE, "5")
        monkeypatch.setenv(ENV_POOL_MAX_SIZE, "2")

        with pytest.raises(PersistenceError, match="must not exceed max_size"):
            get_repository(backend="postgres", database_url=PG_URL)
