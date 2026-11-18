"""Unit tests for the read-only GitHub REST API client (Mission 27.2).

These tests never touch the network: every request is served by an
``httpx.MockTransport`` handler, and no real GitHub token is required.

Coverage:
* Repository validation, branch/commit/file/PR retrieval.
* Missing credential, invalid credential, permission, not-found, rate limit
  (403 + 429), server error and network failures.
* Response decoding (base64), unsupported encoding, oversized files, oversized
  responses, directory paths.
* Input validation for owner/repo/ref/path.
* Origin restriction: scheme (HTTPS only), host and port; redirect rejection,
  no unauthenticated fallback, and rejection of non-GET methods.
* Rate limiting: primary (403/429) and secondary (403 with a positive
  remaining count and no Retry-After) limits.
* Malformed numeric headers never raise (treated as unavailable metadata).
* Token redaction from logs and raised errors.
"""

from __future__ import annotations

import base64
import logging
from typing import Any, Callable, List

import httpx
import pytest

from core.integrations.github_client import (
    DEFAULT_MAX_PAGES,
    ENV_GITHUB_TOKEN,
    GITHUB_API_BASE_URL,
    MAX_PER_PAGE,
    GitHubAuthenticationError,
    GitHubBranch,
    GitHubClient,
    GitHubCommit,
    GitHubConfigurationError,
    GitHubError,
    GitHubFileContent,
    GitHubInvalidResponseError,
    GitHubNotFoundError,
    GitHubPermissionError,
    GitHubPullRequest,
    GitHubRateLimitError,
    GitHubRepository,
    GitHubUnavailableError,
    GitHubValidationError,
    redact_secret,
)

#: A distinctive, well-formed classic PAT-style token used throughout.
TOKEN = "ghp_" + "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q7R8"

Handler = Callable[[httpx.Request], httpx.Response]


def _client(handler: Handler, *, token: Any = TOKEN, **kwargs: Any) -> GitHubClient:
    """Build a client backed by a MockTransport handler."""
    return GitHubClient(token=token, transport=httpx.MockTransport(handler), **kwargs)


def _repo_payload(**overrides: Any) -> dict:
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


# ============================================================================
# Repository validation
# ============================================================================

def test_validate_repository_returns_metadata_and_sends_header_only():
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("authorization")
        return httpx.Response(200, json=_repo_payload())

    repo = _client(handler).validate_repository("octo", "demo")

    assert isinstance(repo, GitHubRepository)
    assert (repo.owner, repo.name) == ("octo", "demo")
    assert repo.visibility == "private"
    assert repo.default_branch == "main"
    assert repo.private is True
    assert seen["method"] == "GET"
    assert seen["url"] == f"{GITHUB_API_BASE_URL}/repos/octo/demo"
    assert seen["auth"] == f"Bearer {TOKEN}"
    # The token is never placed in the URL or query string.
    assert TOKEN not in seen["url"]


def test_visibility_falls_back_to_private_flag_when_absent():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_repo_payload(visibility=None, private=False))

    repo = _client(handler).validate_repository("octo", "demo")
    assert repo.visibility == "public"
    assert repo.private is False


# ============================================================================
# Credential configuration
# ============================================================================

def test_missing_token_raises_configuration_error_without_request(monkeypatch):
    monkeypatch.delenv(ENV_GITHUB_TOKEN, raising=False)
    calls: List[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json=_repo_payload())

    client = _client(handler, token=None)
    with pytest.raises(GitHubConfigurationError):
        client.validate_repository("octo", "demo")
    # No unauthenticated fallback: the request is never attempted.
    assert calls == []


def test_token_is_resolved_from_environment(monkeypatch):
    monkeypatch.setenv(ENV_GITHUB_TOKEN, TOKEN)
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers.get("authorization")
        return httpx.Response(200, json=_repo_payload())

    client = GitHubClient(transport=httpx.MockTransport(handler))
    client.validate_repository("octo", "demo")
    assert seen["auth"] == f"Bearer {TOKEN}"


# ============================================================================
# Status-code mapping
# ============================================================================

def test_invalid_credentials_map_to_authentication_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"message": "Bad credentials"})

    with pytest.raises(GitHubAuthenticationError):
        _client(handler).validate_repository("octo", "demo")


def test_forbidden_without_rate_limit_maps_to_permission_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            403,
            headers={"x-ratelimit-remaining": "4999"},
            json={"message": "Resource not accessible by integration"},
        )

    with pytest.raises(GitHubPermissionError):
        _client(handler).validate_repository("octo", "demo")


def test_not_found_maps_to_not_found_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"message": "Not Found"})

    with pytest.raises(GitHubNotFoundError):
        _client(handler).validate_repository("octo", "demo")


def test_unprocessable_entity_maps_to_validation_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(422, json={"message": "No commit found"})

    with pytest.raises(GitHubValidationError):
        _client(handler).get_commit("octo", "demo", "deadbeef")


def test_server_error_maps_to_unavailable():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(502, json={"message": "Bad gateway"})

    with pytest.raises(GitHubUnavailableError):
        _client(handler).validate_repository("octo", "demo")


def test_redirect_is_rejected_and_not_followed():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": "https://evil.example.com/steal"})

    with pytest.raises(GitHubInvalidResponseError):
        _client(handler).validate_repository("octo", "demo")


# ============================================================================
# Rate limiting (403 and 429)
# ============================================================================

def test_rate_limit_via_403_carries_reset_metadata():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            403,
            headers={
                "x-ratelimit-limit": "5000",
                "x-ratelimit-remaining": "0",
                "x-ratelimit-reset": "1735689600",
            },
            json={"message": "API rate limit exceeded"},
        )

    with pytest.raises(GitHubRateLimitError) as excinfo:
        _client(handler).validate_repository("octo", "demo")

    err = excinfo.value
    assert err.limit == 5000
    assert err.remaining == 0
    assert err.reset_at == 1735689600


def test_rate_limit_via_429_and_retry_after():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            429,
            headers={"retry-after": "60"},
            json={"message": "Too many requests"},
        )

    with pytest.raises(GitHubRateLimitError) as excinfo:
        _client(handler).validate_repository("octo", "demo")

    assert excinfo.value.retry_after == 60


def test_secondary_rate_limit_is_recognised_despite_positive_remaining():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            403,
            headers={"x-ratelimit-remaining": "4999"},
            json={
                "message": (
                    "You have exceeded a secondary rate limit. "
                    "Please wait a few minutes before you try again."
                )
            },
        )

    with pytest.raises(GitHubRateLimitError) as excinfo:
        _client(handler).validate_repository("octo", "demo")

    err = excinfo.value
    # Safely available metadata only; the raw body is never echoed back.
    assert err.remaining == 4999
    assert err.retry_after is None
    assert "please wait" not in str(err).lower()
    assert "secondary rate limit." not in str(err).lower()


# ============================================================================
# Malformed numeric headers
# ============================================================================

def test_non_ascii_numeric_header_yields_safe_metadata():
    # "²" passes str.isdigit() but is not a valid int() argument.
    headers = httpx.Headers(
        {"x-ratelimit-remaining": "0", "x-ratelimit-reset": "\u00b2"},
        encoding="latin-1",
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            403, headers=headers, json={"message": "API rate limit exceeded"}
        )

    with pytest.raises(GitHubRateLimitError) as excinfo:
        _client(handler).validate_repository("octo", "demo")

    assert excinfo.value.remaining == 0
    # The malformed value is treated as unavailable rather than raising.
    assert excinfo.value.reset_at is None


def test_invalid_content_length_is_treated_as_unavailable():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, headers={"content-length": "not-a-number"}, json=_repo_payload()
        )

    repo = _client(handler).validate_repository("octo", "demo")
    assert repo.name == "demo"


def test_invalid_rate_limit_reset_header_is_ignored():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            403,
            headers={"x-ratelimit-remaining": "0", "x-ratelimit-reset": "soon"},
            json={"message": "API rate limit exceeded"},
        )

    with pytest.raises(GitHubRateLimitError) as excinfo:
        _client(handler).validate_repository("octo", "demo")

    assert excinfo.value.reset_at is None


def test_malformed_numeric_header_never_raises_value_error():
    # Directly exercise the parser with digit-like but non-ASCII content.
    class _StubResponse:
        def __init__(self, value: str) -> None:
            self.headers = {"x-ratelimit-reset": value}

    for value in ("\u00b2", "\u0663", "abc", "", "  ", "12x", "-1", "1.5"):
        assert GitHubClient._int_header(_StubResponse(value), "x-ratelimit-reset") is None

    assert GitHubClient._int_header(_StubResponse("42"), "x-ratelimit-reset") == 42


# ============================================================================
# Network failures
# ============================================================================

def test_timeout_maps_to_unavailable():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("connect timed out")

    with pytest.raises(GitHubUnavailableError):
        _client(handler).validate_repository("octo", "demo")


def test_connection_failure_maps_to_unavailable():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    with pytest.raises(GitHubUnavailableError):
        _client(handler).validate_repository("octo", "demo")


# ============================================================================
# Branches
# ============================================================================

def test_list_branches_returns_metadata_and_paginates():
    pages: List[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        page = int(request.url.params["page"])
        pages.append(page)
        assert request.url.params["per_page"] == "2"
        if page == 1:
            return httpx.Response(
                200,
                json=[
                    {"name": "main", "protected": True, "commit": {"sha": "aaa111"}},
                    {"name": "dev", "protected": False, "commit": {"sha": "bbb222"}},
                ],
            )
        return httpx.Response(
            200,
            json=[{"name": "feature", "protected": False, "commit": {"sha": "ccc333"}}],
        )

    branches = _client(handler).list_branches("octo", "demo", per_page=2, max_items=10)

    assert [b.name for b in branches] == ["main", "dev", "feature"]
    assert branches[0] == GitHubBranch(name="main", protected=True, commit_sha="aaa111")
    assert pages == [1, 2]


def test_list_branches_stops_at_max_items():
    requests_seen: List[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests_seen.append(int(request.url.params["page"]))
        return httpx.Response(
            200,
            json=[
                {"name": "a", "protected": False, "commit": {"sha": "1"}},
                {"name": "b", "protected": False, "commit": {"sha": "2"}},
            ],
        )

    branches = _client(handler).list_branches("octo", "demo", per_page=2, max_items=2)
    assert len(branches) == 2
    assert requests_seen == [1]


def test_list_branches_stops_at_max_pages():
    pages: List[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        page = int(request.url.params["page"])
        pages.append(page)
        # Always a full page, so only the max_pages bound can end the loop.
        return httpx.Response(
            200,
            json=[{"name": f"b{page}", "protected": False, "commit": {"sha": str(page)}}],
        )

    branches = _client(handler, max_pages=2).list_branches(
        "octo", "demo", per_page=1, max_items=1000
    )

    assert pages == [1, 2]
    assert [b.name for b in branches] == ["b1", "b2"]


def test_list_branches_bounds_extremely_large_max_items():
    pages: List[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        page = int(request.url.params["page"])
        per_page = int(request.url.params["per_page"])
        pages.append(page)
        return httpx.Response(
            200,
            json=[
                {"name": f"b{page}_{i}", "protected": False, "commit": {"sha": str(i)}}
                for i in range(per_page)
            ],
        )

    branches = _client(handler, max_pages=20).list_branches(
        "octo", "demo", per_page=MAX_PER_PAGE, max_items=10**9
    )

    # max_items is clamped to MAX_PER_PAGE * DEFAULT_MAX_PAGES, so pagination
    # stops well before the 20 pages the caller would otherwise allow.
    assert len(branches) == MAX_PER_PAGE * DEFAULT_MAX_PAGES
    assert pages == list(range(1, DEFAULT_MAX_PAGES + 1))


# ============================================================================
# Commits
# ============================================================================

def test_get_commit_resolves_sha_and_metadata():
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        return httpx.Response(
            200,
            json={
                "sha": "9f8e7d6c5b4a3210",
                "html_url": "https://github.com/octo/demo/commit/9f8e7d",
                "commit": {
                    "message": "Fix null deref\n\nDetails",
                    "author": {"name": "Ada", "date": "2026-01-02T03:04:05Z"},
                },
                "author": {"login": "ada"},
            },
        )

    commit = _client(handler).get_commit("octo", "demo", "main")

    assert isinstance(commit, GitHubCommit)
    assert commit.sha == "9f8e7d6c5b4a3210"
    assert commit.author_login == "ada"
    assert commit.author_name == "Ada"
    assert commit.committed_at == "2026-01-02T03:04:05Z"
    assert "/repos/octo/demo/commits/main" in seen["url"]


def test_get_commit_pins_full_sha_in_path():
    seen: dict = {}
    sha = "0123456789abcdef0123456789abcdef01234567"

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        return httpx.Response(200, json={"sha": sha, "commit": {}})

    commit = _client(handler).get_commit("octo", "demo", sha)
    assert commit.sha == sha
    assert seen["url"].endswith(f"/commits/{sha}")


# ============================================================================
# File contents
# ============================================================================

def test_get_file_contents_decodes_base64_and_pins_ref():
    raw = b"print('hello')\n"
    encoded = base64.b64encode(raw).decode("ascii")
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["ref"] = request.url.params.get("ref")
        seen["path"] = request.url.path
        return httpx.Response(
            200,
            json={
                "type": "file",
                "encoding": "base64",
                "size": len(raw),
                # GitHub wraps base64 payloads with newlines.
                "content": "\n".join(encoded[i : i + 20] for i in range(0, len(encoded), 20)) + "\n",
                "sha": "filesha123",
                "path": "service/app.py",
                "name": "app.py",
            },
        )

    file_content = _client(handler).get_file_contents("octo", "demo", "service/app.py", ref="main")

    assert isinstance(file_content, GitHubFileContent)
    assert file_content.content == "print('hello')\n"
    assert file_content.is_binary is False
    assert file_content.size == len(raw)
    assert file_content.sha == "filesha123"
    assert file_content.ref == "main"
    assert seen["ref"] == "main"
    assert seen["path"] == "/repos/octo/demo/contents/service/app.py"


def test_get_file_contents_rejects_unsupported_encoding():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"type": "file", "encoding": "utf-8", "size": 3, "content": "abc", "sha": "x"},
        )

    with pytest.raises(GitHubInvalidResponseError):
        _client(handler).get_file_contents("octo", "demo", "README.md")


def test_get_file_contents_rejects_oversized_declared_size():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "type": "file",
                "encoding": "base64",
                "size": 10_000,
                "content": base64.b64encode(b"x" * 16).decode("ascii"),
                "sha": "x",
            },
        )

    client = _client(handler, max_file_bytes=100)
    with pytest.raises(GitHubInvalidResponseError):
        client.get_file_contents("octo", "demo", "big.bin")


def test_get_file_contents_rejects_oversized_decoded_content():
    payload = b"x" * 512

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "type": "file",
                "encoding": "base64",
                "size": 1,  # understated to force the decoded-length guard
                "content": base64.b64encode(payload).decode("ascii"),
                "sha": "x",
            },
        )

    client = _client(handler, max_file_bytes=100)
    with pytest.raises(GitHubInvalidResponseError):
        client.get_file_contents("octo", "demo", "big.bin")


def test_get_file_contents_handles_binary_payload():
    payload = b"\xff\xfe\x00\x01binary"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "type": "file",
                "encoding": "base64",
                "size": len(payload),
                "content": base64.b64encode(payload).decode("ascii"),
                "sha": "x",
            },
        )

    file_content = _client(handler).get_file_contents("octo", "demo", "blob.bin")
    assert file_content.is_binary is True
    assert file_content.content is None


def test_get_file_contents_decodes_embedded_nul_as_text():
    payload = b"value=\x00end"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "type": "file",
                "encoding": "base64",
                "size": len(payload),
                "content": base64.b64encode(payload).decode("ascii"),
                "sha": "x",
            },
        )

    file_content = _client(handler).get_file_contents("octo", "demo", "nul.txt")

    # NUL is valid UTF-8, so existing behaviour decodes it as text.
    assert file_content.is_binary is False
    assert file_content.content == "value=\x00end"


def test_get_file_contents_rejects_directory_payload():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[{"type": "file", "name": "a.py"}])

    with pytest.raises(GitHubInvalidResponseError):
        _client(handler).get_file_contents("octo", "demo", "service")


# ============================================================================
# Pull requests
# ============================================================================

def test_list_pull_requests_returns_metadata():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["state"] == "all"
        return httpx.Response(
            200,
            json=[
                {
                    "number": 12,
                    "title": "Fix crash",
                    "state": "open",
                    "draft": False,
                    "user": {"login": "ada"},
                    "head": {"sha": "head123", "ref": "fix"},
                    "base": {"ref": "main"},
                    "html_url": "https://github.com/octo/demo/pull/12",
                    "updated_at": "2026-02-03T00:00:00Z",
                }
            ],
        )

    prs = _client(handler).list_pull_requests("octo", "demo", state="all")

    assert prs == [
        GitHubPullRequest(
            number=12,
            title="Fix crash",
            state="open",
            author_login="ada",
            head_sha="head123",
            base_branch="main",
            draft=False,
            html_url="https://github.com/octo/demo/pull/12",
            updated_at="2026-02-03T00:00:00Z",
        )
    ]


def test_list_pull_requests_bounds_pagination():
    pages: List[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        page = int(request.url.params["page"])
        pages.append(page)
        if page == 1:
            return httpx.Response(
                200,
                json=[
                    {"number": 1, "title": "a", "state": "open"},
                    {"number": 2, "title": "b", "state": "open"},
                ],
            )
        return httpx.Response(200, json=[{"number": 3, "title": "c", "state": "open"}])

    prs = _client(handler).list_pull_requests("octo", "demo", per_page=2, max_items=10)
    assert [pr.number for pr in prs] == [1, 2, 3]
    assert pages == [1, 2]


def test_list_pull_requests_stops_at_max_items():
    pages: List[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        pages.append(int(request.url.params["page"]))
        return httpx.Response(
            200,
            json=[
                {"number": 1, "title": "a", "state": "open"},
                {"number": 2, "title": "b", "state": "open"},
            ],
        )

    prs = _client(handler).list_pull_requests("octo", "demo", per_page=2, max_items=1)
    assert len(prs) == 1
    assert pages == [1]


# ============================================================================
# Response size bounds
# ============================================================================

def test_oversized_response_body_is_rejected():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"x" * 500)

    client = _client(handler, max_response_bytes=100)
    with pytest.raises(GitHubInvalidResponseError):
        client.validate_repository("octo", "demo")


# ============================================================================
# Input validation
# ============================================================================

def _never_called(request: httpx.Request) -> httpx.Response:  # pragma: no cover - guard
    raise AssertionError("no request should be issued for invalid input")


@pytest.mark.parametrize("owner", ["", " ", "-leading", "trailing-", "bad name", "a/b"])
def test_invalid_owner_is_rejected(owner):
    client = _client(_never_called)
    with pytest.raises(GitHubValidationError):
        client.validate_repository(owner, "demo")


@pytest.mark.parametrize("repo", ["", " ", "bad name", "bad/name", "x" * 101])
def test_invalid_repo_is_rejected(repo):
    client = _client(_never_called)
    with pytest.raises(GitHubValidationError):
        client.validate_repository("octo", repo)


@pytest.mark.parametrize(
    "path",
    ["../etc/passwd", "/abs/path", "a/../b", "a//b", "trailing/", "back\\slash", "a\x00b"],
)
def test_invalid_path_is_rejected(path):
    client = _client(_never_called)
    with pytest.raises(GitHubValidationError):
        client.get_file_contents("octo", "demo", path)


@pytest.mark.parametrize("ref", ["", " ", "..", "a//b", "bad ref", "ref~1", "v1.0^", "ends.lock"])
def test_invalid_ref_is_rejected(ref):
    client = _client(_never_called)
    with pytest.raises(GitHubValidationError):
        client.get_commit("octo", "demo", ref)


def test_invalid_pull_request_state_is_rejected():
    client = _client(_never_called)
    with pytest.raises(GitHubValidationError):
        client.list_pull_requests("octo", "demo", state="merged")


# ============================================================================
# Origin restriction (scheme, host and port)
# ============================================================================

def _no_request_handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover - guard
    raise AssertionError("no request should be issued to a rejected origin")


def test_client_refuses_non_github_host():
    client = GitHubClient(
        token=TOKEN,
        base_url="https://evil.example.com",
        transport=httpx.MockTransport(_no_request_handler),
    )
    with pytest.raises(GitHubError):
        client.validate_repository("octo", "demo")


def test_client_refuses_plain_http_scheme():
    client = GitHubClient(
        token=TOKEN,
        base_url="http://api.github.com",
        transport=httpx.MockTransport(_no_request_handler),
    )
    with pytest.raises(GitHubError):
        client.validate_repository("octo", "demo")


def test_client_refuses_non_standard_port():
    client = GitHubClient(
        token=TOKEN,
        base_url="https://api.github.com:8443",
        transport=httpx.MockTransport(_no_request_handler),
    )
    with pytest.raises(GitHubError):
        client.validate_repository("octo", "demo")


def test_client_accepts_canonical_https_origin():
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        return httpx.Response(200, json=_repo_payload())

    # The implicit default port (None) is accepted.
    repo = _client(handler).validate_repository("octo", "demo")
    assert repo.name == "demo"
    assert seen["url"] == f"{GITHUB_API_BASE_URL}/repos/octo/demo"


def test_client_accepts_explicit_default_https_port():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_repo_payload())

    client = GitHubClient(
        token=TOKEN,
        base_url="https://api.github.com:443",
        transport=httpx.MockTransport(handler),
    )
    assert client.validate_repository("octo", "demo").name == "demo"


# ============================================================================
# Read-only method guard
# ============================================================================

def test_direct_non_get_request_is_rejected():
    client = _client(_never_called)
    with pytest.raises(GitHubError) as excinfo:
        client._request("POST", "/repos/octo/demo")
    assert "read-only" in str(excinfo.value).lower()


# ============================================================================
# Secret redaction
# ============================================================================

def test_redact_secret_masks_provided_and_prefixed_tokens():
    message = f"failure using {TOKEN} and github_pat_{'x' * 40}"
    redacted = redact_secret(message)
    assert TOKEN not in redacted
    assert "github_pat_" not in redacted
    assert "***REDACTED_GITHUB_TOKEN***" in redacted


def test_token_never_appears_in_logs_or_raised_errors(caplog):
    caplog.set_level(logging.DEBUG)

    def handler(request: httpx.Request) -> httpx.Response:
        # Simulate a transport error whose message embeds the token.
        raise httpx.ConnectError(f"connection refused while using {TOKEN}")

    client = _client(handler)
    with pytest.raises(GitHubUnavailableError) as excinfo:
        client.validate_repository("octo", "demo")

    assert TOKEN not in str(excinfo.value)
    assert TOKEN not in caplog.text
    assert "***REDACTED_GITHUB_TOKEN***" in str(excinfo.value)


def test_error_messages_do_not_include_response_bodies():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"message": "internal detail leaked"})

    with pytest.raises(GitHubPermissionError) as excinfo:
        _client(handler).validate_repository("octo", "demo")

    assert "internal detail leaked" not in str(excinfo.value)


# ============================================================================
# Lifecycle
# ============================================================================

def test_client_is_a_context_manager():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_repo_payload())

    with _client(handler) as client:
        repo = client.validate_repository("octo", "demo")
    assert repo.name == "demo"
