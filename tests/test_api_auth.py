"""Tests for Mission 20: configurable API-key authentication.

Covers the disabled-by-default behaviour, credential transport and rejection,
constant-time comparison, fail-closed configuration validation, secret
non-leakage, the optional read-protection policy, SSE pass-through, ingestion
independence, and a fail-closed route-coverage regression over every non-safe
HTTP method.
"""

from __future__ import annotations

import asyncio
import logging
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest
from starlette.testclient import TestClient

import api.__main__ as api_main
from api.app import create_app
from api.auth import (
    ENV_API_AUTH_ENABLED,
    ENV_API_AUTH_PROTECT_READS,
    ENV_API_KEY,
    ApiKeyAuthMiddleware,
    AuthConfig,
    AuthConfigurationError,
    load_auth_config,
)
from api.routes import _ACTIVE_INVESTIGATIONS, _ACTIVE_LOCK
from core.domain.models import IncidentStatus
from core.domain.state import InvestigationState
from core.persistence import PersistenceRepository

SECRET_KEY = "SUPER_SECRET_KEY_123"
AUTH_ENV_VARS = (ENV_API_AUTH_ENABLED, ENV_API_KEY, ENV_API_AUTH_PROTECT_READS)

#: Mutating endpoints that must be protected by default.
MUTATING_PROBES = [
    ("POST", "/incidents"),
    ("POST", "/investigations"),
    ("POST", "/investigations/probe/resume"),
    ("POST", "/investigations/probe/approval"),
]

_PATH_PARAM_RE = re.compile(r"\{[^}]+\}")


# ---------------------------------------------------------------------------
# Fixtures & helpers
# ---------------------------------------------------------------------------

class FakeRepository(PersistenceRepository):
    """Minimal in-memory repository for deterministic auth tests."""

    def __init__(self, states: Optional[Dict[str, InvestigationState]] = None) -> None:
        self._states: Dict[str, InvestigationState] = states or {}
        self.save_called: List[InvestigationState] = []

    def save(self, state: InvestigationState, events: Optional[List[Any]] = None) -> None:
        self.save_called.append(state)
        self._states[state.incident_id] = state

    def load(self, investigation_id: str) -> Optional[InvestigationState]:
        return self._states.get(investigation_id)

    def exists(self, investigation_id: str) -> bool:
        return investigation_id in self._states

    def delete(self, investigation_id: str) -> None:
        self._states.pop(investigation_id, None)

    def list(self) -> List[str]:
        return sorted(self._states.keys())

    @property
    def supports_event_history(self) -> bool:
        return True

    def list_events(self, investigation_id: str, limit: int = 200, after_cursor: Any = None) -> List:
        return []


@pytest.fixture(autouse=True)
def _clean_env_and_registry(monkeypatch: pytest.MonkeyPatch):
    """Ensure a clean process-local registry and no inherited auth env."""
    for name in AUTH_ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    with _ACTIVE_LOCK:
        _ACTIVE_INVESTIGATIONS.clear()
    yield
    with _ACTIVE_LOCK:
        _ACTIVE_INVESTIGATIONS.clear()


@pytest.fixture
def incidents_root(tmp_path: Path) -> Path:
    root = tmp_path / "incidents"
    root.mkdir()
    (root / "inc_empty_bundle").mkdir()  # evidence-free bundles are valid (warning only)
    return root


def _client(**kwargs: Any) -> TestClient:
    return TestClient(create_app(repository=FakeRepository(), **kwargs))


def _bearer(key: str) -> Dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


def _enabled_client(root: Optional[Path] = None, protect_reads: bool = False) -> TestClient:
    kwargs: Dict[str, Any] = dict(auth_enabled=True, api_key=SECRET_KEY, auth_protect_reads=protect_reads)
    if root is not None:
        kwargs["incidents_root"] = root
    return _client(**kwargs)


# ===========================================================================
# 1. Default-disabled compatibility
# ===========================================================================

def test_disabled_by_default_preserves_existing_behaviour(incidents_root: Path):
    client = _client(incidents_root=incidents_root)

    # Read route open, no credential required.
    assert client.get("/health").status_code == 200

    # Mutating route reaches the handler (400 validation) instead of 401.
    resp = client.post("/investigations", json={"incident_id": ""})
    assert resp.status_code == 400
    assert resp.json()["error"] == "invalid_incident_id"


def test_disabled_config_is_inert():
    config = load_auth_config(enabled=False, api_key=None, protect_reads=True)
    assert config.enabled is False
    assert config.api_key is None  # secret not held while disabled
    assert config.protect_reads is False


# ===========================================================================
# 2. Credential rejection (missing / wrong / empty / malformed / non-ASCII)
# ===========================================================================

@pytest.mark.parametrize("method,path", MUTATING_PROBES)
def test_missing_credential_is_unauthorized(method: str, path: str, incidents_root: Path):
    client = _enabled_client(incidents_root)
    resp = client.request(method, path)
    assert resp.status_code == 401
    assert resp.json() == {"error": "unauthorized", "detail": "Missing or invalid API credentials."}
    assert resp.headers.get("www-authenticate") == "Bearer"


def test_wrong_key_is_unauthorized(incidents_root: Path):
    client = _enabled_client(incidents_root)
    resp = client.post("/investigations", json={"incident_id": "x"}, headers=_bearer("wrong-key"))
    assert resp.status_code == 401


def test_empty_bearer_token_is_unauthorized(incidents_root: Path):
    client = _enabled_client(incidents_root)
    resp = client.post("/investigations", json={"incident_id": "x"}, headers={"Authorization": "Bearer "})
    assert resp.status_code == 401


def test_malformed_scheme_is_unauthorized(incidents_root: Path):
    client = _enabled_client(incidents_root)
    resp = client.post("/investigations", json={"incident_id": "x"}, headers={"Authorization": "Basic abc"})
    assert resp.status_code == 401


def _run_middleware(
    api_key: str,
    headers: List[Any],
    method: str = "POST",
    path: str = "/investigations",
    protect_reads: bool = False,
    downstream: Optional[Dict[str, Any]] = None,
) -> List[Dict[str, Any]]:
    """Drive ApiKeyAuthMiddleware directly with raw ASGI header bytes."""
    async def _app(scope, receive, send):
        if downstream is not None:
            downstream["called"] = True

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    sent: List[Dict[str, Any]] = []

    async def send(message):
        sent.append(message)

    middleware = ApiKeyAuthMiddleware(
        _app, AuthConfig(enabled=True, api_key=api_key, protect_reads=protect_reads)
    )
    scope = {"type": "http", "method": method, "path": path, "headers": headers}
    asyncio.run(middleware(scope, receive, send))
    return sent


def test_non_ascii_provided_credential_is_rejected_without_error():
    # Raw ASGI header bytes mirror what a real non-ASCII client would send.
    headers = [(b"authorization", b"Bearer " + "clé-secrète-é".encode("latin-1"))]
    sent = _run_middleware(SECRET_KEY, headers)
    assert sent[0]["type"] == "http.response.start"
    assert sent[0]["status"] == 401  # no exception raised, generic rejection


def test_non_ascii_configured_key_rejects_non_matching_token():
    sent = _run_middleware("sécret-clé-ü", [(b"authorization", b"Bearer wrong-token")])
    assert sent[0]["status"] == 401  # non-ASCII config never raises during compare


def test_non_ascii_provided_token_does_not_reach_downstream():
    seen: Dict[str, Any] = {"called": False}
    headers = [(b"authorization", b"Bearer " + "kéy".encode("latin-1"))]
    _run_middleware(SECRET_KEY, headers, downstream=seen)
    assert seen["called"] is False


# ===========================================================================
# 3. Valid credentials preserve behaviour
# ===========================================================================

def test_valid_credential_passes_sync_request_through(incidents_root: Path):
    client = _enabled_client(incidents_root)
    resp = client.post(
        "/investigations", json={"incident_id": "does_not_exist"}, headers=_bearer(SECRET_KEY)
    )
    assert resp.status_code == 404
    assert resp.json()["error"] == "incident_not_found"


def test_valid_credential_passes_async_request_through(incidents_root: Path, monkeypatch: pytest.MonkeyPatch):
    # Prevent the background pipeline (and any LLM call) from running.
    monkeypatch.setattr("api.routes._run_investigation_background", lambda *a, **k: None)
    client = _enabled_client(incidents_root)
    resp = client.post(
        "/investigations",
        json={"incident_id": "inc_empty_bundle"},
        headers={**_bearer(SECRET_KEY), "Prefer": "respond-async"},
    )
    assert resp.status_code == 202
    assert "Location" in resp.headers


# ===========================================================================
# 4. Resume and approval routes
# ===========================================================================

def test_resume_route_requires_credential(incidents_root: Path):
    client = _enabled_client(incidents_root)
    assert client.post("/investigations/inc_empty_bundle/resume").status_code == 401


def test_resume_route_authenticated_passes_through(incidents_root: Path):
    client = _enabled_client(incidents_root)
    resp = client.post(
        "/investigations/does_not_exist/resume", headers=_bearer(SECRET_KEY)
    )
    assert resp.status_code == 404
    assert resp.json()["error"] == "incident_not_found"


def test_approval_route_requires_credential(incidents_root: Path):
    client = _enabled_client(incidents_root)
    resp = client.post(
        "/investigations/inc_x/approval",
        json={"proposal_id": "FIX-001", "decision": "approved", "reviewer": "alice"},
    )
    assert resp.status_code == 401


def test_approval_route_authenticated_passes_through(incidents_root: Path):
    client = _enabled_client(incidents_root)
    resp = client.post(
        "/investigations/inc_missing/approval",
        json={"proposal_id": "FIX-001", "decision": "approved", "reviewer": "alice"},
        headers=_bearer(SECRET_KEY),
    )
    assert resp.status_code == 404
    assert resp.json()["error"] == "investigation_not_found"


# ===========================================================================
# 5. Ingestion stays independently opt-in
# ===========================================================================

def test_auth_enabled_does_not_enable_ingestion(incidents_root: Path):
    client = _enabled_client(incidents_root)  # ingestion_enabled not passed -> env default False
    resp = client.post(
        "/incidents",
        json={"incident_id": "inc_new", "files": {"logs/application.log": "x"}},
        headers=_bearer(SECRET_KEY),
    )
    assert resp.status_code == 404
    assert resp.json()["error"] == "ingestion_disabled"


def test_ingestion_opt_in_is_separate_from_auth(incidents_root: Path):
    client = TestClient(
        create_app(
            repository=FakeRepository(),
            incidents_root=incidents_root,
            ingestion_enabled=True,
        )
    )
    resp = client.post(
        "/incidents",
        json={"incident_id": "inc_new", "files": {"logs/application.log": "hello"}},
    )
    assert resp.status_code == 201  # ingestion path unchanged when auth is disabled


# ===========================================================================
# 6–7. Fail-closed configuration validation and startup
# ===========================================================================

def test_enabled_without_key_fails_app_construction():
    with pytest.raises(AuthConfigurationError):
        create_app(repository=FakeRepository(), auth_enabled=True, api_key=None)


def test_enabled_with_blank_key_fails_app_construction():
    with pytest.raises(AuthConfigurationError):
        create_app(repository=FakeRepository(), auth_enabled=True, api_key="   ")


def test_enabled_without_key_in_environment_fails_construction(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv(ENV_API_AUTH_ENABLED, "true")
    monkeypatch.delenv(ENV_API_KEY, raising=False)
    with pytest.raises(AuthConfigurationError):
        create_app(repository=FakeRepository())


def test_config_error_message_names_variable_without_secret_value():
    with pytest.raises(AuthConfigurationError) as exc_info:
        load_auth_config(enabled=True, api_key="   ")
    message = str(exc_info.value)
    assert ENV_API_KEY in message
    assert "   " not in message


def test_startup_exits_non_zero_without_exposing_credentials(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
):
    monkeypatch.setenv(ENV_API_AUTH_ENABLED, "true")
    monkeypatch.delenv(ENV_API_KEY, raising=False)
    monkeypatch.setattr("dotenv.load_dotenv", lambda *a, **k: False)
    monkeypatch.setattr("core.persistence.factory.get_repository", lambda *a, **k: FakeRepository())
    monkeypatch.setattr("sys.argv", ["python -m api"])

    def _forbidden_uvicorn(*args: Any, **kwargs: Any):  # pragma: no cover - must not run
        raise AssertionError("uvicorn.run must not be reached when auth config is invalid")

    monkeypatch.setattr("uvicorn.run", _forbidden_uvicorn)

    with pytest.raises(SystemExit) as exit_info:
        api_main.main()

    assert exit_info.value.code == 1
    captured = capsys.readouterr()
    assert "authentication" in captured.err.lower()


# ===========================================================================
# 8. Secret non-leakage
# ===========================================================================

def test_secret_never_appears_in_responses_logs_or_state_dumps(
    incidents_root: Path, caplog: pytest.LogCaptureFixture
):
    caplog.set_level(logging.DEBUG)
    client = _enabled_client(incidents_root)

    unauthorized = client.post("/investigations", json={"incident_id": "x"})
    authorized = client.post(
        "/investigations", json={"incident_id": "does_not_exist"}, headers=_bearer(SECRET_KEY)
    )

    assert SECRET_KEY not in unauthorized.text
    assert SECRET_KEY not in authorized.text
    assert SECRET_KEY not in caplog.text
    # Non-leakage holds regardless of success; the message is generic.
    assert unauthorized.json()["detail"] == "Missing or invalid API credentials."


# ===========================================================================
# 9. Constant-time comparison is actually used
# ===========================================================================

def test_hmac_compare_digest_is_used(incidents_root: Path, monkeypatch: pytest.MonkeyPatch):
    calls = {"count": 0}
    import api.auth as auth_module

    real_compare = auth_module.hmac.compare_digest

    def _spy(a, b):
        calls["count"] += 1
        return real_compare(a, b)

    monkeypatch.setattr(auth_module.hmac, "compare_digest", _spy)

    client = _enabled_client(incidents_root)
    assert client.post("/investigations", json={"x": 1}, headers=_bearer(SECRET_KEY)).status_code != 401
    assert calls["count"] >= 1


# ===========================================================================
# 10. Read protection policy (with /health always public)
# ===========================================================================

def test_read_protection_off_leaves_reads_open(incidents_root: Path):
    client = _enabled_client(incidents_root, protect_reads=False)
    assert client.get("/health").status_code == 200
    assert client.get("/investigations").status_code == 200


def test_read_protection_on_requires_credential(incidents_root: Path):
    client = _enabled_client(incidents_root, protect_reads=True)
    assert client.get("/investigations").status_code == 401
    assert client.get("/investigations/probe").status_code == 401
    assert client.get("/investigations/probe/result").status_code == 401


def test_health_stays_public_under_read_protection(incidents_root: Path):
    client = _enabled_client(incidents_root, protect_reads=True)
    assert client.get("/health").status_code == 200


def test_read_protection_on_accepts_valid_credential(incidents_root: Path):
    client = _enabled_client(incidents_root, protect_reads=True)
    assert client.get("/investigations", headers=_bearer(SECRET_KEY)).status_code == 200


# ===========================================================================
# 11. SSE streaming remains intact
# ===========================================================================

def test_sse_streaming_works_with_valid_credential():
    state = InvestigationState(incident_id="inc_sse")
    state.status = IncidentStatus.COMPLETED
    repo = FakeRepository({"inc_sse": state})
    client = TestClient(
        create_app(
            repository=repo,
            auth_enabled=True,
            api_key=SECRET_KEY,
            auth_protect_reads=True,
        )
    )

    # Without credentials the stream endpoint is rejected.
    assert client.get("/investigations/inc_sse/events", headers={"Accept": "text/event-stream"}).status_code == 401

    # With credentials the streaming response is delivered unmodified.
    resp = client.get(
        "/investigations/inc_sse/events",
        headers={**_bearer(SECRET_KEY), "Accept": "text/event-stream"},
    )
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/event-stream")


def test_middleware_forwards_streaming_messages_unmodified():
    """The pure-ASGI guard must not buffer or alter a streaming response."""
    sent: List[Dict[str, Any]] = []

    async def streaming_app(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"part-1", "more_body": True})
        await send({"type": "http.response.body", "body": b"part-2", "more_body": False})

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        sent.append(message)

    middleware = ApiKeyAuthMiddleware(
        streaming_app, AuthConfig(enabled=True, api_key=SECRET_KEY, protect_reads=True)
    )
    scope = {
        "type": "http",
        "method": "GET",
        "path": "/investigations/inc_sse/events",
        "headers": [(b"authorization", f"Bearer {SECRET_KEY}".encode())],
    }
    asyncio.run(middleware(scope, receive, send))

    body_messages = [m for m in sent if m["type"] == "http.response.body"]
    assert len(body_messages) == 2  # not merged/buffered
    assert body_messages[0]["body"] == b"part-1"
    assert body_messages[1]["body"] == b"part-2"


# ===========================================================================
# 12. Route-coverage regression (fail-closed by default)
# ===========================================================================

def test_every_non_safe_method_is_protected_by_default(incidents_root: Path):
    client = _enabled_client(incidents_root)
    checked = 0
    for route in client.app.routes:  # type: ignore[attr-defined]
        methods = getattr(route, "methods", None) or set()
        path = getattr(route, "path", None)
        if not path:
            continue
        for method in methods:
            if method in ("GET", "HEAD", "OPTIONS"):
                continue
            materialised = _PATH_PARAM_RE.sub("probe", path)
            resp = client.request(method, materialised)
            assert resp.status_code == 401, f"{method} {path} was not protected"
            checked += 1
    assert checked >= 4  # /incidents, /investigations, .../resume, .../approval


# ===========================================================================
# 13. Rejection occurs before the request body is consumed
# ===========================================================================

def test_rejection_happens_before_body_is_consumed():
    seen = {"downstream": False, "receive_calls": 0}
    sent: List[Dict[str, Any]] = []

    async def downstream(scope, receive, send):  # pragma: no cover - must not run
        seen["downstream"] = True

    async def receive():
        seen["receive_calls"] += 1
        return {"type": "http.request", "body": b"should-not-be-read", "more_body": False}

    async def send(message):
        sent.append(message)

    middleware = ApiKeyAuthMiddleware(
        downstream, AuthConfig(enabled=True, api_key=SECRET_KEY, protect_reads=False)
    )
    scope = {"type": "http", "method": "POST", "path": "/investigations", "headers": []}
    asyncio.run(middleware(scope, receive, send))

    assert seen["downstream"] is False
    assert seen["receive_calls"] == 0
    assert sent[0]["type"] == "http.response.start"
    assert sent[0]["status"] == 401


# ===========================================================================
# 14. Hardening: no credential oracle, duplicate headers, config warnings
# ===========================================================================

def test_missing_and_incorrect_credentials_are_indistinguishable(incidents_root: Path):
    """A caller must not be able to tell a missing credential from a wrong one."""
    client = _enabled_client(incidents_root)
    payload = {"incident_id": "x"}

    missing = client.post("/investigations", json=payload)
    incorrect = client.post("/investigations", json=payload, headers=_bearer("wrong-key"))

    assert missing.status_code == incorrect.status_code == 401
    assert missing.json() == incorrect.json()
    assert (
        missing.headers.get("www-authenticate")
        == incorrect.headers.get("www-authenticate")
        == "Bearer"
    )


@pytest.mark.parametrize(
    "headers",
    [
        # valid first / invalid second — must NOT accept the first value
        [(b"authorization", b"Bearer " + SECRET_KEY.encode()), (b"authorization", b"Bearer wrong")],
        # invalid first / valid second — must NOT accept the second either
        [(b"authorization", b"Bearer wrong"), (b"authorization", b"Bearer " + SECRET_KEY.encode())],
        # two identical valid values are still ambiguous
        [(b"authorization", b"Bearer " + SECRET_KEY.encode()), (b"authorization", b"Bearer " + SECRET_KEY.encode())],
    ],
)
def test_duplicate_authorization_headers_are_rejected_at_asgi_layer(headers):
    sent = _run_middleware(SECRET_KEY, headers)
    assert sent[0]["type"] == "http.response.start"
    assert sent[0]["status"] == 401


def test_duplicate_authorization_headers_are_rejected_over_http(incidents_root: Path):
    # Starlette's TestClient (httpx) forwards repeated headers as separate
    # entries, so this exercises the real duplicate-header path.
    client = _enabled_client(incidents_root)
    resp = client.post(
        "/investigations",
        json={"incident_id": "x"},
        headers=[("Authorization", f"Bearer {SECRET_KEY}"), ("Authorization", "Bearer wrong")],
    )
    assert resp.status_code == 401


def test_single_authorization_header_is_still_accepted(incidents_root: Path):
    # Guard against the duplicate-header check over-reaching.
    client = _enabled_client(incidents_root)
    resp = client.post("/investigations", json={"incident_id": "x"}, headers=_bearer(SECRET_KEY))
    assert resp.status_code != 401


@pytest.mark.parametrize("value", ["1", "true", "yes", "on", "TRUE", "  Yes  ", "On"])
def test_truthy_enable_values_enable_auth(monkeypatch: pytest.MonkeyPatch, value: str):
    monkeypatch.setenv(ENV_API_AUTH_ENABLED, value)
    monkeypatch.setenv(ENV_API_KEY, SECRET_KEY)
    assert load_auth_config().enabled is True


@pytest.mark.parametrize("value", ["0", "false", "no", "off", "FALSE", " Off "])
def test_recognised_falsey_values_disable_without_warning(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, value: str
):
    monkeypatch.setenv(ENV_API_AUTH_ENABLED, value)
    monkeypatch.delenv(ENV_API_KEY, raising=False)
    with caplog.at_level(logging.WARNING):
        config = load_auth_config()
    assert config.enabled is False
    assert ENV_API_AUTH_ENABLED not in caplog.text


@pytest.mark.parametrize("value", ["", "   "])
def test_empty_enable_value_defaults_off_without_warning(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, value: str
):
    monkeypatch.setenv(ENV_API_AUTH_ENABLED, value)
    monkeypatch.delenv(ENV_API_KEY, raising=False)
    with caplog.at_level(logging.WARNING):
        config = load_auth_config()
    assert config.enabled is False
    assert ENV_API_AUTH_ENABLED not in caplog.text


def test_absent_enable_flag_defaults_off_without_warning(caplog: pytest.LogCaptureFixture):
    with caplog.at_level(logging.WARNING):
        config = load_auth_config()
    assert config.enabled is False
    assert ENV_API_AUTH_ENABLED not in caplog.text


def test_unrecognised_enable_value_warns_and_stays_disabled(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
):
    bogus = "definitely-not-a-boolean"
    monkeypatch.setenv(ENV_API_AUTH_ENABLED, bogus)
    monkeypatch.delenv(ENV_API_KEY, raising=False)
    with caplog.at_level(logging.WARNING):
        config = load_auth_config()

    # Fail safe (disabled) rather than enabling on garbage...
    assert config.enabled is False
    # ...but never silently: a sanitised warning names the variable.
    assert ENV_API_AUTH_ENABLED in caplog.text
    # The unrecognised value itself is not echoed.
    assert bogus not in caplog.text


def test_truthy_enable_value_does_not_warn(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
):
    monkeypatch.setenv(ENV_API_AUTH_ENABLED, "yes")
    monkeypatch.setenv(ENV_API_KEY, SECRET_KEY)
    with caplog.at_level(logging.WARNING):
        config = load_auth_config()
    assert config.enabled is True
    assert ENV_API_AUTH_ENABLED not in caplog.text


def test_safe_methods_are_not_protected_by_default(incidents_root: Path):
    client = _enabled_client(incidents_root, protect_reads=False)
    # HEAD/OPTIONS are safe methods, so the guard does not reject them even
    # though the routes themselves may not support them (405) / are public.
    assert client.head("/investigations").status_code != 401
    assert client.options("/investigations").status_code != 401
    assert client.head("/health").status_code == 200


def test_safe_methods_are_protected_under_read_protection(incidents_root: Path):
    client = _enabled_client(incidents_root, protect_reads=True)
    assert client.head("/investigations").status_code == 401
    assert client.options("/investigations").status_code == 401
    # /health stays public even for HEAD under read protection.
    assert client.head("/health").status_code == 200


def test_non_lowercase_header_name_is_treated_as_no_credential():
    # ASGI servers lowercase header names; a server that does not is treated as
    # "no credential" and fails closed rather than being trusted.
    sent = _run_middleware(
        SECRET_KEY, [(b"Authorization", b"Bearer " + SECRET_KEY.encode())]
    )
    assert sent[0]["status"] == 401


def test_app_state_auth_config_does_not_retain_the_secret():
    app = create_app(repository=FakeRepository(), auth_enabled=True, api_key=SECRET_KEY)
    assert app.state.auth_config.enabled is True
    assert app.state.auth_config.protect_reads is False
    assert app.state.auth_config.api_key is None
    assert SECRET_KEY not in repr(app.state.auth_config)
