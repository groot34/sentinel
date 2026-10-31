"""Focused tests for the runtime environment configuration (Mission 22, Task 6).

Covers the two variables that remain supported after the audit:

* ``LOG_LEVEL`` — applied identically by the API and event-worker entry points.
* ``INCIDENTS_DIR`` — the incidents directory used by the API for bundle
  validation and ingestion.

``SENTINEL_ENV`` was removed as redundant; a guard test keeps it from creeping
back into ``.env.example``.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict, List

import pytest

import api.__main__ as api_main
from api.event_worker.worker import REQUIRED_DELIVERY_METHODS
from core.logging_config import (
    APPLICATION_LOGGER_NAMESPACES,
    DEFAULT_LOG_LEVEL,
    ENV_LOG_LEVEL,
    VALID_LOG_LEVELS,
    configure_logging,
    resolve_log_level,
)

REPO_ROOT = Path(__file__).resolve().parent.parent

ENV_INCIDENTS_DIR = api_main.ENV_INCIDENTS_DIR


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch):
    """Isolate the process environment for every test in this module."""
    monkeypatch.delenv(ENV_LOG_LEVEL, raising=False)
    monkeypatch.delenv(ENV_INCIDENTS_DIR, raising=False)


@pytest.fixture(autouse=True)
def _restore_logging():
    """Restore global logging state mutated by ``configure_logging``."""
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


class _DeliveryCapableRepository:
    """Minimal repository satisfying the worker's delivery-method guard."""

    def __init__(self) -> None:
        for name in REQUIRED_DELIVERY_METHODS:
            setattr(self, name, lambda *args, **kwargs: None)


def _run_api_main(
    monkeypatch: pytest.MonkeyPatch, argv: List[str]
) -> Dict[str, Any]:
    """Run ``api.__main__.main()`` with side effects stubbed out.

    Returns the keyword arguments ``create_app`` was called with.
    """
    captured: Dict[str, Any] = {}
    runner: Dict[str, Any] = {}

    def _fake_create_app(*args: Any, **kwargs: Any) -> object:
        captured["args"] = args
        captured.update(kwargs)
        return object()

    monkeypatch.setattr("dotenv.load_dotenv", lambda *a, **k: False)
    monkeypatch.setattr("core.persistence.factory.get_repository", lambda *a, **k: object())
    monkeypatch.setattr("api.app.create_app", _fake_create_app)
    monkeypatch.setattr("uvicorn.run", lambda app, **kwargs: runner.update(kwargs))
    monkeypatch.setattr("sys.argv", ["python -m api", *argv])

    api_main.main()
    return captured


# ===========================================================================
# LOG_LEVEL — resolution
# ===========================================================================

def test_log_level_defaults_to_info_when_absent():
    assert DEFAULT_LOG_LEVEL == "INFO"
    assert resolve_log_level() == "INFO"


def test_log_level_reads_the_environment(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv(ENV_LOG_LEVEL, "DEBUG")
    assert resolve_log_level() == "DEBUG"


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("DEBUG", "DEBUG"),
        ("debug", "DEBUG"),
        ("WaRnInG", "WARNING"),
        (" error ", "ERROR"),
        ("critical", "CRITICAL"),
        ("info", "INFO"),
    ],
)
def test_log_level_accepts_valid_names_case_insensitively(raw: str, expected: str):
    assert resolve_log_level(raw) == expected
    assert expected in VALID_LOG_LEVELS


@pytest.mark.parametrize("raw", ["verbose", "10", "NOTSET", "trace", "", "   "])
def test_log_level_invalid_values_fall_back_to_info(raw: str):
    assert resolve_log_level(raw) == DEFAULT_LOG_LEVEL


# ===========================================================================
# LOG_LEVEL — application
# ===========================================================================

def test_configure_logging_sets_sentinels_loggers_only():
    root = logging.getLogger()
    root_level_before = root.level

    assert configure_logging("DEBUG") == "DEBUG"

    for namespace in APPLICATION_LOGGER_NAMESPACES:
        assert logging.getLogger(namespace).level == logging.DEBUG
    # Third-party library loggers inherit from root, whose level is untouched.
    assert root.level == root_level_before


def test_configure_logging_defaults_and_ignores_invalid_values():
    assert configure_logging() == DEFAULT_LOG_LEVEL
    assert configure_logging("nonsense") == DEFAULT_LOG_LEVEL
    assert logging.getLogger("api").level == logging.INFO


def test_configure_logging_is_idempotent():
    configure_logging("WARNING")
    handlers_after_first = list(logging.getLogger().handlers)
    configure_logging("WARNING")
    assert list(logging.getLogger().handlers) == handlers_after_first


def test_api_entry_point_applies_log_level(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv(ENV_LOG_LEVEL, "debug")
    _run_api_main(monkeypatch, [])
    assert logging.getLogger("api").level == logging.DEBUG


def test_api_entry_point_defaults_log_level_to_info(monkeypatch: pytest.MonkeyPatch):
    _run_api_main(monkeypatch, [])
    assert logging.getLogger("api").level == logging.INFO


def test_api_entry_point_invalid_log_level_falls_back_to_info(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv(ENV_LOG_LEVEL, "loud")
    _run_api_main(monkeypatch, [])
    assert logging.getLogger("api").level == logging.INFO


def test_worker_entry_point_applies_log_level(monkeypatch: pytest.MonkeyPatch):
    import api.event_worker.worker as worker_module

    monkeypatch.setenv(ENV_LOG_LEVEL, "error")
    monkeypatch.setattr("dotenv.load_dotenv", lambda *a, **k: False)
    monkeypatch.setattr(
        "core.persistence.factory.get_repository",
        lambda *a, **k: _DeliveryCapableRepository(),
    )
    monkeypatch.setattr(
        worker_module.EventDeliveryWorker, "install_signal_handlers", lambda self, *a, **k: None
    )
    monkeypatch.setattr(worker_module.EventDeliveryWorker, "start", lambda self: None)

    worker_module.main()

    assert logging.getLogger("api").level == logging.ERROR
    assert logging.getLogger("api.event_worker.worker").getEffectiveLevel() == logging.ERROR


def test_api_and_worker_agree_on_the_configured_level(monkeypatch: pytest.MonkeyPatch):
    import api.event_worker.worker as worker_module

    monkeypatch.setenv(ENV_LOG_LEVEL, "warning")
    _run_api_main(monkeypatch, [])
    api_level = logging.getLogger("api").level

    monkeypatch.setattr("dotenv.load_dotenv", lambda *a, **k: False)
    monkeypatch.setattr(
        "core.persistence.factory.get_repository",
        lambda *a, **k: _DeliveryCapableRepository(),
    )
    monkeypatch.setattr(
        worker_module.EventDeliveryWorker, "install_signal_handlers", lambda self, *a, **k: None
    )
    monkeypatch.setattr(worker_module.EventDeliveryWorker, "start", lambda self: None)
    worker_module.main()

    assert api_level == logging.WARNING
    assert logging.getLogger("api").level == logging.WARNING


# ===========================================================================
# INCIDENTS_DIR
# ===========================================================================

def test_incidents_dir_defaults_to_none_without_configuration(
    monkeypatch: pytest.MonkeyPatch,
):
    captured = _run_api_main(monkeypatch, [])
    # None preserves create_app()'s own Path("incidents") default.
    assert captured["incidents_root"] is None


def test_incidents_dir_environment_variable_is_used(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv(ENV_INCIDENTS_DIR, str(Path("/srv/bundles")))
    captured = _run_api_main(monkeypatch, [])
    assert captured["incidents_root"] == Path("/srv/bundles")


def test_cli_incidents_root_overrides_the_environment(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv(ENV_INCIDENTS_DIR, "/srv/bundles")
    captured = _run_api_main(monkeypatch, ["--incidents-root", "/explicit/path"])
    assert captured["incidents_root"] == Path("/explicit/path")


@pytest.mark.parametrize("raw", ["", "   "])
def test_blank_incidents_dir_falls_back_to_the_default(
    monkeypatch: pytest.MonkeyPatch, raw: str
):
    monkeypatch.setenv(ENV_INCIDENTS_DIR, raw)
    captured = _run_api_main(monkeypatch, [])
    assert captured["incidents_root"] is None


def test_relative_incidents_dir_matches_the_local_default(
    monkeypatch: pytest.MonkeyPatch,
):
    # The shipped default ('incidents') equals create_app()'s built-in default,
    # so wiring the variable cannot silently relocate local bundles.
    monkeypatch.setenv(ENV_INCIDENTS_DIR, "incidents")
    captured = _run_api_main(monkeypatch, [])
    assert captured["incidents_root"] == Path("incidents")


# ===========================================================================
# SENTINEL_ENV — removal guard
# ===========================================================================

def test_env_example_advertises_only_supported_variables():
    content = (REPO_ROOT / ".env.example").read_text(encoding="utf-8")
    assert "SENTINEL_ENV" not in content
    assert "LOG_LEVEL=" in content
    assert "INCIDENTS_DIR=" in content
