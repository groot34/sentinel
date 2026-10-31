"""Shared logging configuration for the Sentinel entry points.

Both the API server (``python -m api``) and the event delivery worker
(``python -m api.event_worker``) read the same ``LOG_LEVEL`` environment
variable and apply it through :func:`configure_logging`, so the two processes
log at a consistent level from a single implementation.

Only Sentinel's own logger namespaces are given a level. The root logger keeps
Python's default level and is merely given a handler, so third-party libraries
(``uvicorn``, ``httpx``, ``groq``, ...) keep their own levels instead of
inheriting the application's verbosity.
"""

from __future__ import annotations

import logging
import os
from typing import Optional

#: Environment variable controlling application log verbosity.
ENV_LOG_LEVEL = "LOG_LEVEL"

#: Applied when ``LOG_LEVEL`` is absent, empty, or unrecognised.
DEFAULT_LOG_LEVEL = "INFO"

#: Accepted level names (case-insensitive). Mirrors the ``--log-level`` CLI
#: choices; ``NOTSET`` is excluded because it is not a usable verbosity.
VALID_LOG_LEVELS = ("CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG")

#: Top-level packages owned by Sentinel. Third-party loggers are left alone.
APPLICATION_LOGGER_NAMESPACES = ("api", "agents", "core")

_LOG_FORMAT = "%(asctime)s - %(name)s - %(levelname)s - %(message)s"


def resolve_log_level(value: Optional[str] = None) -> str:
    """Return the effective level name for ``LOG_LEVEL``.

    Reads the environment unless *value* is given. Absent, empty,
    whitespace-only, or unrecognised values fall back to
    :data:`DEFAULT_LOG_LEVEL`, so a typo can never silence or flood the logs.
    The result is upper-case and always one of :data:`VALID_LOG_LEVELS`.
    """
    if value is None:
        value = os.getenv(ENV_LOG_LEVEL)
    if value is None:
        return DEFAULT_LOG_LEVEL
    candidate = str(value).strip().upper()
    return candidate if candidate in VALID_LOG_LEVELS else DEFAULT_LOG_LEVEL


def configure_logging(value: Optional[str] = None) -> str:
    """Apply ``LOG_LEVEL`` to Sentinel's loggers and return the level name.

    Attaches a root ``StreamHandler`` once (``basicConfig`` is a no-op when a
    handler already exists, so repeated calls are harmless) and sets the level
    on :data:`APPLICATION_LOGGER_NAMESPACES`. The root level is deliberately
    left at its default, so ``LOG_LEVEL=DEBUG`` does not also make libraries
    verbose.
    """
    level_name = resolve_log_level(value)
    level = getattr(logging, level_name)
    logging.basicConfig(format=_LOG_FORMAT)
    for namespace in APPLICATION_LOGGER_NAMESPACES:
        logging.getLogger(namespace).setLevel(level)
    return level_name
