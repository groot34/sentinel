"""Read-only GitHub REST API client for Sentinel 2.0 (Mission 27.2).

This module provides a deliberately small, read-only client for the public
GitHub REST API, used to enrich incident investigations with repository
context. It implements the client proposed in the Mission 27.1 architecture
audit and follows existing project conventions (environment-based credentials,
sanitised errors, no secret leakage).

Design principles
-----------------
* **Read-only.** Only ``GET`` requests are ever issued. There is no method to
  write files, create commits, branches or pull requests, or deploy anything.
* **Fail-closed.** Requests are only issued against ``api.github.com`` with
  redirects disabled, explicit connect/read timeouts, bounded response sizes
  and bounded pagination. There is no fallback to unauthenticated requests.
* **Credential hygiene.** The token is read from ``SENTINEL_GITHUB_TOKEN`` and
  is only ever sent in the ``Authorization`` header - never in a URL or query
  string, never logged, and never included in error messages.
* **Safe errors.** Every raised exception carries a fixed, sanitised message;
  raw response bodies and request headers are never surfaced, and any token
  value found in an error string is redacted defensively.
* **No interpretation.** Retrieved file contents are decoded to text but never
  executed, evaluated, or otherwise interpreted by this module.
"""

from __future__ import annotations

import base64
import binascii
import logging
import os
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional
from urllib.parse import quote

import httpx

logger = logging.getLogger(__name__)


# ============================================================================
# Configuration
# ============================================================================

#: Environment variable holding the read-only GitHub credential.
ENV_GITHUB_TOKEN = "SENTINEL_GITHUB_TOKEN"

#: Fixed GitHub REST API base URL.
GITHUB_API_BASE_URL = "https://api.github.com"

#: The only host requests are ever issued against.
GITHUB_API_HOST = "api.github.com"

#: The only scheme and port permitted for API requests. Requests must use HTTPS
#: on the standard port; an unexpected scheme or explicit port is refused.
GITHUB_API_SCHEME = "https"
GITHUB_API_PORT = 443

#: Pinned GitHub REST API version header value.
GITHUB_API_VERSION = "2022-11-28"

#: Default connect/read timeouts in seconds. Both are always explicit so a
#: stalled GitHub endpoint can never block an investigation indefinitely.
DEFAULT_CONNECT_TIMEOUT_SECONDS = 5.0
DEFAULT_READ_TIMEOUT_SECONDS = 15.0
DEFAULT_TIMEOUT = httpx.Timeout(
    DEFAULT_READ_TIMEOUT_SECONDS,
    connect=DEFAULT_CONNECT_TIMEOUT_SECONDS,
)

#: Bounds applied by default (all overridable per instance / per call).
DEFAULT_MAX_FILE_BYTES = 1_048_576  # 1 MiB decoded file content
DEFAULT_MAX_RESPONSE_BYTES = 10_485_760  # 10 MiB raw HTTP response body
DEFAULT_MAX_PAGES = 10
DEFAULT_PER_PAGE = 30
MAX_PER_PAGE = 100
DEFAULT_MAX_ITEMS = 100

_USER_AGENT = "sentinel-incident-investigator"


# ============================================================================
# Secret redaction
# ============================================================================

_REDACTED = "***REDACTED_GITHUB_TOKEN***"

#: Well-known GitHub credential prefixes, matched defensively so a token can
#: never leak even when it was not supplied to :func:`redact_secret`.
_TOKEN_PATTERNS = (
    re.compile(r"github_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}"),
)


def redact_secret(message: str, secret: Optional[str] = None) -> str:
    """Return *message* with any GitHub token value removed.

    The supplied *secret* (when long enough to be a real credential) is
    replaced verbatim, and known GitHub token prefixes are matched
    defensively. The result is always safe to log or surface.
    """
    if not message:
        return ""
    sanitized = str(message)
    if secret and len(secret) >= 8:
        sanitized = sanitized.replace(secret, _REDACTED)
    for pattern in _TOKEN_PATTERNS:
        sanitized = pattern.sub(_REDACTED, sanitized)
    return sanitized


# ============================================================================
# Errors
# ============================================================================

class GitHubError(Exception):
    """Base class for all GitHub integration errors."""


class GitHubConfigurationError(GitHubError):
    """Raised when the GitHub credential is missing or unusable.

    Deliberately raised only when a GitHub operation is attempted, so an
    unconfigured deployment still starts and runs normally.
    """


class GitHubValidationError(GitHubError):
    """Raised when caller-supplied input (owner, repo, ref, path) is invalid."""


class GitHubAuthenticationError(GitHubError):
    """Raised when GitHub rejects the configured credential (HTTP 401)."""


class GitHubPermissionError(GitHubError):
    """Raised when the credential lacks permission for the resource (HTTP 403)."""


class GitHubNotFoundError(GitHubError):
    """Raised when a resource is missing or not accessible (HTTP 404)."""


class GitHubRateLimitError(GitHubError):
    """Raised when GitHub rate-limits the credential (HTTP 403 or 429).

    Carries the rate-limit metadata parsed from the response headers so callers
    can back off intelligently. No request headers or response bodies are kept.
    """

    def __init__(
        self,
        message: str,
        *,
        limit: Optional[int] = None,
        remaining: Optional[int] = None,
        reset_at: Optional[int] = None,
        retry_after: Optional[int] = None,
    ) -> None:
        super().__init__(message)
        self.limit = limit
        self.remaining = remaining
        self.reset_at = reset_at
        self.retry_after = retry_after


class GitHubUnavailableError(GitHubError):
    """Raised for GitHub 5xx responses, network failures and timeouts."""


class GitHubInvalidResponseError(GitHubError):
    """Raised for malformed, unexpected, oversized or unsupported responses."""


# ============================================================================
# Response models
# ============================================================================

@dataclass(frozen=True)
class GitHubRepository:
    """Validated metadata for a single GitHub repository."""

    owner: str
    name: str
    full_name: str
    visibility: str
    default_branch: str
    private: bool
    description: Optional[str]
    html_url: str


@dataclass(frozen=True)
class GitHubBranch:
    """A repository branch with its protection state and tip commit."""

    name: str
    protected: bool
    commit_sha: str


@dataclass(frozen=True)
class GitHubCommit:
    """Metadata for a resolved commit."""

    sha: str
    message: str
    author_login: Optional[str]
    author_name: Optional[str]
    committed_at: Optional[str]
    html_url: str


@dataclass(frozen=True)
class GitHubFileContent:
    """Decoded contents of a single repository file.

    ``content`` is ``None`` when the file is not valid UTF-8 text, in which case
    ``is_binary`` is ``True``. Contents are never executed or interpreted.
    """

    path: str
    ref: Optional[str]
    sha: Optional[str]
    size: int
    is_binary: bool
    content: Optional[str]


@dataclass(frozen=True)
class GitHubPullRequest:
    """Metadata for a single pull request."""

    number: int
    title: str
    state: str
    author_login: Optional[str]
    head_sha: str
    base_branch: str
    draft: bool
    html_url: str
    updated_at: Optional[str]


# ============================================================================
# Input validation
# ============================================================================

_OWNER_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9._-]{0,98}[A-Za-z0-9])?$")
_REPO_RE = re.compile(r"^[A-Za-z0-9._-]{1,100}$")
_REF_ALLOWED_RE = re.compile(r"^[A-Za-z0-9._/@+\-]+$")

_MAX_REF_LENGTH = 255
_MAX_PATH_LENGTH = 1024
_VALID_PR_STATES = ("open", "closed", "all")


def _validate_owner(owner: str) -> str:
    if not isinstance(owner, str) or not _OWNER_RE.match(owner.strip()):
        raise GitHubValidationError("Invalid GitHub repository owner.")
    return owner.strip()


def _validate_repo(repo: str) -> str:
    if not isinstance(repo, str) or not _REPO_RE.match(repo.strip()):
        raise GitHubValidationError("Invalid GitHub repository name.")
    return repo.strip()


def _validate_ref(ref: str) -> str:
    """Validate a branch, tag or commit SHA reference."""
    if not isinstance(ref, str):
        raise GitHubValidationError("Invalid GitHub ref.")
    candidate = ref.strip()
    if not candidate or len(candidate) > _MAX_REF_LENGTH:
        raise GitHubValidationError("Invalid GitHub ref.")
    if candidate.startswith("/") or candidate.endswith("/"):
        raise GitHubValidationError("Invalid GitHub ref.")
    if candidate == "." or candidate.endswith(".lock"):
        raise GitHubValidationError("Invalid GitHub ref.")
    if "//" in candidate or ".." in candidate or "@{" in candidate:
        raise GitHubValidationError("Invalid GitHub ref.")
    if any(ch in candidate for ch in " ~^:?*[\\"):
        raise GitHubValidationError("Invalid GitHub ref.")
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in candidate):
        raise GitHubValidationError("Invalid GitHub ref.")
    if not _REF_ALLOWED_RE.match(candidate):
        raise GitHubValidationError("Invalid GitHub ref.")
    return candidate


def _validate_path(path: str) -> str:
    """Validate a repository-relative file path."""
    if not isinstance(path, str):
        raise GitHubValidationError("Invalid GitHub file path.")
    candidate = path.strip()
    if not candidate or len(candidate) > _MAX_PATH_LENGTH:
        raise GitHubValidationError("Invalid GitHub file path.")
    if candidate.startswith("/") or candidate.endswith("/"):
        raise GitHubValidationError("Invalid GitHub file path.")
    if "\\" in candidate or "\x00" in candidate:
        raise GitHubValidationError("Invalid GitHub file path.")
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in candidate):
        raise GitHubValidationError("Invalid GitHub file path.")
    if any(segment in ("", ".", "..") for segment in candidate.split("/")):
        raise GitHubValidationError("Invalid GitHub file path.")
    return candidate


def _validate_state(state: str) -> str:
    if not isinstance(state, str) or state.strip().lower() not in _VALID_PR_STATES:
        raise GitHubValidationError("Invalid pull request state.")
    return state.strip().lower()


def _clamp_int(value: Any, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return max(minimum, min(maximum, parsed))


# ============================================================================
# Client
# ============================================================================

class GitHubClient:
    """Minimal, read-only GitHub REST API client.

    The credential is resolved lazily from ``SENTINEL_GITHUB_TOKEN`` (or the
    constructor's ``token`` argument) so that an unconfigured Sentinel instance
    starts normally and only reports a clear configuration error when a GitHub
    operation is actually attempted.
    """

    #: Only GET is ever used; the constant documents the read-only contract.
    ALLOWED_METHOD = "GET"

    def __init__(
        self,
        token: Optional[str] = None,
        *,
        base_url: str = GITHUB_API_BASE_URL,
        timeout: Optional[httpx.Timeout] = None,
        transport: Optional[httpx.BaseTransport] = None,
        max_file_bytes: int = DEFAULT_MAX_FILE_BYTES,
        max_response_bytes: int = DEFAULT_MAX_RESPONSE_BYTES,
        max_pages: int = DEFAULT_MAX_PAGES,
    ) -> None:
        self._token = token
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout or DEFAULT_TIMEOUT
        self._max_file_bytes = max(1, int(max_file_bytes))
        self._max_response_bytes = max(1, int(max_response_bytes))
        self._max_pages = max(1, int(max_pages))

        self._client = httpx.Client(
            base_url=self._base_url,
            timeout=self._timeout,
            # Unsafe redirects are never followed; the base host is fixed.
            follow_redirects=False,
            transport=transport,
            headers={
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": GITHUB_API_VERSION,
                "User-Agent": _USER_AGENT,
            },
        )

    # -- lifecycle ----------------------------------------------------------

    def close(self) -> None:
        """Close the underlying HTTP connection pool."""
        self._client.close()

    def __enter__(self) -> "GitHubClient":
        return self

    def __exit__(self, *exc_info: Any) -> None:
        self.close()

    # -- credential handling ------------------------------------------------

    def _resolve_token(self) -> str:
        """Return the configured token or raise a clear configuration error."""
        token = self._token if self._token is not None else os.getenv(ENV_GITHUB_TOKEN)
        if not token or not token.strip():
            raise GitHubConfigurationError(
                f"Missing required {ENV_GITHUB_TOKEN} environment variable. "
                "Configure a read-only GitHub token before using the GitHub integration."
            )
        return token.strip()

    def _build_headers(self) -> Dict[str, str]:
        # The token is only ever placed in the Authorization header.
        return {"Authorization": f"Bearer {self._resolve_token()}"}

    # -- HTTP plumbing ------------------------------------------------------

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: Optional[Dict[str, Any]] = None,
    ) -> httpx.Response:
        if method != self.ALLOWED_METHOD:
            # Defensive: this client only ever issues read-only requests.
            raise GitHubError("Only read-only GitHub requests are supported.")

        headers = self._build_headers()

        # Restrict requests to the fixed GitHub API origin: HTTPS only, the
        # exact api.github.com host, and no unexpected explicit port. This is
        # enforced regardless of the transport in use, so a MockTransport in
        # tests never weakens production URL validation.
        resolved = httpx.URL(self._base_url).join(path)
        if (
            resolved.scheme != GITHUB_API_SCHEME
            or resolved.host != GITHUB_API_HOST
            or resolved.port not in (None, GITHUB_API_PORT)
        ):
            raise GitHubError(
                "Refusing to contact anything other than https://api.github.com."
            )

        logger.debug("GitHub request: %s %s", method, path)

        try:
            response = self._client.request(method, path, params=params, headers=headers)
        except httpx.TimeoutException as exc:
            raise GitHubUnavailableError(
                redact_secret(
                    f"GitHub request timed out ({exc.__class__.__name__}).", self._token
                )
            ) from None
        except httpx.RequestError as exc:
            raise GitHubUnavailableError(
                redact_secret(
                    f"GitHub request failed ({exc.__class__.__name__}). {exc}", self._token
                )
            ) from None

        self._enforce_response_size(response)
        self._raise_for_status(response)
        return response

    def _enforce_response_size(self, response: httpx.Response) -> None:
        # Known limitation: httpx buffers the full body before this runs, so the
        # bound prevents an oversized payload from being processed, not from
        # being transferred. A streaming HTTP refactor is deliberately out of
        # scope for this client.
        declared = self._int_header(response, "content-length")
        if declared is not None and declared > self._max_response_bytes:
            raise GitHubInvalidResponseError(
                "GitHub response exceeded the maximum allowed size."
            )
        if len(response.content) > self._max_response_bytes:
            raise GitHubInvalidResponseError(
                "GitHub response exceeded the maximum allowed size."
            )

    def _raise_for_status(self, response: httpx.Response) -> None:
        status = response.status_code
        if 200 <= status < 300:
            return
        if 300 <= status < 400:
            raise GitHubInvalidResponseError(
                "GitHub returned an unexpected redirect; redirects are not followed."
            )
        if status == 401:
            raise GitHubAuthenticationError(
                "GitHub rejected the configured credentials (401 Unauthorized)."
            )
        if status == 403:
            if self._is_rate_limited(response):
                raise self._build_rate_limit_error(response)
            raise GitHubPermissionError(
                "GitHub denied access to the requested resource (403 Forbidden)."
            )
        if status == 429:
            raise self._build_rate_limit_error(response)
        if status == 404:
            raise GitHubNotFoundError(
                "The requested GitHub resource was not found or is not accessible."
            )
        if status in (400, 422):
            raise GitHubValidationError("GitHub rejected the request as invalid.")
        if 500 <= status < 600:
            raise GitHubUnavailableError(
                "GitHub is currently unavailable (server error)."
            )
        raise GitHubError(f"Unexpected GitHub response status ({status}).")

    @classmethod
    def _is_rate_limited(cls, response: httpx.Response) -> bool:
        """Return True when a response indicates any form of rate limit.

        Covers the primary limit (``x-ratelimit-remaining`` of zero), any
        response carrying ``Retry-After``, and secondary/abuse limits, which
        are reported with a positive remaining count, no ``Retry-After`` header
        and an explicit message.
        """
        if cls._int_header(response, "x-ratelimit-remaining") == 0:
            return True
        if response.headers.get("retry-after"):
            return True
        return "secondary rate limit" in cls._response_message(response).lower()

    @staticmethod
    def _response_message(response: httpx.Response) -> str:
        """Best-effort read of GitHub's error ``message`` field.

        Only the ``message`` string is read; the raw response body is never
        surfaced. Returns an empty string when the body is absent or is not a
        JSON object, so classification can never raise on a malformed body.
        """
        try:
            payload = response.json()
        except ValueError:
            return ""
        if isinstance(payload, dict):
            message = payload.get("message")
            if isinstance(message, str):
                return message
        return ""

    @staticmethod
    def _int_header(response: httpx.Response, name: str) -> Optional[int]:
        """Parse an integer response header, tolerating malformed values.

        Only ASCII decimal digits are accepted; anything else (including
        non-ASCII digits such as ``²``) is treated as unavailable metadata so a
        malformed header can never raise an unexpected error.
        """
        value = response.headers.get(name)
        if value is None:
            return None
        value = value.strip()
        if not value or not value.isascii() or not value.isdigit():
            return None
        try:
            return int(value)
        except ValueError:  # pragma: no cover - unreachable given the checks above
            return None

    def _build_rate_limit_error(self, response: httpx.Response) -> GitHubRateLimitError:
        limit = self._int_header(response, "x-ratelimit-limit")
        remaining = self._int_header(response, "x-ratelimit-remaining")
        reset_at = self._int_header(response, "x-ratelimit-reset")
        retry_after = self._int_header(response, "retry-after")
        message = (
            "GitHub API rate limit exceeded "
            f"(limit={limit}, remaining={remaining}, "
            f"reset_at={reset_at}, retry_after={retry_after})."
        )
        return GitHubRateLimitError(
            message,
            limit=limit,
            remaining=remaining,
            reset_at=reset_at,
            retry_after=retry_after,
        )

    @staticmethod
    def _json(response: httpx.Response) -> Any:
        try:
            return response.json()
        except ValueError:
            raise GitHubInvalidResponseError(
                "GitHub returned a response that was not valid JSON."
            ) from None

    def _paginate(
        self,
        path: str,
        *,
        params: Optional[Dict[str, Any]],
        per_page: int,
        max_items: int,
    ) -> List[Dict[str, Any]]:
        """Collect at most *max_items* objects across bounded pages."""
        bounded_per_page = _clamp_int(per_page, DEFAULT_PER_PAGE, 1, MAX_PER_PAGE)
        bounded_max_items = _clamp_int(max_items, DEFAULT_MAX_ITEMS, 1, MAX_PER_PAGE * DEFAULT_MAX_PAGES)

        items: List[Dict[str, Any]] = []
        page = 1
        while page <= self._max_pages and len(items) < bounded_max_items:
            page_params = dict(params or {})
            page_params["per_page"] = bounded_per_page
            page_params["page"] = page
            response = self._request("GET", path, params=page_params)
            payload = self._json(response)
            if not isinstance(payload, list):
                raise GitHubInvalidResponseError(
                    "GitHub returned an unexpected response shape."
                )
            if not payload:
                break
            items.extend(entry for entry in payload if isinstance(entry, dict))
            if len(payload) < bounded_per_page:
                break
            page += 1

        return items[:bounded_max_items]

    # -- public read-only operations ---------------------------------------

    def validate_repository(self, owner: str, repo: str) -> GitHubRepository:
        """Retrieve and validate repository metadata.

        Returns owner, name, visibility and default branch information.
        """
        owner = _validate_owner(owner)
        repo = _validate_repo(repo)
        response = self._request("GET", f"/repos/{owner}/{repo}")
        data = self._json(response)
        if not isinstance(data, dict):
            raise GitHubInvalidResponseError("GitHub returned an unexpected response shape.")

        owner_block = data.get("owner") if isinstance(data.get("owner"), dict) else {}
        resolved_owner = str(owner_block.get("login") or owner)
        private = bool(data.get("private", False))
        visibility = data.get("visibility")
        if not isinstance(visibility, str) or not visibility.strip():
            visibility = "private" if private else "public"

        return GitHubRepository(
            owner=resolved_owner,
            name=str(data.get("name") or repo),
            full_name=str(data.get("full_name") or f"{owner}/{repo}"),
            visibility=visibility,
            default_branch=str(data.get("default_branch") or ""),
            private=private,
            description=data.get("description") if isinstance(data.get("description"), str) else None,
            html_url=str(data.get("html_url") or ""),
        )

    def list_branches(
        self,
        owner: str,
        repo: str,
        *,
        per_page: int = DEFAULT_PER_PAGE,
        max_items: int = DEFAULT_MAX_ITEMS,
    ) -> List[GitHubBranch]:
        """Retrieve repository branches with protection state and tip SHA."""
        owner = _validate_owner(owner)
        repo = _validate_repo(repo)
        entries = self._paginate(
            f"/repos/{owner}/{repo}/branches",
            params=None,
            per_page=per_page,
            max_items=max_items,
        )
        branches: List[GitHubBranch] = []
        for entry in entries:
            commit = entry.get("commit") if isinstance(entry.get("commit"), dict) else {}
            name = entry.get("name")
            if not isinstance(name, str):
                continue
            branches.append(
                GitHubBranch(
                    name=name,
                    protected=bool(entry.get("protected", False)),
                    commit_sha=str(commit.get("sha") or ""),
                )
            )
        return branches

    def get_commit(self, owner: str, repo: str, ref: str) -> GitHubCommit:
        """Retrieve metadata for a commit, resolving the requested ref to a SHA."""
        owner = _validate_owner(owner)
        repo = _validate_repo(repo)
        ref = _validate_ref(ref)
        response = self._request("GET", f"/repos/{owner}/{repo}/commits/{quote(ref, safe='')}")
        data = self._json(response)
        if not isinstance(data, dict):
            raise GitHubInvalidResponseError("GitHub returned an unexpected response shape.")

        commit_block = data.get("commit") if isinstance(data.get("commit"), dict) else {}
        author_block = commit_block.get("author") if isinstance(commit_block.get("author"), dict) else {}
        user_block = data.get("author") if isinstance(data.get("author"), dict) else {}

        return GitHubCommit(
            sha=str(data.get("sha") or ""),
            message=str(commit_block.get("message") or ""),
            author_login=str(user_block.get("login")) if user_block.get("login") else None,
            author_name=str(author_block.get("name")) if author_block.get("name") else None,
            committed_at=str(author_block.get("date")) if author_block.get("date") else None,
            html_url=str(data.get("html_url") or ""),
        )

    def get_file_contents(
        self,
        owner: str,
        repo: str,
        path: str,
        ref: Optional[str] = None,
    ) -> GitHubFileContent:
        """Retrieve and decode a single file, optionally pinned to a ref/SHA.

        Contents are decoded to text but never executed or interpreted.
        """
        owner = _validate_owner(owner)
        repo = _validate_repo(repo)
        path = _validate_path(path)
        params: Optional[Dict[str, Any]] = None
        if ref is not None:
            params = {"ref": _validate_ref(ref)}

        response = self._request(
            "GET", f"/repos/{owner}/{repo}/contents/{quote(path, safe='/')}", params=params
        )
        data = self._json(response)
        if isinstance(data, list):
            raise GitHubInvalidResponseError(
                "The requested path refers to a directory, not a file."
            )
        if not isinstance(data, dict):
            raise GitHubInvalidResponseError("GitHub returned an unexpected response shape.")

        encoding = data.get("encoding")
        if encoding != "base64":
            raise GitHubInvalidResponseError(
                "GitHub returned an unsupported file encoding."
            )

        declared_size = data.get("size")
        if isinstance(declared_size, int) and declared_size > self._max_file_bytes:
            raise GitHubInvalidResponseError("File exceeds the maximum allowed size.")

        raw_content = data.get("content")
        if not isinstance(raw_content, str):
            raise GitHubInvalidResponseError("GitHub returned no file content.")
        compact = re.sub(r"\s+", "", raw_content)
        try:
            decoded = base64.b64decode(compact, validate=True)
        except (binascii.Error, ValueError):
            raise GitHubInvalidResponseError(
                "GitHub returned file content that could not be decoded."
            ) from None

        if len(decoded) > self._max_file_bytes:
            raise GitHubInvalidResponseError("File exceeds the maximum allowed size.")

        try:
            text: Optional[str] = decoded.decode("utf-8")
            is_binary = False
        except UnicodeDecodeError:
            text = None
            is_binary = True

        return GitHubFileContent(
            path=path,
            ref=ref,
            sha=str(data.get("sha")) if data.get("sha") else None,
            size=len(decoded),
            is_binary=is_binary,
            content=text,
        )

    def list_pull_requests(
        self,
        owner: str,
        repo: str,
        *,
        state: str = "open",
        per_page: int = DEFAULT_PER_PAGE,
        max_items: int = DEFAULT_MAX_ITEMS,
    ) -> List[GitHubPullRequest]:
        """Retrieve pull requests with bounded pagination."""
        owner = _validate_owner(owner)
        repo = _validate_repo(repo)
        state = _validate_state(state)
        entries = self._paginate(
            f"/repos/{owner}/{repo}/pulls",
            params={"state": state},
            per_page=per_page,
            max_items=max_items,
        )
        pull_requests: List[GitHubPullRequest] = []
        for entry in entries:
            number = entry.get("number")
            if not isinstance(number, int):
                continue
            user_block = entry.get("user") if isinstance(entry.get("user"), dict) else {}
            head_block = entry.get("head") if isinstance(entry.get("head"), dict) else {}
            base_block = entry.get("base") if isinstance(entry.get("base"), dict) else {}
            pull_requests.append(
                GitHubPullRequest(
                    number=number,
                    title=str(entry.get("title") or ""),
                    state=str(entry.get("state") or ""),
                    author_login=str(user_block.get("login")) if user_block.get("login") else None,
                    head_sha=str(head_block.get("sha") or ""),
                    base_branch=str(base_block.get("ref") or ""),
                    draft=bool(entry.get("draft", False)),
                    html_url=str(entry.get("html_url") or ""),
                    updated_at=str(entry.get("updated_at")) if entry.get("updated_at") else None,
                )
            )
        return pull_requests


__all__ = [
    # Configuration
    "ENV_GITHUB_TOKEN",
    "GITHUB_API_BASE_URL",
    "GITHUB_API_HOST",
    "GITHUB_API_SCHEME",
    "GITHUB_API_PORT",
    "GITHUB_API_VERSION",
    # Client
    "GitHubClient",
    # Response models
    "GitHubRepository",
    "GitHubBranch",
    "GitHubCommit",
    "GitHubFileContent",
    "GitHubPullRequest",
    # Errors
    "GitHubError",
    "GitHubConfigurationError",
    "GitHubValidationError",
    "GitHubAuthenticationError",
    "GitHubPermissionError",
    "GitHubNotFoundError",
    "GitHubRateLimitError",
    "GitHubUnavailableError",
    "GitHubInvalidResponseError",
    # Utilities
    "redact_secret",
]
