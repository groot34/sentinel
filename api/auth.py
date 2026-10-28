"""API-key authentication for the Sentinel 2.0 HTTP API (Mission 20).

This module owns a deliberately small, static API-key guard:

* an :class:`AuthConfig` value object and :func:`load_auth_config` loader that
  resolves configuration from explicit arguments or the environment;
* :class:`AuthConfigurationError`, raised when authentication is enabled but no
  usable key is configured, so the process fails closed instead of silently
  serving unprotected requests; and
* :class:`ApiKeyAuthMiddleware`, a **pure ASGI** middleware that rejects
  unauthenticated requests before the request body is read.

Design rules
------------
* Authentication is **disabled by default** so existing local workflows are
  unchanged. Any value other than a recognised truthy token leaves it disabled;
  an unrecognised non-empty value is logged so a typo cannot silently disable
  the guard.
* Only ``GET``, ``HEAD`` and ``OPTIONS`` are considered safe. Every other method
  is protected automatically, so a future mutating route is protected without
  further wiring. Optional read protection (``SENTINEL_API_AUTH_PROTECT_READS``)
  extends the guard to safe methods; ``/health`` stays public regardless.
* The guard is a **pure ASGI** middleware, not ``BaseHTTPMiddleware``: for
  authorised requests the same ``receive``/``send`` callables are passed to the
  downstream app unchanged, so Server-Sent Events streaming and Starlette
  ``BackgroundTask`` dispatch behave exactly as before.
* The configured key is never logged, echoed, or included in an error payload.
  Comparison uses :func:`hmac.compare_digest` over UTF-8 encoded bytes so a
  non-ASCII credential can never raise or take a data-dependent code path.

Not implemented here (deliberately out of scope): OAuth/JWT issuance, user
accounts, roles/RBAC, rate limiting and key rotation.
"""

from __future__ import annotations

import hmac
import logging
import os
from dataclasses import dataclass
from typing import Optional

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from api.errors import error_json_response

ENV_API_AUTH_ENABLED = "SENTINEL_API_AUTH_ENABLED"
ENV_API_KEY = "SENTINEL_API_KEY"
ENV_API_AUTH_PROTECT_READS = "SENTINEL_API_AUTH_PROTECT_READS"

logger = logging.getLogger(__name__)

#: Values (case-insensitive) that enable a boolean environment flag.
_TRUE_VALUES = frozenset({"1", "true", "yes", "on"})

#: Values (case-insensitive) that explicitly disable a boolean environment flag.
_FALSE_VALUES = frozenset({"0", "false", "no", "off"})

#: Methods treated as safe (idempotent, non-mutating) unless read protection is on.
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})

#: Paths that stay public even when read protection is enabled.
PUBLIC_PATHS = frozenset({"/health"})

_BEARER_PREFIX = "bearer "

_UNAUTHORIZED_DETAIL = "Missing or invalid API credentials."


class AuthConfigurationError(Exception):
    """Raised when authentication is enabled but misconfigured.

    Construction fails closed: an enabled-but-unusable configuration must stop
    the application from starting rather than run unprotected.
    """


@dataclass(frozen=True)
class AuthConfig:
    """Resolved authentication configuration.

    ``api_key`` is ``None`` whenever authentication is disabled, so the secret
    is only ever held in memory while the guard is actually active. A redacted
    view (see :meth:`redacted`) also has ``api_key`` set to ``None`` and is what
    the application exposes on ``app.state``; the live secret lives only inside
    :class:`ApiKeyAuthMiddleware`.
    """

    enabled: bool = False
    api_key: Optional[str] = None
    protect_reads: bool = False

    def redacted(self) -> "AuthConfig":
        """Return an equivalent configuration with the API key removed.

        Used to expose enablement state (e.g. on ``app.state``) without holding
        a second in-memory copy of the secret.
        """
        return AuthConfig(enabled=self.enabled, api_key=None, protect_reads=self.protect_reads)


def _env_bool(name: str, default: bool = False) -> bool:
    """Read a truthy environment flag, falling back to *default*.

    An absent or empty value yields *default*. A non-empty value that is neither
    a recognised truthy token (``1``/``true``/``yes``/``on``) nor a recognised
    falsey token (``0``/``false``/``no``/``off``) is treated as disabled but is
    never ignored silently — a sanitised warning is logged so a typo cannot
    quietly weaken the guard. The value itself is not logged.
    """
    raw = os.getenv(name)
    if raw is None:
        return default
    token = str(raw).strip().lower()
    if token == "":
        return default
    if token in _TRUE_VALUES:
        return True
    if token in _FALSE_VALUES:
        return False
    logger.warning(
        "Unrecognised boolean value for %s; treating it as disabled. "
        "Accepted truthy values: %s.",
        name,
        ", ".join(sorted(_TRUE_VALUES)),
    )
    return False


def load_auth_config(
    enabled: Optional[bool] = None,
    api_key: Optional[str] = None,
    protect_reads: Optional[bool] = None,
) -> AuthConfig:
    """Resolve the effective :class:`AuthConfig`.

    Explicit arguments take precedence over the environment, mirroring the
    ``create_app`` factory convention used elsewhere in the API layer.

    Raises:
        AuthConfigurationError: when authentication is enabled but the key is
            missing or blank after trimming.
    """
    resolved_enabled = _env_bool(ENV_API_AUTH_ENABLED) if enabled is None else bool(enabled)

    if api_key is None:
        api_key = os.getenv(ENV_API_KEY)
    resolved_key = api_key.strip() if isinstance(api_key, str) else ""

    resolved_protect_reads = (
        _env_bool(ENV_API_AUTH_PROTECT_READS) if protect_reads is None else bool(protect_reads)
    )

    if resolved_enabled and not resolved_key:
        raise AuthConfigurationError(
            f"API authentication is enabled ({ENV_API_AUTH_ENABLED}) but {ENV_API_KEY} "
            "is missing or blank. Refusing to start with authentication enabled and no "
            "usable key."
        )

    return AuthConfig(
        enabled=resolved_enabled,
        api_key=resolved_key if resolved_enabled else None,
        protect_reads=resolved_protect_reads if resolved_enabled else False,
    )


def _unauthorized_response() -> JSONResponse:
    """Build the single generic 401 response used for every credential failure."""
    response = error_json_response(401, "unauthorized", _UNAUTHORIZED_DETAIL)
    response.headers["WWW-Authenticate"] = "Bearer"
    return response


class ApiKeyAuthMiddleware:
    """Pure-ASGI API-key guard.

    Rejects unauthenticated requests *before* the downstream application runs,
    so a protected endpoint never reads the request body of an unauthenticated
    caller. Authorised requests are forwarded unchanged using the original
    ``receive``/``send`` callables.
    """

    def __init__(self, app: ASGIApp, config: AuthConfig) -> None:
        self.app = app
        self.config = config
        # Encode once so comparison is always bytes-to-bytes.
        self._expected_key: bytes = (config.api_key or "").encode("utf-8")

    def is_protected(self, scope: Scope) -> bool:
        """Return True when *scope* must present a valid API key."""
        if not self.config.enabled or scope.get("type") != "http":
            return False

        path = scope.get("path", "")
        if path in PUBLIC_PATHS:
            return False

        method = str(scope.get("method") or "GET").upper()
        if method in SAFE_METHODS:
            # Safe methods are only guarded when read protection is explicitly on.
            return self.config.protect_reads
        return True

    @staticmethod
    def _extract_token(scope: Scope) -> Optional[str]:
        """Return the single bearer token from the ``Authorization`` header.

        Exactly one ``Authorization`` header is required. Duplicate headers are
        ambiguous — proxies and servers disagree about which value wins — so
        they are rejected outright instead of silently trusting the first one.
        """
        found: Optional[str] = None
        saw_authorization = False
        for name, value in scope.get("headers", []) or []:
            if name != b"authorization":
                continue
            if saw_authorization:
                # More than one Authorization header: refuse rather than guess.
                return None
            saw_authorization = True
            try:
                decoded = value.decode("latin-1")
            except (UnicodeDecodeError, AttributeError):
                return None
            if decoded[: len(_BEARER_PREFIX)].lower() != _BEARER_PREFIX:
                # Missing or non-Bearer scheme.
                return None
            found = decoded[len(_BEARER_PREFIX) :].strip()
        return found

    def _is_authorised(self, scope: Scope) -> bool:
        provided = self._extract_token(scope)
        if not provided:
            return False
        # compare_digest over bytes: never raises, no unsafe type coercion.
        return hmac.compare_digest(provided.encode("utf-8"), self._expected_key)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if self.is_protected(scope) and not self._is_authorised(scope):
            # Short-circuit: the downstream app (and any body read) never runs.
            await _unauthorized_response()(scope, receive, send)
            return
        await self.app(scope, receive, send)
