"""Focused tests for environment-driven PostgreSQL pool configuration (Mission 22, Task 7).

Covers the pool sizing / acquisition-timeout settings that were previously
hardcoded in ``PostgresRepository`` and are now resolved, validated and shared
by the API and the event worker through ``core.persistence.factory.get_repository``.

Also verifies that the neighbouring ``SENTINEL_PG_RETRY_*`` and incident-ingestion
settings are still loaded correctly from the environment.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List

import pytest

from agents.bundle_ingest import (
    IngestionLimits,
    ingestion_enabled,
    load_ingestion_limits,
)
from core.persistence.factory import get_repository
from core.persistence.postgres import (
    DEFAULT_POOL_MAX_SIZE,
    DEFAULT_POOL_MIN_SIZE,
    DEFAULT_POOL_TIMEOUT_SECONDS,
    ENV_POOL_MAX_SIZE,
    ENV_POOL_MIN_SIZE,
    ENV_POOL_TIMEOUT_SECONDS,
    PoolConfig,
    PostgresRepository,
    pool_config_from_env,
)
from core.persistence.repository import PersistenceError
from core.persistence.retry import policy_from_env

POOL_ENV_VARS = (ENV_POOL_MIN_SIZE, ENV_POOL_MAX_SIZE, ENV_POOL_TIMEOUT_SECONDS)
RETRY_ENV_VARS = (
    "SENTINEL_PG_RETRY_MAX_ATTEMPTS",
    "SENTINEL_PG_RETRY_BASE_DELAY",
    "SENTINEL_PG_RETRY_MAX_DELAY",
)
INGEST_ENV_VARS = (
    "SENTINEL_ALLOW_INCIDENT_INGESTION",
    "SENTINEL_INGEST_MAX_FILES",
    "SENTINEL_INGEST_MAX_FILE_BYTES",
    "SENTINEL_INGEST_MAX_TOTAL_BYTES",
    "SENTINEL_INGEST_MAX_REQUEST_BYTES",
)

DUMMY_URL = "postgresql://user:pass@localhost:5432/test_db"


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch):
    """Isolate the process environment for every test in this module."""
    for name in (*POOL_ENV_VARS, *RETRY_ENV_VARS, *INGEST_ENV_VARS):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def recording_pool(monkeypatch: pytest.MonkeyPatch):
    """Replace ConnectionPool with a recorder that captures constructor kwargs.

    Avoids opening a real pool (and therefore any real connection attempt) while
    still exercising the full factory → repository → pool-construction path.
    """
    created: List[Any] = []

    class _RecordingPool:
        def __init__(self, **kwargs: Any) -> None:
            self.kwargs = kwargs
            # Mirror the real ConnectionPool attributes callers inspect.
            self.min_size = kwargs.get("min_size")
            self.max_size = kwargs.get("max_size")
            self.timeout = kwargs.get("timeout")
            created.append(self)

        def close(self) -> None:  # pragma: no cover - not exercised here
            pass

    monkeypatch.setattr("core.persistence.postgres.ConnectionPool", _RecordingPool)
    return created


# ===========================================================================
# PoolConfig — defaults
# ===========================================================================

def test_defaults_preserve_previous_hardcoded_values():
    config = pool_config_from_env()
    assert config == PoolConfig(
        min_size=DEFAULT_POOL_MIN_SIZE,
        max_size=DEFAULT_POOL_MAX_SIZE,
        timeout=DEFAULT_POOL_TIMEOUT_SECONDS,
    )
    assert (config.min_size, config.max_size, config.timeout) == (1, 10, 30.0)


def test_blank_values_fall_back_to_defaults(monkeypatch: pytest.MonkeyPatch):
    for name in POOL_ENV_VARS:
        monkeypatch.setenv(name, "   ")
    config = pool_config_from_env()
    assert (config.min_size, config.max_size, config.timeout) == (1, 10, 30.0)


# ===========================================================================
# PoolConfig — custom values
# ===========================================================================

def test_custom_min_and_max_sizes(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv(ENV_POOL_MIN_SIZE, "4")
    monkeypatch.setenv(ENV_POOL_MAX_SIZE, "25")
    config = pool_config_from_env()
    assert config.min_size == 4
    assert config.max_size == 25


def test_custom_timeout(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv(ENV_POOL_TIMEOUT_SECONDS, "7.5")
    assert pool_config_from_env().timeout == 7.5


def test_min_equal_to_max_is_allowed(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv(ENV_POOL_MIN_SIZE, "3")
    monkeypatch.setenv(ENV_POOL_MAX_SIZE, "3")
    config = pool_config_from_env()
    assert (config.min_size, config.max_size) == (3, 3)


# ===========================================================================
# PoolConfig — validation
# ===========================================================================

@pytest.mark.parametrize("name", POOL_ENV_VARS)
def test_non_numeric_values_are_rejected(monkeypatch: pytest.MonkeyPatch, name: str):
    monkeypatch.setenv(name, "not-a-number")
    with pytest.raises(PersistenceError, match="Invalid .* value"):
        pool_config_from_env()


def test_fractional_integer_value_is_rejected(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv(ENV_POOL_MIN_SIZE, "1.5")
    with pytest.raises(PersistenceError, match=ENV_POOL_MIN_SIZE):
        pool_config_from_env()


def test_min_greater_than_max_is_rejected(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv(ENV_POOL_MIN_SIZE, "20")
    monkeypatch.setenv(ENV_POOL_MAX_SIZE, "5")
    with pytest.raises(PersistenceError, match="must not exceed max_size"):
        pool_config_from_env()


@pytest.mark.parametrize("raw", ["0", "-1"])
def test_non_positive_min_size_is_rejected(monkeypatch: pytest.MonkeyPatch, raw: str):
    monkeypatch.setenv(ENV_POOL_MIN_SIZE, raw)
    with pytest.raises(PersistenceError, match="min_size"):
        pool_config_from_env()


@pytest.mark.parametrize("raw", ["0", "-2"])
def test_non_positive_max_size_is_rejected(monkeypatch: pytest.MonkeyPatch, raw: str):
    monkeypatch.setenv(ENV_POOL_MAX_SIZE, raw)
    with pytest.raises(PersistenceError, match="max_size"):
        pool_config_from_env()


@pytest.mark.parametrize("raw", ["0", "-1", "nan", "inf"])
def test_invalid_timeout_is_rejected(monkeypatch: pytest.MonkeyPatch, raw: str):
    monkeypatch.setenv(ENV_POOL_TIMEOUT_SECONDS, raw)
    with pytest.raises(PersistenceError, match="timeout"):
        pool_config_from_env()


def test_error_message_never_echoes_a_connection_string(
    monkeypatch: pytest.MonkeyPatch,
):
    """Sanitised errors name only the offending setting, never a credential/DSN."""
    monkeypatch.setenv("SENTINEL_DATABASE_URL", DUMMY_URL)
    monkeypatch.setenv(ENV_POOL_MIN_SIZE, "not-an-int")
    with pytest.raises(PersistenceError) as excinfo:
        get_repository(backend="postgres", database_url=DUMMY_URL)
    message = str(excinfo.value)
    assert "postgresql://" not in message
    assert "pass" not in message


# ===========================================================================
# Factory wiring — the single shared configuration source
# ===========================================================================

def test_factory_builds_pool_from_default_config(recording_pool):
    repo = get_repository(backend="postgres", database_url=DUMMY_URL)
    assert isinstance(repo, PostgresRepository)
    assert len(recording_pool) == 1
    kwargs = recording_pool[0].kwargs
    assert kwargs["min_size"] == DEFAULT_POOL_MIN_SIZE
    assert kwargs["max_size"] == DEFAULT_POOL_MAX_SIZE
    assert kwargs["timeout"] == DEFAULT_POOL_TIMEOUT_SECONDS


def test_factory_builds_pool_from_environment(monkeypatch: pytest.MonkeyPatch, recording_pool):
    monkeypatch.setenv(ENV_POOL_MIN_SIZE, "2")
    monkeypatch.setenv(ENV_POOL_MAX_SIZE, "12")
    monkeypatch.setenv(ENV_POOL_TIMEOUT_SECONDS, "15.0")

    repo = get_repository(backend="postgres", database_url=DUMMY_URL)

    kwargs = recording_pool[0].kwargs
    assert (kwargs["min_size"], kwargs["max_size"], kwargs["timeout"]) == (2, 12, 15.0)
    assert repo.pool.min_size == 2
    assert repo.pool.max_size == 12
    assert repo.pool.timeout == 15.0


def test_factory_rejects_inconsistent_bounds(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv(ENV_POOL_MIN_SIZE, "8")
    monkeypatch.setenv(ENV_POOL_MAX_SIZE, "4")
    with pytest.raises(PersistenceError, match="must not exceed max_size"):
        get_repository(backend="postgres", database_url=DUMMY_URL)


def test_from_env_uses_the_shared_pool_config(monkeypatch: pytest.MonkeyPatch, recording_pool):
    monkeypatch.setenv("SENTINEL_DATABASE_URL", DUMMY_URL)
    monkeypatch.setenv(ENV_POOL_MIN_SIZE, "3")
    monkeypatch.setenv(ENV_POOL_MAX_SIZE, "9")

    PostgresRepository.from_env()

    kwargs = recording_pool[0].kwargs
    assert (kwargs["min_size"], kwargs["max_size"]) == (3, 9)


# ===========================================================================
# API and worker configuration consistency
# ===========================================================================

class _DeliveryCapableRepository:
    """Minimal repository satisfying the worker's delivery-method guard."""

    def __init__(self) -> None:
        for name in (
            "release_expired_claims",
            "claim_pending",
            "mark_processed",
            "mark_failed",
            "schedule_retry",
        ):
            setattr(self, name, lambda *args, **kwargs: None)

    def close(self) -> None:  # pragma: no cover - not exercised here
        pass


def test_api_and_worker_resolve_identical_pool_config(
    monkeypatch: pytest.MonkeyPatch, recording_pool
):
    """Both entry points construct the repository with the same pool settings.

    The API launcher and the event-worker launcher both call
    ``core.persistence.factory.get_repository()``, so the environment-driven pool
    configuration is resolved exactly once, identically.
    """
    monkeypatch.setenv("SENTINEL_DATABASE_URL", DUMMY_URL)
    monkeypatch.setenv("SENTINEL_PERSISTENCE_BACKEND", "postgres")
    monkeypatch.setenv(ENV_POOL_MIN_SIZE, "5")
    monkeypatch.setenv(ENV_POOL_MAX_SIZE, "15")
    monkeypatch.setenv(ENV_POOL_TIMEOUT_SECONDS, "9.0")

    # --- API entry point ---------------------------------------------------
    import api.__main__ as api_main

    captured: Dict[str, Any] = {}

    def _fake_create_app(*args: Any, **kwargs: Any) -> object:
        captured.update(kwargs)
        return object()

    monkeypatch.setattr("dotenv.load_dotenv", lambda *a, **k: False)
    monkeypatch.setattr("api.app.create_app", _fake_create_app)
    monkeypatch.setattr("uvicorn.run", lambda app, **kwargs: None)
    monkeypatch.setattr("sys.argv", ["python -m api"])
    api_main.main()

    api_pool = captured["repository"].pool

    # --- Worker entry point -------------------------------------------------
    import api.event_worker.worker as worker_module

    monkeypatch.setattr(
        PostgresRepository, "initialize_outbox_schema", lambda self: None
    )
    monkeypatch.setattr(
        worker_module.EventDeliveryWorker, "install_signal_handlers", lambda self, *a, **k: None
    )
    monkeypatch.setattr(worker_module.EventDeliveryWorker, "start", lambda self: None)
    worker_module.main()

    assert len(recording_pool) == 2, "expected one pool per entry point"
    worker_pool = recording_pool[1]

    assert (api_pool.min_size, api_pool.max_size, api_pool.timeout) == (5, 15, 9.0)
    assert (
        worker_pool.kwargs["min_size"],
        worker_pool.kwargs["max_size"],
        worker_pool.kwargs["timeout"],
    ) == (5, 15, 9.0)


@pytest.fixture(autouse=True)
def _restore_logging():
    """The worker entry point calls ``configure_logging``; restore global state."""
    from core.logging_config import APPLICATION_LOGGER_NAMESPACES

    root = logging.getLogger()
    saved_root_level = root.level
    saved_handlers = root.handlers[:]
    saved_levels = {
        name: logging.getLogger(name).level for name in APPLICATION_LOGGER_NAMESPACES
    }
    yield
    root.setLevel(saved_root_level)
    root.handlers[:] = saved_handlers
    for name, level in saved_levels.items():
        logging.getLogger(name).setLevel(level)


# ===========================================================================
# Retry and ingestion settings — still loaded correctly from the environment
# ===========================================================================

def test_retry_settings_are_loaded_from_env(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("SENTINEL_PG_RETRY_MAX_ATTEMPTS", "3")
    monkeypatch.setenv("SENTINEL_PG_RETRY_BASE_DELAY", "0.25")
    monkeypatch.setenv("SENTINEL_PG_RETRY_MAX_DELAY", "2.5")

    policy = policy_from_env()
    assert policy.max_attempts == 3
    assert policy.base_delay_seconds == 0.25
    assert policy.max_delay_seconds == 2.5


def test_retry_settings_default_when_absent():
    policy = policy_from_env()
    assert policy.max_attempts == 5
    assert policy.base_delay_seconds == 0.05
    assert policy.max_delay_seconds == 1.0


def test_ingestion_limits_are_loaded_from_env(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("SENTINEL_INGEST_MAX_FILES", "7")
    monkeypatch.setenv("SENTINEL_INGEST_MAX_FILE_BYTES", "1024")
    monkeypatch.setenv("SENTINEL_INGEST_MAX_TOTAL_BYTES", "4096")
    monkeypatch.setenv("SENTINEL_INGEST_MAX_REQUEST_BYTES", "8192")

    limits = load_ingestion_limits()
    assert limits == IngestionLimits(
        max_files=7,
        max_file_bytes=1024,
        max_total_bytes=4096,
        max_request_bytes=8192,
    )


def test_ingestion_opt_in_is_loaded_from_env(monkeypatch: pytest.MonkeyPatch):
    assert ingestion_enabled() is False
    monkeypatch.setenv("SENTINEL_ALLOW_INCIDENT_INGESTION", "true")
    assert ingestion_enabled() is True
