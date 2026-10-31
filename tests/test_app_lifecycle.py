"""Mission 22 Task 4: application lifespan and graceful shutdown.

Covers the resources the API releases when it shuts down: the persistence
repository (a PostgreSQL connection pool is simulated with a controlled fake so
no live database is required), and the readiness probe executor. Also covers
cleanup after a startup or runtime failure, single-shot shutdown semantics, and
that filesystem persistence keeps working without a pool.
"""

from __future__ import annotations

import asyncio
from typing import Any, List, Optional

import pytest
from starlette.testclient import TestClient

import api.routes as routes
from api.app import _shutdown_application, create_app
from core.persistence import PersistenceRepository
from core.persistence.filesystem import FilesystemRepository
from core.persistence.postgres import PostgresRepository


class ClosingRepository(PersistenceRepository):
    """Controlled fake that records how often it was released."""

    def __init__(self, probe_error: Optional[BaseException] = None) -> None:
        self.close_calls = 0
        self._probe_error = probe_error

    def save(self, state: Any, events: Optional[List[Any]] = None) -> None:
        raise AssertionError("save() must never be called by a lifecycle test")

    def load(self, investigation_id: str) -> Optional[Any]:
        return None

    def exists(self, investigation_id: str) -> bool:
        return False

    def delete(self, investigation_id: str) -> None:
        pass

    def list(self) -> List[str]:
        return []

    def probe(self) -> None:
        if self._probe_error is not None:
            raise self._probe_error

    def close(self) -> None:
        self.close_calls += 1


class FakeConnectionPool:
    """Minimal stand-in for psycopg_pool.ConnectionPool; contacts no server."""

    def __init__(self, **kwargs: Any) -> None:
        self.closed = False
        self.close_calls = 0

    def close(self, timeout: Optional[float] = None) -> None:
        self.close_calls += 1
        self.closed = True


# ===========================================================================
# 1. PostgreSQL pool cleanup on shutdown (simulated pool, no live database)
# ===========================================================================

def test_postgres_pool_is_closed_on_application_shutdown(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr("core.persistence.postgres.ConnectionPool", FakeConnectionPool)
    repository = PostgresRepository(
        "postgresql://sentinel:secret@localhost:5432/sentinel_db"
    )
    assert repository.pool.closed is False

    app = create_app(repository=repository)
    with TestClient(app) as client:
        assert client.get("/health").status_code == 200
        # Still open while the application is running.
        assert repository.pool.closed is False

    assert repository.pool.closed is True
    assert repository.pool.close_calls == 1


def test_postgres_pool_is_not_closed_when_repository_does_not_own_it(
    monkeypatch: pytest.MonkeyPatch,
):
    """An injected pool belongs to its creator, so shutdown must leave it open."""
    monkeypatch.setattr("core.persistence.postgres.ConnectionPool", FakeConnectionPool)
    pool = FakeConnectionPool()
    repository = PostgresRepository(pool)

    app = create_app(repository=repository)
    with TestClient(app) as client:
        assert client.get("/health").status_code == 200

    assert pool.closed is False


# ===========================================================================
# 2. Filesystem shutdown without assuming a pool exists
# ===========================================================================

def test_filesystem_repository_shutdown_needs_no_pool(
    tmp_path, monkeypatch: pytest.MonkeyPatch
):
    # The base contract supplies a no-op close, so a backend with no pool is
    # never asked to close something it does not have.
    FilesystemRepository(tmp_path / "scratch").close()

    repository = FilesystemRepository(tmp_path / "state")
    closed: List[int] = []
    monkeypatch.setattr(repository, "close", lambda: closed.append(1))

    app = create_app(repository=repository)
    with TestClient(app) as client:
        assert client.get("/health").status_code == 200
        assert closed == []

    assert closed == [1]


# ===========================================================================
# 3. Readiness executor is shut down
# ===========================================================================

def test_readiness_executor_is_shut_down_on_application_shutdown():
    app = create_app(repository=ClosingRepository())

    with TestClient(app) as client:
        assert client.get("/ready").status_code == 200
        runtime = app.state.readiness_runtime
        # The application owns its own executor rather than the process-wide
        # fallback, so shutdown disposes of only what it created.
        assert runtime.executor is not routes._READY_EXECUTOR
        assert runtime.executor.submit(lambda: "ok").result() == "ok"

    with pytest.raises(RuntimeError):
        runtime.executor.submit(lambda: "not accepted")

    # The fallback executor survives: another application in this process is
    # unaffected by this application's shutdown.
    assert routes._READY_EXECUTOR.submit(lambda: "alive").result() == "alive"


def test_readiness_still_served_while_running_and_uses_app_runtime():
    repository = ClosingRepository()
    app = create_app(repository=repository)

    with TestClient(app) as client:
        assert app.state.readiness_runtime is not None
        for _ in range(routes._READY_MAX_CONCURRENCY + 1):
            assert client.get("/ready").status_code == 200


# ===========================================================================
# 4. Cleanup after startup or runtime failure
# ===========================================================================

def test_cleanup_runs_after_a_runtime_probe_failure():
    repository = ClosingRepository(probe_error=RuntimeError("probe boom"))
    app = create_app(repository=repository)

    with TestClient(app) as client:
        assert client.get("/ready").status_code == 503

    assert repository.close_calls == 1


def test_cleanup_runs_when_startup_fails(monkeypatch: pytest.MonkeyPatch):
    repository = ClosingRepository()
    app = create_app(repository=repository)

    def _explode() -> None:
        raise RuntimeError("startup boom")

    # Fail the lifespan after the app exists but before it starts serving.
    monkeypatch.setattr("api.app.create_readiness_runtime", _explode)

    async def _enter_lifespan() -> None:
        async with app.router.lifespan_context(app):
            pass

    with pytest.raises(RuntimeError, match="startup boom"):
        asyncio.run(_enter_lifespan())

    assert repository.close_calls == 1


def test_repository_close_failure_does_not_abort_shutdown():
    class FailingCloseRepository(ClosingRepository):
        def close(self) -> None:
            raise RuntimeError("close boom")

    app = create_app(repository=FailingCloseRepository())

    with TestClient(app) as client:
        assert client.get("/ready").status_code == 200

    # Shutdown completed without raising, and the executor was still disposed.
    with pytest.raises(RuntimeError):
        app.state.readiness_runtime.executor.submit(lambda: "not accepted")


# ===========================================================================
# 5. Repeated shutdown does not double-close resources
# ===========================================================================

def test_repeated_shutdown_does_not_double_close():
    repository = ClosingRepository()
    app = create_app(repository=repository)

    with TestClient(app):
        pass

    assert repository.close_calls == 1

    # A duplicate shutdown (for example a second signal handler) is a no-op.
    asyncio.run(_shutdown_application(app))

    assert repository.close_calls == 1


def test_shutdown_without_startup_is_safe():
    """Shutdown must not fail when the lifecycle never ran (no runtime)."""
    repository = ClosingRepository()
    app = create_app(repository=repository)

    asyncio.run(_shutdown_application(app))

    assert repository.close_calls == 1
