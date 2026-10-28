"""Tests for Mission 21: operational readiness endpoint (GET /ready).

Covers the fixed readiness contract (200 ready / 503 not ready), the read-only
persistence probe (default and backend override), no information leakage on
failure, the HTTP response deadline, the fixed admission cap that prevents
unbounded abandoned probe work, recovery once a blocked probe finally
completes, liveness (/health) compatibility, public access to /ready under read
protection, the exact allowlisted response keys, and timeout configuration
parsing.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

import pytest
from starlette.testclient import TestClient

import api.routes as routes
from api.app import create_app
from api.routes import (
    DEFAULT_READINESS_TIMEOUT_SECONDS,
    ENV_READINESS_TIMEOUT_SECONDS,
    MAX_READINESS_TIMEOUT_SECONDS,
    _READY_MAX_CONCURRENCY,
    _parse_readiness_timeout,
    _readiness_timeout_seconds,
    readiness,
)
from core.persistence import PersistenceError, PersistenceRepository

SECRET_KEY = "SUPER_SECRET_KEY_123"
READY_BODY = {"status": "ready", "checks": {"persistence": "ok"}}
NOT_READY_BODY = {"status": "not_ready", "checks": {"persistence": "unavailable"}}


# ---------------------------------------------------------------------------
# Fixtures, repositories and helpers
# ---------------------------------------------------------------------------

class MemoryRepository(PersistenceRepository):
    """Repository implementing only the abstract methods.

    ``probe`` is deliberately *not* overridden so the inherited default
    (``self.list()``) is exercised by the readiness path.
    """

    def __init__(self, states: Optional[Dict[str, Any]] = None) -> None:
        self._states: Dict[str, Any] = dict(states or {})

    def save(self, state: Any, events: Optional[List[Any]] = None) -> None:
        self._states[state.incident_id] = state

    def load(self, investigation_id: str) -> Optional[Any]:
        return self._states.get(investigation_id)

    def exists(self, investigation_id: str) -> bool:
        return investigation_id in self._states

    def delete(self, investigation_id: str) -> None:
        self._states.pop(investigation_id, None)

    def list(self) -> List[str]:
        return sorted(self._states.keys())


class RaisingRepository(MemoryRepository):
    """Repository whose probe always raises, carrying sensitive-looking detail."""

    def __init__(self, exc: BaseException) -> None:
        super().__init__()
        self._exc = exc

    def probe(self) -> None:
        raise self._exc


class BlockingRepository(MemoryRepository):
    """Repository whose probe blocks until explicitly released."""

    def __init__(self) -> None:
        super().__init__()
        self.signal_started = threading.Semaphore(0)
        self.signal_finished = threading.Semaphore(0)
        self._release = threading.Event()
        self.blocked_calls = 0

    def probe(self) -> None:
        self.blocked_calls += 1
        self.signal_started.release()
        self._release.wait(timeout=10)
        self.signal_finished.release()

    def release(self) -> None:
        self._release.set()


def _request(repository: Optional[PersistenceRepository]) -> SimpleNamespace:
    """Build the minimal request stub consumed by :func:`readiness`."""
    state = SimpleNamespace(repository=repository)
    return SimpleNamespace(app=SimpleNamespace(state=state))


def _body(response: Any) -> Dict[str, Any]:
    return json.loads(response.body)


def _drain_readiness_capacity(timeout: float = 5.0) -> None:
    """Best-effort wait until every readiness admission slot is free again."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        held = 0
        while held < _READY_MAX_CONCURRENCY and routes._READY_SLOTS.acquire(blocking=False):
            held += 1
        for _ in range(held):
            routes._READY_SLOTS.release()
        if held == _READY_MAX_CONCURRENCY:
            return
        time.sleep(0.01)


@pytest.fixture(autouse=True)
def _clean_readiness_env(monkeypatch: pytest.MonkeyPatch):
    """Start each test with no inherited readiness config and full capacity."""
    monkeypatch.delenv(ENV_READINESS_TIMEOUT_SECONDS, raising=False)
    yield
    _drain_readiness_capacity()


def _client(repository: Optional[PersistenceRepository] = None, **kwargs: Any) -> TestClient:
    return TestClient(create_app(repository=repository or MemoryRepository(), **kwargs))


# ===========================================================================
# 1. Successful readiness
# ===========================================================================

def test_ready_returns_200_when_persistence_is_healthy():
    client = _client()

    response = client.get("/ready")

    assert response.status_code == 200
    assert response.json() == READY_BODY


def test_ready_invokes_repository_probe_read_only():
    """The default probe lists investigations and mutates nothing."""
    repo = MemoryRepository({"inc_a": object(), "inc_b": object()})

    response = readiness(_request(repo))

    assert response.status_code == 200
    # Listing is read-only: the same states remain.
    assert repo.list() == ["inc_a", "inc_b"]


# ===========================================================================
# 2. Repository absent / probe failure - always a fixed, sanitised 503
# ===========================================================================

def test_ready_returns_503_when_repository_is_absent():
    app = create_app(repository=MemoryRepository())
    app.state.repository = None
    client = TestClient(app)

    response = client.get("/ready")

    assert response.status_code == 503
    assert response.json() == NOT_READY_BODY


def test_ready_returns_503_when_probe_raises_persistence_error():
    repo = RaisingRepository(PersistenceError("boom"))

    response = readiness(_request(repo))

    assert response.status_code == 503
    assert _body(response) == NOT_READY_BODY


def test_ready_failure_does_not_leak_backend_details():
    secret = (
        "postgresql://sentinel:supersecret@10.0.0.5:5432/sentinel_db "
        "C:\\srv\\sentinel\\secret\\path /var/lib/private/state"
    )
    repo = RaisingRepository(PersistenceError(secret))

    response = readiness(_request(repo))

    assert response.status_code == 503
    raw = response.body.decode("utf-8")
    for leaked in ("supersecret", "10.0.0.5", "sentinel_db", "secret", "postgresql", "var/lib"):
        assert leaked not in raw
    assert _body(response) == NOT_READY_BODY


def test_ready_returns_503_on_unexpected_probe_exception():
    repo = RaisingRepository(RuntimeError("internal detail that must not surface"))

    response = readiness(_request(repo))

    assert response.status_code == 503
    assert _body(response) == NOT_READY_BODY
    assert "internal detail" not in response.body.decode("utf-8")


# ===========================================================================
# 3. Exact allowlisted response shape
# ===========================================================================

def test_ready_response_keys_are_exactly_allowlisted():
    ready_body = _body(readiness(_request(MemoryRepository())))
    assert set(ready_body.keys()) == {"status", "checks"}
    assert set(ready_body["checks"].keys()) == {"persistence"}

    failing_body = _body(readiness(_request(RaisingRepository(PersistenceError("x")))))
    assert set(failing_body.keys()) == {"status", "checks"}
    assert set(failing_body["checks"].keys()) == {"persistence"}


# ===========================================================================
# 4. HTTP response deadline
# ===========================================================================

def test_ready_returns_503_when_probe_exceeds_deadline(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv(ENV_READINESS_TIMEOUT_SECONDS, "0.05")
    repo = BlockingRepository()

    started = time.monotonic()
    response = readiness(_request(repo))
    elapsed = time.monotonic() - started

    assert response.status_code == 503
    assert _body(response) == NOT_READY_BODY
    assert elapsed < 1.0  # returned near the configured deadline, not blocked indefinitely

    # The underlying probe is still running; release it so capacity is restored.
    repo.release()
    assert repo.signal_finished.acquire(timeout=5)


def test_deadline_expiry_is_logged_as_a_deadline(monkeypatch: pytest.MonkeyPatch, caplog):
    """A genuine deadline expiry is distinguished in the log from a probe error."""
    monkeypatch.setenv(ENV_READINESS_TIMEOUT_SECONDS, "0.05")
    repo = BlockingRepository()

    with caplog.at_level(logging.WARNING, logger="api.routes"):
        response = readiness(_request(repo))

    assert response.status_code == 503
    assert "exceeded the response deadline" in caplog.text

    repo.release()
    assert repo.signal_finished.acquire(timeout=5)


def test_probe_raised_timeout_error_is_not_reported_as_a_deadline(caplog):
    """``concurrent.futures.TimeoutError`` is the builtin ``TimeoutError``.

    A probe that raises ``TimeoutError`` on its own must still return the fixed
    503, but must not be misreported as the HTTP response deadline expiring.
    """
    repo = RaisingRepository(TimeoutError("probe-level timeout"))

    with caplog.at_level(logging.WARNING, logger="api.routes"):
        response = readiness(_request(repo))

    assert response.status_code == 503
    assert _body(response) == NOT_READY_BODY
    assert "exceeded the response deadline" not in caplog.text
    assert "Readiness probe failed (TimeoutError)" in caplog.text


# ===========================================================================
# 5. Admission cap: bounded abandoned work + fail-fast rejection
# ===========================================================================

def test_excess_concurrent_probes_fail_fast(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv(ENV_READINESS_TIMEOUT_SECONDS, "2.0")
    repo = BlockingRepository()
    held: List[Any] = []

    def _invoke() -> None:
        held.append(readiness(_request(repo)))

    workers = [threading.Thread(target=_invoke) for _ in range(_READY_MAX_CONCURRENCY)]
    for worker in workers:
        worker.start()

    # Wait until every worker is blocked inside the probe.
    for _ in range(_READY_MAX_CONCURRENCY):
        assert repo.signal_started.acquire(timeout=5)

    # Capacity is now saturated: a further request must fail fast, not queue.
    started = time.monotonic()
    rejected = readiness(_request(repo))
    elapsed = time.monotonic() - started

    assert rejected.status_code == 503
    assert _body(rejected) == NOT_READY_BODY
    assert elapsed < 0.5
    # The rejected request never reached the repository.
    assert repo.blocked_calls == _READY_MAX_CONCURRENCY

    repo.release()
    for worker in workers:
        worker.join(timeout=5)

    assert [response.status_code for response in held] == [200] * _READY_MAX_CONCURRENCY


def test_timed_out_probe_retains_slot_and_recovers_after_completion(monkeypatch: pytest.MonkeyPatch):
    """A probe that outlives the HTTP deadline keeps its slot until it finishes."""
    monkeypatch.setenv(ENV_READINESS_TIMEOUT_SECONDS, "0.05")
    repo = BlockingRepository()
    timed_out: List[Any] = []

    def _invoke() -> None:
        timed_out.append(readiness(_request(repo)))

    workers = [threading.Thread(target=_invoke) for _ in range(_READY_MAX_CONCURRENCY)]
    for worker in workers:
        worker.start()
    for _ in range(_READY_MAX_CONCURRENCY):
        assert repo.signal_started.acquire(timeout=5)

    for worker in workers:
        worker.join(timeout=5)
    assert [response.status_code for response in timed_out] == [503] * _READY_MAX_CONCURRENCY

    # Despite both HTTP responses already being sent, the abandoned probes still
    # hold every slot, so fresh requests continue to be rejected fast.
    assert readiness(_request(repo)).status_code == 503

    # Completing the blocked work - not the HTTP response - frees capacity.
    repo.release()
    for _ in range(_READY_MAX_CONCURRENCY):
        assert repo.signal_finished.acquire(timeout=5)
    _drain_readiness_capacity()

    assert readiness(_request(repo)).status_code == 200


def test_admission_capacity_is_derived_from_executor_worker_count():
    """The admission cap tracks the executor's real worker count.

    Deriving both from one source keeps them from drifting apart, so an admitted
    probe is always guaranteed a worker and can never queue unboundedly.
    """
    _drain_readiness_capacity()
    assert _READY_MAX_CONCURRENCY == routes._READY_EXECUTOR._max_workers

    acquired = 0
    while routes._READY_SLOTS.acquire(blocking=False):
        acquired += 1
        if acquired > _READY_MAX_CONCURRENCY:  # guard against a runaway loop
            break
    for _ in range(acquired):
        routes._READY_SLOTS.release()

    assert acquired == _READY_MAX_CONCURRENCY


# ===========================================================================
# 6. Liveness (/health) unchanged
# ===========================================================================

def test_health_behaviour_is_unchanged():
    client = _client()

    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "service": "sentinel", "version": "2.0"}


# ===========================================================================
# 7. Authentication policy
# ===========================================================================

def test_ready_is_public_under_read_protection_while_other_routes_stay_protected():
    client = _client(auth_enabled=True, api_key=SECRET_KEY, auth_protect_reads=True)

    # Public probes remain reachable without credentials.
    assert client.get("/ready").status_code == 200
    assert client.get("/health").status_code == 200

    # Other reads and all mutations keep the existing default-deny behaviour.
    assert client.get("/investigations").status_code == 401
    assert client.post("/investigations", json={"incident_id": "probe"}).status_code == 401


def test_ready_public_endpoint_is_unchanged_when_auth_disabled():
    client = _client()

    assert client.get("/ready").status_code == 200


# ===========================================================================
# 8. Timeout configuration parsing
# ===========================================================================

@pytest.mark.parametrize(
    "raw,expected",
    [
        (None, DEFAULT_READINESS_TIMEOUT_SECONDS),
        ("", DEFAULT_READINESS_TIMEOUT_SECONDS),
        ("   ", DEFAULT_READINESS_TIMEOUT_SECONDS),
        ("abc", DEFAULT_READINESS_TIMEOUT_SECONDS),
        ("0", DEFAULT_READINESS_TIMEOUT_SECONDS),
        ("-1", DEFAULT_READINESS_TIMEOUT_SECONDS),
        ("nan", DEFAULT_READINESS_TIMEOUT_SECONDS),
        ("inf", DEFAULT_READINESS_TIMEOUT_SECONDS),
        ("-inf", DEFAULT_READINESS_TIMEOUT_SECONDS),
        ("0.05", 0.05),
        ("2", 2.0),
        (" 3.5 ", 3.5),
        ("1e-3", 0.001),
        ("30", MAX_READINESS_TIMEOUT_SECONDS),
        ("31", MAX_READINESS_TIMEOUT_SECONDS),
        ("3600", MAX_READINESS_TIMEOUT_SECONDS),
        ("1e9", MAX_READINESS_TIMEOUT_SECONDS),
    ],
)
def test_readiness_timeout_parsing(raw: Optional[str], expected: float):
    assert _parse_readiness_timeout(raw) == pytest.approx(expected)


def test_readiness_timeout_is_clamped_to_a_reasonable_maximum():
    """Positive values above the ceiling are clamped; invalid ones still default."""
    assert MAX_READINESS_TIMEOUT_SECONDS > DEFAULT_READINESS_TIMEOUT_SECONDS
    clamped = MAX_READINESS_TIMEOUT_SECONDS + 1.0
    assert _parse_readiness_timeout(str(clamped)) == pytest.approx(MAX_READINESS_TIMEOUT_SECONDS)
    assert _parse_readiness_timeout("1e6") == pytest.approx(MAX_READINESS_TIMEOUT_SECONDS)
    # The ceiling itself is honoured exactly, and non-finite values still default.
    assert _parse_readiness_timeout("30") == pytest.approx(MAX_READINESS_TIMEOUT_SECONDS)
    assert _parse_readiness_timeout("inf") == DEFAULT_READINESS_TIMEOUT_SECONDS


def test_readiness_timeout_reads_environment(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv(ENV_READINESS_TIMEOUT_SECONDS, raising=False)
    assert _readiness_timeout_seconds() == DEFAULT_READINESS_TIMEOUT_SECONDS

    monkeypatch.setenv(ENV_READINESS_TIMEOUT_SECONDS, "0.75")
    assert _readiness_timeout_seconds() == pytest.approx(0.75)

    monkeypatch.setenv(ENV_READINESS_TIMEOUT_SECONDS, "not-a-number")
    assert _readiness_timeout_seconds() == DEFAULT_READINESS_TIMEOUT_SECONDS
