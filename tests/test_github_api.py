"""API tests for the read-only GitHub integration (Mission 27.3.2).

Two clearly separated groups:

* **Mocked unit tests** — every GitHub network request is served by an
  ``httpx.MockTransport`` handler, so no real GitHub account or token is ever
  used. These run unconditionally.
* **Live PostgreSQL tests** — the API is wired to a real ``PostgresIntegrationStore``
  (``TestPostgresGithubApiLive``). They are skipped when PostgreSQL is
  unavailable.

Nothing here depends on a real GitHub credential: the token is a well-formed
placeholder supplied only to the mocked client.
"""

from __future__ import annotations

import os
from typing import Any, Callable, Dict, List, Optional

import httpx
import psycopg
import pytest
from starlette.testclient import TestClient

from api.app import create_app
from api.models import _SAFE_ID_REGEX
from api.routes import _build_connection_id
from core.domain.state import InvestigationState
from core.integrations.filesystem_store import FilesystemIntegrationStore
from core.integrations.github_client import ENV_GITHUB_TOKEN, GitHubClient
from core.integrations.postgres_store import PostgresIntegrationStore
from core.integrations.repository_store import validate_connection_id
from core.persistence import PersistenceRepository

#: A distinctive, well-formed classic PAT-style token used by the mocked client.
TOKEN = "ghp_" + "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q7R8"

#: Static API key used by the authentication-convention tests.
SECRET_KEY = "GITHUB_API_SECRET_KEY_123"

Handler = Callable[[httpx.Request], httpx.Response]

PG_URL = os.getenv(
    "SENTINEL_DATABASE_URL",
    "postgresql://sentinel:sentinel_dev_password@localhost:5432/sentinel_db",
)


def _is_postgres_available(url: str) -> bool:
    try:
        with psycopg.connect(url, connect_timeout=1) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1;")
                return cur.fetchone() == (1,)
    except Exception:
        return False


POSTGRES_AVAILABLE = _is_postgres_available(PG_URL)

CONNECTIONS = "/integrations/github/connections"

#: The connection ID the API derives for octo/demo. Derived through the same
#: helper the route uses so the tests never hard-code the digest format.
OCTO_DEMO_ID = _build_connection_id("octo", "demo")


# ============================================================================
# Helpers & fixtures
# ============================================================================

class FakeRepository(PersistenceRepository):
    """Minimal in-memory repository so create_app() has a usable backend."""

    def __init__(self) -> None:
        self._states: Dict[str, InvestigationState] = {}

    def save(self, state: InvestigationState, events: Optional[List[Any]] = None) -> None:
        self._states[state.incident_id] = state

    def load(self, investigation_id: str) -> Optional[InvestigationState]:
        return self._states.get(investigation_id)

    def exists(self, investigation_id: str) -> bool:
        return investigation_id in self._states

    def delete(self, investigation_id: str) -> None:
        self._states.pop(investigation_id, None)

    def list(self) -> List[str]:
        return sorted(self._states.keys())


def _repo_payload(**overrides: Any) -> Dict[str, Any]:
    payload = {
        "name": "demo",
        "full_name": "octo/demo",
        "private": True,
        "visibility": "private",
        "default_branch": "main",
        "description": "Demo repository",
        "html_url": "https://github.com/octo/demo",
        "owner": {"login": "octo"},
    }
    payload.update(overrides)
    return payload


def _branches_payload() -> List[Dict[str, Any]]:
    return [
        {"name": "main", "protected": True, "commit": {"sha": "abc123"}},
        {"name": "dev", "protected": False, "commit": {"sha": "def456"}},
    ]


def _commit_payload(**overrides: Any) -> Dict[str, Any]:
    payload = {
        "sha": "abc123",
        "html_url": "https://github.com/octo/demo/commit/abc123",
        "commit": {
            "message": "Initial commit",
            "author": {"name": "Octo Cat", "date": "2024-01-01T00:00:00Z"},
        },
        "author": {"login": "octo"},
    }
    payload.update(overrides)
    return payload


def _pulls_payload() -> List[Dict[str, Any]]:
    return [
        {
            "number": 7,
            "title": "Fix the thing",
            "state": "open",
            "user": {"login": "octo"},
            "head": {"sha": "def456"},
            "base": {"ref": "main"},
            "draft": False,
            "html_url": "https://github.com/octo/demo/pull/7",
            "updated_at": "2024-01-02T00:00:00Z",
        }
    ]


def _handler(mapping: Dict[str, Any]) -> Handler:
    """Build a MockTransport handler from a {path: response|callable} table."""

    def handler(request: httpx.Request) -> httpx.Response:
        entry = mapping.get(request.url.path)
        if entry is None:
            return httpx.Response(404, json={"message": "Not Found"})
        return entry(request) if callable(entry) else entry

    return handler


def _ok_mapping() -> Dict[str, Any]:
    """A fully working read-only repository."""
    return {
        "/repos/octo/demo": httpx.Response(200, json=_repo_payload()),
        "/repos/octo/demo/branches": httpx.Response(200, json=_branches_payload()),
        "/repos/octo/demo/commits/main": httpx.Response(200, json=_commit_payload()),
        "/repos/octo/demo/pulls": httpx.Response(200, json=_pulls_payload()),
    }


def _generic_handler() -> Handler:
    """A handler that answers any well-formed read-only repository path."""

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/branches"):
            return httpx.Response(200, json=_branches_payload())
        if "/commits/" in path:
            return httpx.Response(200, json=_commit_payload())
        if path.endswith("/pulls"):
            return httpx.Response(200, json=_pulls_payload())
        parts = path.strip("/").split("/")
        if len(parts) == 3 and parts[0] == "repos":
            owner, repo = parts[1], parts[2]
            return httpx.Response(
                200,
                json=_repo_payload(
                    name=repo, owner={"login": owner}, full_name=f"{owner}/{repo}"
                ),
            )
        return httpx.Response(404, json={"message": "Not Found"})

    return handler


@pytest.fixture(autouse=True)
def _no_inherited_token(monkeypatch: pytest.MonkeyPatch):
    """Never let an ambient token leak into the mocked tests."""
    monkeypatch.delenv(ENV_GITHUB_TOKEN, raising=False)


@pytest.fixture
def build_client(tmp_path):
    """Factory that builds a TestClient wired to an isolated integration store.

    All clients built by one test share a single filesystem persistence root, so
    a connection created through one app instance is visible to the next.
    """
    created_clients: List[GitHubClient] = []

    def _build(
        handler: Handler,
        *,
        store: Optional[Any] = None,
        client: Optional[GitHubClient] = None,
        token: Any = TOKEN,
        **app_kwargs: Any,
    ) -> TestClient:
        if client is None:
            client = GitHubClient(token=token, transport=httpx.MockTransport(handler))
        created_clients.append(client)
        if store is None:
            store = FilesystemIntegrationStore(tmp_path / "persistence")
        app = create_app(
            repository=FakeRepository(),
            integration_store=store,
            github_client=client,
            **app_kwargs,
        )
        return TestClient(app)

    yield _build

    for client in created_clients:
        client.close()


def _connect(client: TestClient, owner: str = "octo", repo: str = "demo", **extra: Any) -> Any:
    return client.post(CONNECTIONS, json={"owner": owner, "repo": repo, **extra})


def _connect_and_verify(client: TestClient) -> str:
    """Connect octo/demo, verify it successfully, and return last_verified_at."""
    assert _connect(client).status_code == 201
    resp = client.post(f"{CONNECTIONS}/{OCTO_DEMO_ID}/verify")
    assert resp.status_code == 200
    stamp = resp.json()["connection"]["last_verified_at"]
    assert stamp
    return stamp


# ============================================================================
# 1. Connect repository
# ============================================================================

class TestConnectRepository:
    def test_connect_success(self, build_client) -> None:
        client = build_client(_handler(_ok_mapping()))
        resp = _connect(client)

        assert resp.status_code == 201
        body = resp.json()
        assert body["connection_id"] == OCTO_DEMO_ID
        assert body["owner"] == "octo"
        assert body["repo"] == "demo"
        assert body["provider"] == "github"
        assert body["status"] == "connected"
        assert body["default_branch"] == "main"
        assert body["visibility"] == "private"
        assert resp.headers["location"] == f"{CONNECTIONS}/{OCTO_DEMO_ID}"

    def test_connect_accepts_client_default_branch_override(self, build_client) -> None:
        """A caller-supplied default_branch overrides the repository default."""
        client = build_client(_handler(_ok_mapping()))
        resp = _connect(client, default_branch="develop")
        assert resp.status_code == 201
        assert resp.json()["default_branch"] == "develop"

    def test_connect_repository_not_found(self, build_client) -> None:
        mapping = {"/repos/octo/missing": httpx.Response(404, json={"message": "Not Found"})}
        client = build_client(_handler(mapping))
        resp = _connect(client, repo="missing")

        assert resp.status_code == 404
        assert resp.json()["error"] == "github_not_found"

    def test_connect_repository_inaccessible_permission(self, build_client) -> None:
        mapping = {"/repos/octo/secret": httpx.Response(403, json={"message": "Forbidden"})}
        client = build_client(_handler(mapping))
        resp = _connect(client, repo="secret")

        assert resp.status_code == 403
        assert resp.json()["error"] == "github_permission_denied"

    def test_connect_missing_token_is_not_configured_and_no_request(self, build_client) -> None:
        calls: List[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(request)
            return httpx.Response(200, json=_repo_payload())

        client = build_client(handler, token=None)
        resp = _connect(client)

        assert resp.status_code == 503
        assert resp.json()["error"] == "github_not_configured"
        assert calls == []  # no unauthenticated fallback request

    def test_connect_rejects_client_supplied_token(self, build_client) -> None:
        client = build_client(_handler(_ok_mapping()))
        resp = client.post(
            CONNECTIONS, json={"owner": "octo", "repo": "demo", "token": "ghp_" + "x" * 40}
        )
        assert resp.status_code == 400
        assert resp.json()["error"] == "invalid_request"

    def test_connect_rejects_credential_ref_field(self, build_client) -> None:
        client = build_client(_handler(_ok_mapping()))
        resp = client.post(
            CONNECTIONS,
            json={"owner": "octo", "repo": "demo", "credential_ref": "env:SENTINEL_GITHUB_TOKEN"},
        )
        assert resp.status_code == 400
        assert resp.json()["error"] == "invalid_request"

    @pytest.mark.parametrize(
        "payload",
        [
            {"owner": "octo", "repo": "de mo"},
            {"owner": "oc to", "repo": "demo"},
            {"owner": "", "repo": "demo"},
            {"owner": "octo", "repo": ""},
            {"owner": "octo/evil", "repo": "demo"},
            {"owner": "octo", "repo": "../etc"},
            {"repo": "demo"},
            {"owner": "octo"},
        ],
    )
    def test_connect_invalid_coordinates(self, build_client, payload: Dict[str, Any]) -> None:
        client = build_client(_handler(_ok_mapping()))
        resp = client.post(CONNECTIONS, json=payload)
        assert resp.status_code == 400
        assert resp.json()["error"] == "invalid_request"

    def test_connect_malformed_json(self, build_client) -> None:
        client = build_client(_handler(_ok_mapping()))
        resp = client.post(
            CONNECTIONS, content=b"{not json", headers={"Content-Type": "application/json"}
        )
        assert resp.status_code == 400
        assert resp.json()["error"] == "invalid_json"

    def test_connect_non_object_body(self, build_client) -> None:
        client = build_client(_handler(_ok_mapping()))
        resp = client.post(
            CONNECTIONS, content=b"[1, 2, 3]", headers={"Content-Type": "application/json"}
        )
        assert resp.status_code == 400
        assert resp.json()["error"] == "invalid_json"

    def test_connect_duplicate_same_repository(self, build_client) -> None:
        client = build_client(_handler(_ok_mapping()))
        assert _connect(client).status_code == 201
        resp = _connect(client)
        assert resp.status_code == 409
        assert resp.json()["error"] == "duplicate_connection"

    @pytest.mark.parametrize("owner,repo", [("OCTO", "DEMO"), ("Octo", "Demo"), ("octo", "DEMO")])
    def test_connect_duplicate_case_variants(self, build_client, owner: str, repo: str) -> None:
        mapping = _ok_mapping()
        # Case-variant coordinates resolve to the same canonical repository.
        mapping.update(
            {
                f"/repos/{owner}/{repo}": httpx.Response(200, json=_repo_payload()),
            }
        )
        client = build_client(_handler(mapping))
        assert _connect(client, owner="octo", repo="demo").status_code == 201
        resp = _connect(client, owner=owner, repo=repo)
        assert resp.status_code == 409
        assert resp.json()["error"] == "duplicate_connection"


# ============================================================================
# 2. List & get connections
# ============================================================================

class TestListAndGetConnections:
    def test_list_empty(self, build_client) -> None:
        client = build_client(_handler(_ok_mapping()))
        resp = client.get(CONNECTIONS)
        assert resp.status_code == 200
        assert resp.json() == {"connections": []}

    def test_list_after_connect(self, build_client) -> None:
        client = build_client(_handler(_ok_mapping()))
        _connect(client)
        resp = client.get(CONNECTIONS)
        assert resp.status_code == 200
        connections = resp.json()["connections"]
        assert [c["connection_id"] for c in connections] == [OCTO_DEMO_ID]

    def test_get_by_id(self, build_client) -> None:
        client = build_client(_handler(_ok_mapping()))
        _connect(client)
        resp = client.get(f"{CONNECTIONS}/{OCTO_DEMO_ID}")
        assert resp.status_code == 200
        assert resp.json()["connection_id"] == OCTO_DEMO_ID

    def test_get_unknown_id_returns_not_found(self, build_client) -> None:
        client = build_client(_handler(_ok_mapping()))
        resp = client.get(f"{CONNECTIONS}/gh-nope")
        assert resp.status_code == 404
        assert resp.json()["error"] == "connection_not_found"

    @pytest.mark.parametrize("bad_id", ["-bad", "bad..id", "bad id"])
    def test_get_invalid_id_rejected(self, build_client, bad_id: str) -> None:
        client = build_client(_handler(_ok_mapping()))
        resp = client.get(f"{CONNECTIONS}/{bad_id}")
        assert resp.status_code == 400
        assert resp.json()["error"] == "invalid_connection_id"

    def test_connections_visible_across_app_instances(self, build_client) -> None:
        """A connection persisted by one app instance is visible to another."""
        first = build_client(_handler(_ok_mapping()))
        assert _connect(first).status_code == 201
        second = build_client(_handler(_ok_mapping()))
        resp = second.get(CONNECTIONS)
        assert [c["connection_id"] for c in resp.json()["connections"]] == [OCTO_DEMO_ID]

    def test_github_routes_isolated_when_client_missing(self, tmp_path) -> None:
        """A missing client leaves GitHub routes at 503 without touching others."""
        app = create_app(
            repository=FakeRepository(),
            integration_store=FilesystemIntegrationStore(tmp_path / "persistence"),
            github_client=None,
        )
        with TestClient(app) as bare:
            assert bare.get("/health").status_code == 200
            resp = _connect(bare)
            assert resp.status_code == 503
            assert resp.json()["error"] == "github_integration_unavailable"
            # Reads that need no client still answer normally.
            assert bare.get(CONNECTIONS).status_code == 200


# ============================================================================
# 3. Verify connection
# ============================================================================

class TestVerifyConnection:
    def test_verify_success(self, build_client) -> None:
        client = build_client(_handler(_ok_mapping()))
        _connect(client)
        resp = client.post(f"{CONNECTIONS}/{OCTO_DEMO_ID}/verify")

        assert resp.status_code == 200
        body = resp.json()
        assert body["verified"] is True
        assert body["connection"]["status"] == "connected"
        assert body["verified_at"]
        assert body["connection"]["last_verified_at"]
        assert body["verified_at"] == body["connection"]["last_verified_at"]

    def test_verify_success_refreshes_last_verified_at(self, build_client, tmp_path) -> None:
        store = FilesystemIntegrationStore(tmp_path / "persistence")
        client = build_client(_handler(_ok_mapping()), store=store)
        _connect(client)
        # Force an obviously stale timestamp onto the persisted record.
        store.update(OCTO_DEMO_ID, last_verified_at="2000-01-01T00:00:00+00:00")

        resp = client.post(f"{CONNECTIONS}/{OCTO_DEMO_ID}/verify")

        assert resp.status_code == 200
        refreshed = resp.json()["connection"]["last_verified_at"]
        assert refreshed != "2000-01-01T00:00:00+00:00"
        assert resp.json()["verified_at"] == refreshed
        assert client.get(f"{CONNECTIONS}/{OCTO_DEMO_ID}").json()["last_verified_at"] == refreshed

    # -- transient / inconclusive failures leave the record untouched ---------

    def test_verify_rate_limit_429_leaves_record_unchanged(self, build_client) -> None:
        mapping = _ok_mapping()
        client = build_client(_handler(mapping))
        prior = _connect_and_verify(client)

        mapping["/repos/octo/demo"] = httpx.Response(
            429,
            json={"message": "API rate limit exceeded"},
            headers={"x-ratelimit-remaining": "0", "retry-after": "30"},
        )
        resp = client.post(f"{CONNECTIONS}/{OCTO_DEMO_ID}/verify")

        assert resp.status_code == 429
        assert resp.json()["error"] == "github_rate_limited"
        after = client.get(f"{CONNECTIONS}/{OCTO_DEMO_ID}").json()
        assert after["status"] == "connected"
        assert after["last_verified_at"] == prior

    def test_verify_secondary_rate_limit_403_leaves_record_unchanged(self, build_client) -> None:
        mapping = _ok_mapping()
        client = build_client(_handler(mapping))
        prior = _connect_and_verify(client)

        # A secondary/abuse limit: 403 with a positive remaining count, no
        # Retry-After header, and an explicit explanatory message.
        mapping["/repos/octo/demo"] = httpx.Response(
            403,
            json={
                "message": (
                    "You have exceeded a secondary rate limit. "
                    "Please wait a few minutes before you try again."
                )
            },
            headers={"x-ratelimit-remaining": "42"},
        )
        resp = client.post(f"{CONNECTIONS}/{OCTO_DEMO_ID}/verify")

        assert resp.status_code == 429
        assert resp.json()["error"] == "github_rate_limited"
        after = client.get(f"{CONNECTIONS}/{OCTO_DEMO_ID}").json()
        assert after["status"] == "connected"
        assert after["last_verified_at"] == prior

    def test_verify_server_error_leaves_record_unchanged(self, build_client) -> None:
        mapping = _ok_mapping()
        client = build_client(_handler(mapping))
        prior = _connect_and_verify(client)

        mapping["/repos/octo/demo"] = httpx.Response(500, json={"message": "Server Error"})
        resp = client.post(f"{CONNECTIONS}/{OCTO_DEMO_ID}/verify")

        assert resp.status_code == 503
        assert resp.json()["error"] == "github_unavailable"
        after = client.get(f"{CONNECTIONS}/{OCTO_DEMO_ID}").json()
        assert after["status"] == "connected"
        assert after["last_verified_at"] == prior

    def test_verify_network_failure_leaves_record_unchanged(self, build_client) -> None:
        mapping: Dict[str, Any] = _ok_mapping()
        client = build_client(_handler(mapping))
        prior = _connect_and_verify(client)

        def boom(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("connection refused")

        mapping["/repos/octo/demo"] = boom
        resp = client.post(f"{CONNECTIONS}/{OCTO_DEMO_ID}/verify")

        assert resp.status_code == 503
        assert resp.json()["error"] == "github_unavailable"
        after = client.get(f"{CONNECTIONS}/{OCTO_DEMO_ID}").json()
        assert after["status"] == "connected"
        assert after["last_verified_at"] == prior

    # -- definitive access failures persist error ----------------------------

    def test_verify_revoked_credential_marks_error(self, build_client) -> None:
        mapping = _ok_mapping()
        client = build_client(_handler(mapping))
        assert _connect(client).status_code == 201

        # The credential is revoked after connecting.
        mapping["/repos/octo/demo"] = httpx.Response(401, json={"message": "Bad credentials"})
        resp = client.post(f"{CONNECTIONS}/{OCTO_DEMO_ID}/verify")

        assert resp.status_code == 502
        assert resp.json()["error"] == "github_authentication_failed"
        after = client.get(f"{CONNECTIONS}/{OCTO_DEMO_ID}").json()
        assert after["status"] == "error"

    def test_verify_missing_permission_marks_error(self, build_client) -> None:
        mapping = _ok_mapping()
        client = build_client(_handler(mapping))
        assert _connect(client).status_code == 201
        mapping["/repos/octo/demo"] = httpx.Response(403, json={"message": "Forbidden"})

        resp = client.post(f"{CONNECTIONS}/{OCTO_DEMO_ID}/verify")

        assert resp.status_code == 403
        assert resp.json()["error"] == "github_permission_denied"
        after = client.get(f"{CONNECTIONS}/{OCTO_DEMO_ID}").json()
        assert after["status"] == "error"

    def test_verify_not_found_marks_error(self, build_client) -> None:
        mapping = _ok_mapping()
        client = build_client(_handler(mapping))
        assert _connect(client).status_code == 201
        mapping["/repos/octo/demo"] = httpx.Response(404, json={"message": "Not Found"})

        resp = client.post(f"{CONNECTIONS}/{OCTO_DEMO_ID}/verify")

        assert resp.status_code == 404
        assert resp.json()["error"] == "github_not_found"
        after = client.get(f"{CONNECTIONS}/{OCTO_DEMO_ID}").json()
        assert after["status"] == "error"

    def test_verify_unknown_connection(self, build_client) -> None:
        client = build_client(_handler(_ok_mapping()))
        resp = client.post(f"{CONNECTIONS}/gh-missing/verify")
        assert resp.status_code == 404
        assert resp.json()["error"] == "connection_not_found"


# ============================================================================
# 4. Repository information
# ============================================================================

class TestRepositoryInformation:
    def test_list_branches(self, build_client) -> None:
        client = build_client(_handler(_ok_mapping()))
        _connect(client)
        resp = client.get(f"{CONNECTIONS}/{OCTO_DEMO_ID}/branches")

        assert resp.status_code == 200
        body = resp.json()
        assert body["owner"] == "octo"
        assert body["repo"] == "demo"
        assert [b["name"] for b in body["branches"]] == ["main", "dev"]
        assert body["branches"][0]["protected"] is True
        assert body["branches"][0]["commit_sha"] == "abc123"

    def test_get_commit_via_path_segment(self, build_client) -> None:
        """The original single-segment endpoint keeps working."""
        client = build_client(_handler(_ok_mapping()))
        _connect(client)
        resp = client.get(f"{CONNECTIONS}/{OCTO_DEMO_ID}/commits/main")

        assert resp.status_code == 200
        body = resp.json()
        assert body["sha"] == "abc123"
        assert body["message"] == "Initial commit"
        assert body["author_login"] == "octo"
        assert body["author_name"] == "Octo Cat"

    def _commit_recording_client(self, build_client):
        """A client that records the raw GitHub commit path it was asked for."""
        seen: List[bytes] = []

        def handler(request: httpx.Request) -> httpx.Response:
            path = request.url.path
            if path == "/repos/octo/demo":
                return httpx.Response(200, json=_repo_payload())
            if path.startswith("/repos/octo/demo/commits/"):
                seen.append(request.url.raw_path)
                return httpx.Response(200, json=_commit_payload())
            return httpx.Response(404, json={"message": "Not Found"})

        client = build_client(handler)
        assert _connect(client).status_code == 201
        return client, seen

    def test_get_commit_via_query_ref_with_slash(self, build_client) -> None:
        """A slash-containing ref is reachable through ?ref=."""
        client, seen = self._commit_recording_client(build_client)

        resp = client.get(f"{CONNECTIONS}/{OCTO_DEMO_ID}/commits?ref=feature/add-auth")

        assert resp.status_code == 200
        assert resp.json()["sha"] == "abc123"
        # The complete ref is passed to the client unchanged and percent-encoded
        # by the client for the request (single encoding of the slash).
        assert seen == [b"/repos/octo/demo/commits/feature%2Fadd-auth"]

    def test_get_commit_via_encoded_query_ref(self, build_client) -> None:
        """A pre-encoded query value resolves to the same ref."""
        client, seen = self._commit_recording_client(build_client)

        resp = client.get(f"{CONNECTIONS}/{OCTO_DEMO_ID}/commits?ref=feature%2Fadd-auth")

        assert resp.status_code == 200
        assert seen == [b"/repos/octo/demo/commits/feature%2Fadd-auth"]

    def test_query_ref_is_preferred_over_path_segment(self, build_client) -> None:
        client, seen = self._commit_recording_client(build_client)

        resp = client.get(f"{CONNECTIONS}/{OCTO_DEMO_ID}/commits/main?ref=feature/add-auth")

        assert resp.status_code == 200
        assert seen == [b"/repos/octo/demo/commits/feature%2Fadd-auth"]

    def test_encoded_slash_in_path_segment_is_not_routed(self, build_client) -> None:
        """Documents the routing limitation the query form exists to work around.

        Starlette matches on the decoded ASGI path, so ``%2F`` inside a single
        path segment is decoded to ``/`` before matching and cannot reach the
        ``{ref}`` handler. The query form is the supported mechanism.
        """
        client = build_client(_handler(_ok_mapping()))
        _connect(client)
        resp = client.get(f"{CONNECTIONS}/{OCTO_DEMO_ID}/commits/feature%2Fadd-auth")
        assert resp.status_code == 404

    def test_get_commit_missing_ref_returns_400(self, build_client) -> None:
        client = build_client(_handler(_ok_mapping()))
        _connect(client)
        resp = client.get(f"{CONNECTIONS}/{OCTO_DEMO_ID}/commits")
        assert resp.status_code == 400
        assert resp.json()["error"] == "invalid_request"

    def test_get_commit_empty_query_ref_returns_400(self, build_client) -> None:
        client = build_client(_handler(_ok_mapping()))
        _connect(client)
        resp = client.get(f"{CONNECTIONS}/{OCTO_DEMO_ID}/commits?ref=")
        assert resp.status_code == 400
        assert resp.json()["error"] == "invalid_request"

    @pytest.mark.parametrize("bad_ref", ["bad ref", "a..b", "trailing/", "/leading", "with~tilde"])
    def test_get_commit_invalid_query_ref_returns_400(self, build_client, bad_ref: str) -> None:
        client = build_client(_handler(_ok_mapping()))
        _connect(client)
        resp = client.get(
            f"{CONNECTIONS}/{OCTO_DEMO_ID}/commits", params={"ref": bad_ref}
        )
        assert resp.status_code == 400
        assert resp.json()["error"] == "invalid_request"

    def test_get_commit_invalid_path_ref_returns_400(self, build_client) -> None:
        client = build_client(_handler(_ok_mapping()))
        _connect(client)
        resp = client.get(f"{CONNECTIONS}/{OCTO_DEMO_ID}/commits/bad%20ref")
        assert resp.status_code == 400
        assert resp.json()["error"] == "invalid_request"

    def test_list_pull_requests(self, build_client) -> None:
        client = build_client(_handler(_ok_mapping()))
        _connect(client)
        resp = client.get(f"{CONNECTIONS}/{OCTO_DEMO_ID}/pull-requests")

        assert resp.status_code == 200
        body = resp.json()
        assert body["pull_requests"][0]["number"] == 7
        assert body["pull_requests"][0]["state"] == "open"
        assert body["pull_requests"][0]["base_branch"] == "main"

    def test_list_pull_requests_invalid_state(self, build_client) -> None:
        client = build_client(_handler(_ok_mapping()))
        _connect(client)
        resp = client.get(f"{CONNECTIONS}/{OCTO_DEMO_ID}/pull-requests?state=merged")
        assert resp.status_code == 400
        assert resp.json()["error"] == "invalid_request"

    @pytest.mark.parametrize(
        "query",
        ["per_page=0", "per_page=101", "per_page=abc", "max_items=0", "max_items=1001"],
    )
    def test_pagination_bounds(self, build_client, query: str) -> None:
        client = build_client(_handler(_ok_mapping()))
        _connect(client)
        resp = client.get(f"{CONNECTIONS}/{OCTO_DEMO_ID}/branches?{query}")
        assert resp.status_code == 400
        assert resp.json()["error"] == "invalid_request"

    def test_rate_limit_maps_to_429_with_retry_after(self, build_client) -> None:
        mapping: Dict[str, Any] = _ok_mapping()
        client = build_client(_handler(mapping))
        _connect(client)

        mapping["/repos/octo/demo/branches"] = httpx.Response(
            429,
            json={"message": "API rate limit exceeded"},
            headers={"x-ratelimit-remaining": "0", "retry-after": "42"},
        )
        resp = client.get(f"{CONNECTIONS}/{OCTO_DEMO_ID}/branches")
        assert resp.status_code == 429
        assert resp.json()["error"] == "github_rate_limited"
        assert resp.headers["retry-after"] == "42"

    def test_repository_information_unknown_connection(self, build_client) -> None:
        client = build_client(_handler(_ok_mapping()))
        assert client.get(f"{CONNECTIONS}/gh-nope/branches").status_code == 404
        assert client.get(f"{CONNECTIONS}/gh-nope/commits/main").status_code == 404
        assert client.get(f"{CONNECTIONS}/gh-nope/commits?ref=main").status_code == 404
        assert client.get(f"{CONNECTIONS}/gh-nope/pull-requests").status_code == 404


# ============================================================================
# 5. Disconnect
# ============================================================================

class TestDisconnect:
    def test_disconnect_success(self, build_client) -> None:
        client = build_client(_handler(_ok_mapping()))
        _connect(client)
        resp = client.delete(f"{CONNECTIONS}/{OCTO_DEMO_ID}")

        assert resp.status_code == 200
        assert resp.json() == {"connection_id": OCTO_DEMO_ID, "deleted": True}
        assert client.get(f"{CONNECTIONS}/{OCTO_DEMO_ID}").status_code == 404

    def test_disconnect_is_idempotent(self, build_client) -> None:
        client = build_client(_handler(_ok_mapping()))
        _connect(client)
        assert client.delete(f"{CONNECTIONS}/{OCTO_DEMO_ID}").json()["deleted"] is True
        second = client.delete(f"{CONNECTIONS}/{OCTO_DEMO_ID}")
        assert second.status_code == 200
        assert second.json()["deleted"] is False

    def test_disconnect_missing_id_is_success(self, build_client) -> None:
        client = build_client(_handler(_ok_mapping()))
        resp = client.delete(f"{CONNECTIONS}/gh-never-existed")
        assert resp.status_code == 200
        assert resp.json()["deleted"] is False


# ============================================================================
# 6. Authentication conventions
# ============================================================================

class TestAuthenticationConventions:
    def test_mutating_routes_require_key_when_enabled(self, build_client) -> None:
        client = build_client(
            _handler(_ok_mapping()), auth_enabled=True, api_key=SECRET_KEY
        )
        assert client.post(CONNECTIONS, json={"owner": "octo", "repo": "demo"}).status_code == 401
        assert client.post(f"{CONNECTIONS}/{OCTO_DEMO_ID}/verify").status_code == 401
        assert client.delete(f"{CONNECTIONS}/{OCTO_DEMO_ID}").status_code == 401

    def test_reads_stay_open_by_default(self, build_client) -> None:
        client = build_client(
            _handler(_ok_mapping()), auth_enabled=True, api_key=SECRET_KEY
        )
        assert client.get(CONNECTIONS).status_code == 200

    def test_valid_key_passes_through(self, build_client) -> None:
        client = build_client(
            _handler(_ok_mapping()), auth_enabled=True, api_key=SECRET_KEY
        )
        resp = client.post(
            CONNECTIONS,
            json={"owner": "octo", "repo": "demo"},
            headers={"Authorization": f"Bearer {SECRET_KEY}"},
        )
        assert resp.status_code == 201

    def test_read_protection_requires_key_on_reads(self, build_client) -> None:
        client = build_client(
            _handler(_ok_mapping()),
            auth_enabled=True,
            api_key=SECRET_KEY,
            auth_protect_reads=True,
        )
        assert client.get(CONNECTIONS).status_code == 401
        assert (
            client.get(CONNECTIONS, headers={"Authorization": f"Bearer {SECRET_KEY}"}).status_code
            == 200
        )


# ============================================================================
# 7. Credential non-exposure
# ============================================================================

def _assert_credential_free(payload: Any) -> None:
    """Recursively assert no credential-bearing key or value is present."""
    forbidden_keys = {"credential_ref", "token", "access_token", "authorization"}
    if isinstance(payload, dict):
        for key, value in payload.items():
            assert key.lower() not in forbidden_keys, f"leaked key: {key}"
            _assert_credential_free(value)
    elif isinstance(payload, list):
        for item in payload:
            _assert_credential_free(item)
    elif isinstance(payload, str):
        assert TOKEN not in payload
        assert "ghp_" not in payload


class TestCredentialNonExposure:
    def test_connect_and_reads_never_expose_credentials(self, build_client) -> None:
        client = build_client(_handler(_ok_mapping()))
        created = _connect(client)
        _assert_credential_free(created.json())
        _assert_credential_free(client.get(CONNECTIONS).json())
        _assert_credential_free(client.get(f"{CONNECTIONS}/{OCTO_DEMO_ID}").json())

    def test_verify_response_never_exposes_credentials(self, build_client) -> None:
        client = build_client(_handler(_ok_mapping()))
        _connect(client)
        _assert_credential_free(client.post(f"{CONNECTIONS}/{OCTO_DEMO_ID}/verify").json())

    def test_error_responses_never_echo_the_token(self, build_client) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(401, json={"message": f"bad {TOKEN}"})

        # The client redacts token-shaped values from its own error messages.
        client = build_client(handler)
        resp = _connect(client)
        assert resp.status_code == 502
        assert TOKEN not in resp.text
        _assert_credential_free(resp.json())

    def test_stored_record_file_has_no_token(self, build_client, tmp_path) -> None:
        client = build_client(_handler(_ok_mapping()))
        _connect(client)
        record = (
            tmp_path / "persistence" / "integrations" / f"{OCTO_DEMO_ID}.json"
        ).read_text(encoding="utf-8")
        assert TOKEN not in record
        assert "ghp_" not in record
        assert "env:SENTINEL_GITHUB_TOKEN" in record  # non-secret reference only


# ============================================================================
# 8. Connection ID generation
# ============================================================================

class TestConnectionIdGeneration:
    def test_case_variants_produce_the_same_id(self) -> None:
        assert _build_connection_id("octo", "demo") == _build_connection_id("OCTO", "DEMO")
        assert _build_connection_id("Octo", "Demo") == _build_connection_id("octo", "demo")

    def test_distinct_pairs_that_previously_collided_now_differ(self) -> None:
        # With a plain hyphen join these both collapsed to "gh-a-b-c".
        assert _build_connection_id("a-b", "c") != _build_connection_id("a", "b-c")

    @pytest.mark.parametrize(
        "owner,repo",
        [
            ("octo", "demo"),
            ("a-b", "c"),
            ("a", "b-c"),
            ("Octo", "Demo"),
            ("o" * 100, "r" * 100),
            ("owner.with.dots", "repo_name"),
        ],
    )
    def test_generated_ids_pass_existing_validation(self, owner: str, repo: str) -> None:
        connection_id = _build_connection_id(owner, repo)
        assert _SAFE_ID_REGEX.match(connection_id)
        assert validate_connection_id(connection_id) == connection_id

    def test_generated_id_stays_within_the_length_limit(self) -> None:
        connection_id = _build_connection_id("o" * 100, "r" * 100)
        assert len(connection_id) <= 128
        assert _SAFE_ID_REGEX.match(connection_id)


# ============================================================================
# 9. Application lifespan ownership
# ============================================================================

class _SpyGitHubClient(GitHubClient):
    """GitHubClient that records whether it was closed."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.closed = False

    def close(self) -> None:
        self.closed = True
        super().close()


class _SpyStore(FilesystemIntegrationStore):
    """Filesystem store that records whether it was closed."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.closed = False

    def close(self) -> None:
        self.closed = True
        super().close()


class TestLifespanOwnership:
    def test_injected_client_and_store_are_closed_on_shutdown(self, tmp_path) -> None:
        store = _SpyStore(tmp_path / "persistence")
        gh_client = _SpyGitHubClient(
            token=TOKEN, transport=httpx.MockTransport(_handler(_ok_mapping()))
        )
        app = create_app(
            repository=FakeRepository(),
            integration_store=store,
            github_client=gh_client,
        )

        with TestClient(app):
            assert store.closed is False
            assert gh_client.closed is False

        assert store.closed is True
        assert gh_client.closed is True


# ============================================================================
# 10. Live PostgreSQL integration store
# ============================================================================

@pytest.mark.skipif(not POSTGRES_AVAILABLE, reason="PostgreSQL is not available at " + PG_URL)
class TestPostgresGithubApiLive:
    """The API driven through a real PostgresIntegrationStore."""

    @pytest.fixture(scope="class", autouse=True)
    def setup_schema(self):
        with PostgresIntegrationStore(PG_URL) as store:
            store.initialize_schema()
            store.verify_schema(raise_on_error=True)

    @pytest.fixture
    def store(self):
        s = PostgresIntegrationStore(PG_URL)
        yield s
        try:
            with s.pool.connection() as conn:
                with conn.transaction():
                    with conn.cursor() as cur:
                        cur.execute(
                            "DELETE FROM github_integrations "
                            "WHERE connection_id LIKE 'gh-apitest-%';"
                        )
        except Exception:
            pass
        s.close()

    def _client(self, store: PostgresIntegrationStore) -> TestClient:
        gh_client = GitHubClient(
            token=TOKEN, transport=httpx.MockTransport(_generic_handler())
        )
        app = create_app(
            repository=FakeRepository(),
            integration_store=store,
            github_client=gh_client,
        )
        return TestClient(app)

    def test_connect_list_get_disconnect_roundtrip(self, store: PostgresIntegrationStore) -> None:
        client = self._client(store)
        resp = _connect(client, owner="apitest", repo="alpha")
        assert resp.status_code == 201
        connection_id = resp.json()["connection_id"]

        assert [c["connection_id"] for c in client.get(CONNECTIONS).json()["connections"]] == [
            connection_id
        ]
        assert client.get(f"{CONNECTIONS}/{connection_id}").status_code == 200
        assert client.delete(f"{CONNECTIONS}/{connection_id}").json()["deleted"] is True
        assert client.get(f"{CONNECTIONS}/{connection_id}").status_code == 404

    def test_duplicate_case_variant_rejected(self, store: PostgresIntegrationStore) -> None:
        client = self._client(store)
        assert _connect(client, owner="apitest", repo="beta").status_code == 201
        resp = _connect(client, owner="APITEST", repo="BETA")
        assert resp.status_code == 409
        assert resp.json()["error"] == "duplicate_connection"

    def test_verify_and_repository_information(self, store: PostgresIntegrationStore) -> None:
        client = self._client(store)
        connection_id = _connect(client, owner="apitest", repo="gamma").json()["connection_id"]

        verify = client.post(f"{CONNECTIONS}/{connection_id}/verify")
        assert verify.status_code == 200
        assert verify.json()["verified"] is True
        assert client.get(f"{CONNECTIONS}/{connection_id}/branches").status_code == 200
        assert client.get(f"{CONNECTIONS}/{connection_id}/pull-requests").status_code == 200
