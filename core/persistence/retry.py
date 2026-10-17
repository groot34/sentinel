"""Bounded retry for transient PostgreSQL transaction failures (Mission 15).

Retries the *entire* database transaction on transient PostgreSQL contention
errors — SQLSTATE 40001 (``serialization_failure``) and 40P01
(``deadlock_detected``) — using a conservative, bounded exponential backoff with
full jitter.

Boundaries (see ``docs/ARCHITECTURE.md`` section 4.4):

* Only retryable PostgreSQL SQLSTATEs are retried. Application-level
  ``ConcurrencyError`` (OCC), validation failures, and permanent database errors
  propagate immediately and are never retried.
* The retried callable must encapsulate exactly one side-effect-free
  transaction, so re-execution cannot duplicate observable side effects.
* A deterministic, test-only fault-injection seam
  (:func:`inject_transient_fault`) is a no-op unless test code installs a hook.

These helpers deliberately avoid logging connection strings, credentials, or
incident payloads.
"""

from __future__ import annotations

import logging
import os
import random
import time
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Callable, FrozenSet, Optional, TypeVar

from core.persistence.repository import PersistenceError

logger = logging.getLogger(__name__)

_T = TypeVar("_T")

SERIALIZATION_FAILURE = "40001"
DEADLOCK_DETECTED = "40P01"

DEFAULT_RETRYABLE_SQLSTATES: FrozenSet[str] = frozenset(
    {SERIALIZATION_FAILURE, DEADLOCK_DETECTED}
)

# Conservative defaults: a small bounded budget with short delays so transient
# contention resolves quickly without masking persistent failures.
DEFAULT_MAX_ATTEMPTS = 5
DEFAULT_BASE_DELAY_SECONDS = 0.05
DEFAULT_MAX_DELAY_SECONDS = 1.0
_MAX_DELAY_CEILING = 30.0


class RetryExhaustedError(PersistenceError):
    """Raised when retryable transient PostgreSQL failures exhaust the budget.

    Subclasses :class:`PersistenceError` so existing API error handling maps it
    to ``503 Service Unavailable`` (``persistence_unavailable``) — a safe,
    client-retryable signal — rather than an unhandled 500.
    """


@dataclass(frozen=True)
class RetryPolicy:
    """Immutable configuration for bounded transient-failure retry."""

    max_attempts: int = DEFAULT_MAX_ATTEMPTS
    base_delay_seconds: float = DEFAULT_BASE_DELAY_SECONDS
    max_delay_seconds: float = DEFAULT_MAX_DELAY_SECONDS
    retryable_sqlstates: FrozenSet[str] = DEFAULT_RETRYABLE_SQLSTATES

    def __post_init__(self) -> None:
        if self.max_attempts < 1:
            raise ValueError("max_attempts must be >= 1")
        if self.base_delay_seconds < 0:
            raise ValueError("base_delay_seconds must be >= 0")
        if self.max_delay_seconds < 0:
            raise ValueError("max_delay_seconds must be >= 0")
        if self.max_delay_seconds > _MAX_DELAY_CEILING:
            raise ValueError(f"max_delay_seconds must be <= {_MAX_DELAY_CEILING}")


def policy_from_env() -> RetryPolicy:
    """Build a :class:`RetryPolicy` from optional environment overrides.

    Recognized variables (all optional, conservative defaults apply):
      * ``SENTINEL_PG_RETRY_MAX_ATTEMPTS``  (default 5)
      * ``SENTINEL_PG_RETRY_BASE_DELAY``    seconds (default 0.05)
      * ``SENTINEL_PG_RETRY_MAX_DELAY``     seconds (default 1.0)

    Out-of-range or malformed values are clamped to valid bounds rather than
    raising, so a bad environment cannot break repository construction.
    """

    def _num(name: str, default: float) -> float:
        raw = os.getenv(name)
        if raw is None or not str(raw).strip():
            return default
        try:
            return float(raw)
        except (TypeError, ValueError):
            logger.warning("Invalid %s value; falling back to default", name)
            return default

    attempts_raw = os.getenv("SENTINEL_PG_RETRY_MAX_ATTEMPTS")
    try:
        max_attempts = int(attempts_raw) if attempts_raw and attempts_raw.strip() else DEFAULT_MAX_ATTEMPTS
    except (TypeError, ValueError):
        max_attempts = DEFAULT_MAX_ATTEMPTS

    return RetryPolicy(
        max_attempts=max(1, max_attempts),
        base_delay_seconds=max(0.0, _num("SENTINEL_PG_RETRY_BASE_DELAY", DEFAULT_BASE_DELAY_SECONDS)),
        max_delay_seconds=min(
            _MAX_DELAY_CEILING,
            max(0.0, _num("SENTINEL_PG_RETRY_MAX_DELAY", DEFAULT_MAX_DELAY_SECONDS)),
        ),
    )


def is_retryable_pg_error(
    exc: BaseException,
    retryable_sqlstates: FrozenSet[str] = DEFAULT_RETRYABLE_SQLSTATES,
) -> bool:
    """Return True only for transient PostgreSQL errors with a retryable SQLSTATE.

    Unwraps ``__cause__`` / ``__context__`` chains because repository methods
    wrap raw psycopg errors into ``PersistenceError``. Application-level
    ``ConcurrencyError`` and validation errors carry no SQLSTATE and therefore
    return False (never retried).
    """
    seen = set()
    current: Optional[BaseException] = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        sqlstate = getattr(current, "sqlstate", None) or getattr(current, "pgcode", None)
        if sqlstate in retryable_sqlstates:
            return True
        current = current.__cause__ or current.__context__
    return False


def compute_backoff_delay(
    retry_index: int,
    policy: RetryPolicy,
    rng: random.Random,
) -> float:
    """Compute a jittered exponential backoff delay for a 1-based retry index.

    The un-jittered ceiling grows as ``base * 2**(retry_index-1)`` and is capped
    at ``policy.max_delay_seconds``. Full jitter (uniform in ``[0, ceiling]``)
    avoids synchronized retry storms. Guaranteed bounds:
    ``0 <= delay <= max_delay_seconds``.
    """
    ceiling = policy.base_delay_seconds * (2 ** (retry_index - 1))
    ceiling = min(ceiling, policy.max_delay_seconds)
    if ceiling <= 0:
        return 0.0
    return rng.uniform(0.0, ceiling)


def run_with_retry(
    operation: Callable[[int], _T],
    policy: Optional[RetryPolicy] = None,
    sleep: Callable[[float], None] = time.sleep,
    rng: random.Random = random,
) -> _T:
    """Execute ``operation(attempt)`` with bounded retry on transient PG errors.

    ``operation`` receives a 1-based attempt index and must encapsulate exactly
    one complete, side-effect-free transaction (with its own fresh connection).

    Non-retryable exceptions (including ``ConcurrencyError`` and permanent
    database errors) propagate immediately on first occurrence. When the budget
    is exhausted a :class:`RetryExhaustedError` is raised, chaining the last
    transient error.
    """
    active = policy or RetryPolicy()
    last_error: Optional[BaseException] = None

    for attempt in range(1, active.max_attempts + 1):
        try:
            return operation(attempt)
        except Exception as exc:  # noqa: BLE001 - re-raised unless retryable
            if not is_retryable_pg_error(exc, active.retryable_sqlstates):
                raise
            last_error = exc
            if attempt >= active.max_attempts:
                break
            delay = compute_backoff_delay(attempt, active, rng)
            logger.warning(
                "Transient PostgreSQL failure (sqlstate=%s) on attempt %d/%d; retrying in %.3fs",
                getattr(exc, "sqlstate", "unknown"),
                attempt,
                active.max_attempts,
                delay,
            )
            if delay > 0:
                sleep(delay)

    raise RetryExhaustedError(
        "Exhausted {} attempts after transient PostgreSQL failure (sqlstate={}).".format(
            active.max_attempts,
            getattr(last_error, "sqlstate", "unknown"),
        )
    ) from last_error


# ---------------------------------------------------------------------------
# Deterministic, test-only fault-injection seam
# ---------------------------------------------------------------------------

_FAULT_HOOK: ContextVar[Optional[Callable[[int], None]]] = ContextVar(
    "sentinel_pg_fault_hook", default=None
)


@contextmanager
def inject_transient_fault(hook: Callable[[int], None]):
    """Install a test-only hook invoked inside the transaction on each attempt.

    The hook receives the 1-based attempt index and may raise a synthetic
    PostgreSQL error (e.g. ``psycopg.errors.SerializationFailure``) to exercise
    retry behaviour deterministically. This context manager is the only way to
    activate the seam; no hook is installed in production, so
    :func:`maybe_inject_fault` is a no-op there.
    """
    token = _FAULT_HOOK.set(hook)
    try:
        yield
    finally:
        _FAULT_HOOK.reset(token)


def maybe_inject_fault(attempt: int) -> None:
    """Invoke the installed fault hook, if any. No-op in production."""
    hook = _FAULT_HOOK.get()
    if hook is not None:
        hook(attempt)
