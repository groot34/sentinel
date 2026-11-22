"""Deterministic GitHub repository context producer (Mission 27.3.3, Phase 1).

This module retrieves a bounded set of source files from a connected GitHub
repository and turns deterministic findings in the retrieved content into
Sentinel ``EV-CODE-*`` evidence items carrying complete source provenance.

Design rules
------------
* **Read-only.** The incident bundle is opened for reading only and is never
  written, moved or re-created. No repository clones, no branch/commit/file
  writes, no patch application.
* **Deterministic.** No language-model calls and no interpretation are
  performed here. Findings come exclusively from the existing deterministic
  pattern detectors shared with the bundle-based Code Agent.
* **Revision-pinned.** The requested ref is resolved to a single commit SHA
  once, and that immutable SHA is used for every subsequent file retrieval so
  the produced evidence can never mix revisions.
* **Bounded.** At most :data:`MAX_CANDIDATE_FILES` files, at most
  :data:`MAX_FILE_BYTES` per file and at most :data:`MAX_TOTAL_BYTES` of decoded
  content per collection. Requests are never retried.
* **Grounded.** Every evidence item is derived from content actually retrieved
  at the resolved revision; its excerpt is verbatim retrieved text and its
  reference only ever names real files and real line numbers.
* **Non-fatal.** Retrieval problems are reported structurally in the result.
  The caller decides how to record them and continue a bundle-only
  investigation; nothing here fails an investigation by itself.
* **Reuses existing protections.** Transport, origin pinning, timeouts, path
  validation, size limits and binary/encoding detection are all enforced by the
  existing read-only :class:`core.integrations.github_client.GitHubClient`.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple, Union

# The deterministic detectors are reused unchanged so a GitHub-derived finding
# has exactly the same shape as the equivalent bundle-derived finding.
from agents.code_tools import (
    DIFF_PATH,
    _ast_patterns,
    _dedupe_findings,
    _line_patterns,
    iter_source_files,
    load_git_diff,
)
from core.integrations.github_client import (
    GitHubAuthenticationError,
    GitHubConfigurationError,
    GitHubError,
    GitHubInvalidResponseError,
    GitHubNotFoundError,
    GitHubPermissionError,
    GitHubRateLimitError,
    GitHubUnavailableError,
    GitHubValidationError,
    redact_secret,
)
from core.integrations.repository_store import GitHubConnection


# ============================================================================
# Limits
# ============================================================================

#: Maximum number of candidate files retrieved in a single collection.
MAX_CANDIDATE_FILES = 20

#: Maximum decoded size of a single retrieved file (1 MiB).
MAX_FILE_BYTES = 1_048_576

#: Maximum total decoded content retrieved in a single collection (2 MiB).
MAX_TOTAL_BYTES = 2_097_152

#: Maximum characters kept from a single evidence excerpt. Longer excerpts are
#: clipped on a line boundary; clipping only ever removes text.
MAX_EXCERPT_CHARS = 2000

#: File extensions treated as configuration rather than code evidence.
CONFIG_EXTENSIONS = frozenset(
    {".yml", ".yaml", ".sql", ".toml", ".ini", ".cfg", ".conf", ".env", ".properties"}
)

#: Source URL template, always pinned to the resolved commit SHA.
BLOB_URL_TEMPLATE = "https://github.com/{owner}/{repo}/blob/{sha}/{path}"

#: Maximum characters of a SHA used in human-readable descriptions.
_SHORT_SHA_LENGTH = 12


# ============================================================================
# Reason / error codes
# ============================================================================

# Skipped-file reasons.
SKIP_UNSAFE_PATH = "unsafe_path"
SKIP_MISSING = "missing"
SKIP_BINARY = "binary"
SKIP_UNREADABLE = "unreadable"
SKIP_OVERSIZED = "oversized"
SKIP_TOTAL_LIMIT = "total_limit"

# Truncation reasons.
TRUNCATED_MAX_FILES = "max_files"
TRUNCATED_TOTAL_LIMIT = "max_total_bytes"

# Structured error reasons returned instead of raising.
ERROR_CONFIGURATION = "configuration_error"
ERROR_INVALID_REF = "invalid_ref"
ERROR_UNRESOLVED_REF = "unresolved_ref"
ERROR_NOT_FOUND = "not_found"
ERROR_AUTHENTICATION = "authentication_failed"
ERROR_PERMISSION = "permission_denied"
ERROR_RATE_LIMITED = "rate_limited"
ERROR_UNAVAILABLE = "unavailable"
ERROR_INVALID_RESPONSE = "invalid_response"
ERROR_UNEXPECTED = "unexpected"

#: GitHub error type -> structured reason code, for revision resolution.
_RESOLUTION_ERROR_CODES: Tuple[Tuple[type, str], ...] = (
    (GitHubConfigurationError, ERROR_CONFIGURATION),
    (GitHubRateLimitError, ERROR_RATE_LIMITED),
    (GitHubAuthenticationError, ERROR_AUTHENTICATION),
    (GitHubPermissionError, ERROR_PERMISSION),
    (GitHubNotFoundError, ERROR_NOT_FOUND),
    (GitHubValidationError, ERROR_INVALID_REF),
    (GitHubUnavailableError, ERROR_UNAVAILABLE),
    (GitHubInvalidResponseError, ERROR_INVALID_RESPONSE),
)

#: Retrieval-phase failures that stop the whole collection immediately.
_FATAL_RETRIEVAL_ERRORS: Tuple[Tuple[type, str], ...] = (
    (GitHubConfigurationError, ERROR_CONFIGURATION),
    (GitHubRateLimitError, ERROR_RATE_LIMITED),
    (GitHubAuthenticationError, ERROR_AUTHENTICATION),
    (GitHubPermissionError, ERROR_PERMISSION),
    (GitHubUnavailableError, ERROR_UNAVAILABLE),
)


def _utcnow_iso() -> str:
    """Return the current UTC time as an ISO-8601 string."""
    return datetime.now(tz=timezone.utc).isoformat()


# ============================================================================
# Helpers
# ============================================================================

def _error_code_for(exc: BaseException, table: Tuple[Tuple[type, str], ...], default: str) -> str:
    """Map an exception onto a structured reason code from *table*."""
    for exc_type, code in table:
        if isinstance(exc, exc_type):
            return code
    return default


def _is_config_path(path: str) -> bool:
    """Return True when *path* names a configuration file rather than source."""
    return Path(path).suffix.lower() in CONFIG_EXTENSIONS


def is_safe_repo_path(path: Any) -> bool:
    """Return True when *path* is a safe repository-relative POSIX path.

    Mirrors the read-only client's path policy (no absolute paths, no parent
    traversal, no backslashes, no NUL or control characters, no empty/dot
    segments) so unsafe candidates are rejected before any request is issued.
    The client validates the path again independently as defence in depth.
    """
    if not isinstance(path, str):
        return False
    candidate = path.strip()
    if not candidate or candidate.startswith("/") or candidate.endswith("/"):
        return False
    if "\\" in candidate or "\x00" in candidate:
        return False
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in candidate):
        return False
    if any(segment in ("", ".", "..") for segment in candidate.split("/")):
        return False
    return True


def _line_anchor(reference: str) -> Optional[str]:
    """Extract a ``#L..`` anchor from a ``path:start[-end]`` reference."""
    if ":" not in reference:
        return None
    tail = reference.rsplit(":", 1)[1].strip()
    match = re.fullmatch(r"(\d+)(?:-(\d+))?", tail)
    if match is None:
        return None
    start = match.group(1)
    end = match.group(2)
    if end and end != start:
        return f"#L{start}-L{end}"
    return f"#L{start}"


def _clip_excerpt(text: str, limit: int = MAX_EXCERPT_CHARS) -> str:
    """Return a line-aligned prefix of *text* no longer than *limit* characters."""
    if len(text) <= limit:
        return text
    clipped = text[:limit]
    newline = clipped.rfind("\n")
    if newline > 0:
        clipped = clipped[:newline]
    return clipped


def _blob_url(owner: str, repo: str, sha: str, path: str) -> str:
    """Return the GitHub blob URL for *path* pinned to the commit *sha*."""
    return BLOB_URL_TEMPLATE.format(owner=owner, repo=repo, sha=sha, path=path)


def _safe_message(exc: BaseException) -> str:
    """Return a credential-safe description of *exc*."""
    return redact_secret(f"{type(exc).__name__}: {exc}")


# ============================================================================
# Result models
# ============================================================================

@dataclass(frozen=True)
class SkippedFile:
    """A candidate file that was not turned into evidence, and why."""

    path: str
    reason: str
    detail: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {"path": self.path, "reason": self.reason, "detail": self.detail}


@dataclass(frozen=True)
class RetrievedFile:
    """A file successfully retrieved at the pinned revision."""

    path: str
    size: int
    content_hash: str
    line_count: int
    source: str
    evidence_ids: Tuple[str, ...] = ()

    def to_dict(self) -> Dict[str, Any]:
        return {
            "path": self.path,
            "size": self.size,
            "content_hash": self.content_hash,
            "line_count": self.line_count,
            "source": self.source,
            "evidence_ids": list(self.evidence_ids),
        }


@dataclass(frozen=True)
class GitHubContextError:
    """Structured, non-fatal failure information."""

    reason: str
    message: str

    def to_dict(self) -> Dict[str, Any]:
        return {"reason": self.reason, "message": self.message}


@dataclass(frozen=True)
class GitHubContextResult:
    """Deterministic outcome of one GitHub repository context collection.

    The result is fully JSON-serialisable via :meth:`to_dict` and never contains
    a credential value: only the non-secret connection metadata and repository
    provenance are retained.
    """

    owner: str
    repo: str
    connection_id: str
    requested_ref: str
    commit_sha: str
    retrieved_at: str
    evidence: List[Dict[str, Any]] = field(default_factory=list)
    files: List[RetrievedFile] = field(default_factory=list)
    skipped: List[SkippedFile] = field(default_factory=list)
    total_bytes: int = 0
    truncated: bool = False
    truncation_reason: Optional[str] = None
    error: Optional[GitHubContextError] = None

    @property
    def retrieved_file_count(self) -> int:
        """Number of files successfully retrieved at the pinned revision."""
        return len(self.files)

    @property
    def ok(self) -> bool:
        """True when no structured error was recorded."""
        return self.error is None

    def to_dict(self) -> Dict[str, Any]:
        """Return a JSON-serialisable representation of the result."""
        return {
            "provider": "github",
            "owner": self.owner,
            "repo": self.repo,
            "connection_id": self.connection_id,
            "requested_ref": self.requested_ref,
            "commit_sha": self.commit_sha,
            "retrieved_at": self.retrieved_at,
            "evidence": [dict(item) for item in self.evidence],
            "files": [item.to_dict() for item in self.files],
            "skipped": [item.to_dict() for item in self.skipped],
            "retrieved_file_count": self.retrieved_file_count,
            "total_bytes": self.total_bytes,
            "truncated": self.truncated,
            "truncation_reason": self.truncation_reason,
            "error": self.error.to_dict() if self.error is not None else None,
        }


# ============================================================================
# Producer
# ============================================================================

class GitHubContextProducer:
    """Deterministic, bounded GitHub repository context producer.

    The producer owns no resources: it borrows the caller's read-only
    :class:`~core.integrations.github_client.GitHubClient` and never closes it.
    """

    def __init__(
        self,
        client: Any,
        *,
        max_files: int = MAX_CANDIDATE_FILES,
        max_file_bytes: int = MAX_FILE_BYTES,
        max_total_bytes: int = MAX_TOTAL_BYTES,
        start_index: int = 1,
        now: Optional[Callable[[], str]] = None,
    ) -> None:
        """
        Args:
            client: A configured read-only GitHub client (``GitHubClient``).
            max_files: Maximum candidate files retrieved per collection.
            max_file_bytes: Maximum decoded size accepted per file.
            max_total_bytes: Maximum total decoded content per collection.
            start_index: First numeric suffix used for generated evidence IDs.
                Pass ``highest_existing_bundle_index + 1`` to continue the
                bundle's ``EV-CODE-*`` numbering without collisions.
            now: Optional clock returning an ISO-8601 timestamp (for tests).
        """
        self._client = client
        self._max_files = max(1, int(max_files))
        self._max_file_bytes = max(1, int(max_file_bytes))
        self._max_total_bytes = max(1, int(max_total_bytes))
        start = int(start_index)
        if start < 1:
            raise ValueError("start_index must be a positive integer")
        self._start_index = start
        self._now = now or _utcnow_iso

    # -- public API ---------------------------------------------------------

    def collect(
        self,
        connection: GitHubConnection,
        incident_dir: Union[str, Path],
        ref: Optional[str] = None,
    ) -> GitHubContextResult:
        """Retrieve bounded repository context for *incident_dir*.

        Args:
            connection: Validated, non-secret connection record to read from.
            incident_dir: Incident bundle directory (read-only).
            ref: Optional branch, tag or commit SHA. Defaults to the
                connection's configured default branch.

        Returns:
            A :class:`GitHubContextResult`. Retrieval and GitHub failures are
            reported in ``result.error`` / ``result.skipped`` rather than raised.

        Raises:
            FileNotFoundError: The incident directory does not exist.
            NotADirectoryError: The incident path is not a directory.
            ValueError: ``start_index`` was invalid (constructor-time only).
        """
        incident_path = Path(incident_dir)
        if not incident_path.exists():
            raise FileNotFoundError(f"Incident directory not found: {incident_dir}")
        if not incident_path.is_dir():
            raise NotADirectoryError(f"Incident path is not a directory: {incident_dir}")

        retrieved_at = self._now()
        requested_ref = self._resolve_requested_ref(connection, ref)

        revision = self._resolve_revision(connection, requested_ref, retrieved_at)
        if isinstance(revision, GitHubContextResult):
            return revision
        commit_sha = revision

        base = {
            "owner": connection.owner,
            "repo": connection.repo,
            "connection_id": connection.connection_id,
            "requested_ref": requested_ref,
            "commit_sha": commit_sha,
            "retrieved_at": retrieved_at,
        }

        candidates, unsafe, truncated, truncation_reason = self._candidate_paths(incident_path)
        skipped: List[SkippedFile] = list(unsafe)

        evidence: List[Dict[str, Any]] = []
        files: List[RetrievedFile] = []
        total_bytes = 0
        error: Optional[GitHubContextError] = None
        next_index = self._start_index

        for path in candidates:
            if total_bytes >= self._max_total_bytes:
                truncated = True
                truncation_reason = TRUNCATED_TOTAL_LIMIT
                break

            try:
                content = self._client.get_file_contents(
                    connection.owner, connection.repo, path, ref=commit_sha
                )
            except GitHubRateLimitError as exc:
                error = GitHubContextError(ERROR_RATE_LIMITED, _safe_message(exc))
                break
            except GitHubValidationError as exc:
                skipped.append(SkippedFile(path, SKIP_UNSAFE_PATH, _safe_message(exc)))
                continue
            except GitHubNotFoundError as exc:
                skipped.append(SkippedFile(path, SKIP_MISSING, _safe_message(exc)))
                continue
            except GitHubInvalidResponseError as exc:
                reason = (
                    SKIP_OVERSIZED if "maximum allowed size" in str(exc) else SKIP_UNREADABLE
                )
                skipped.append(SkippedFile(path, reason, _safe_message(exc)))
                continue
            except GitHubError as exc:
                fatal = _error_code_for(exc, _FATAL_RETRIEVAL_ERRORS, ERROR_UNEXPECTED)
                error = GitHubContextError(fatal, _safe_message(exc))
                break

            if content.is_binary or content.content is None:
                skipped.append(SkippedFile(path, SKIP_BINARY, None))
                continue

            payload = content.content.encode("utf-8")
            if len(payload) > self._max_file_bytes:
                skipped.append(SkippedFile(path, SKIP_OVERSIZED, None))
                continue
            if total_bytes + len(payload) > self._max_total_bytes:
                skipped.append(SkippedFile(path, SKIP_TOTAL_LIMIT, None))
                truncated = True
                truncation_reason = TRUNCATED_TOTAL_LIMIT
                break

            total_bytes += len(payload)
            content_hash = f"sha256:{hashlib.sha256(payload).hexdigest()}"
            source = "config" if _is_config_path(path) else "code"

            file_evidence_ids: List[str] = []
            for finding in self._findings_for(path, content.content, source):
                item = self._build_evidence(
                    path=path,
                    finding=finding,
                    connection=connection,
                    requested_ref=requested_ref,
                    commit_sha=commit_sha,
                    retrieved_at=retrieved_at,
                    content_hash=content_hash,
                    evidence_id=f"EV-CODE-{next_index:03d}",
                )
                next_index += 1
                file_evidence_ids.append(item["evidence_id"])
                evidence.append(item)

            files.append(
                RetrievedFile(
                    path=path,
                    size=len(payload),
                    content_hash=content_hash,
                    line_count=len(content.content.splitlines()),
                    source=source,
                    evidence_ids=tuple(file_evidence_ids),
                )
            )

        return GitHubContextResult(
            evidence=evidence,
            files=files,
            skipped=skipped,
            total_bytes=total_bytes,
            truncated=truncated,
            truncation_reason=truncation_reason,
            error=error,
            **base,
        )

    # -- revision resolution ------------------------------------------------

    @staticmethod
    def _resolve_requested_ref(connection: GitHubConnection, ref: Optional[str]) -> str:
        """Return the requested ref, defaulting to the repository default branch."""
        if ref is not None and str(ref).strip():
            return str(ref).strip()
        return connection.default_branch

    def _resolve_revision(
        self,
        connection: GitHubConnection,
        requested_ref: str,
        retrieved_at: str,
    ) -> Union[str, GitHubContextResult]:
        """Resolve *requested_ref* to an immutable commit SHA.

        Returns the SHA on success, or a fully-populated failure result.
        """
        def failure(reason: str, message: str) -> GitHubContextResult:
            return GitHubContextResult(
                owner=connection.owner,
                repo=connection.repo,
                connection_id=connection.connection_id,
                requested_ref=requested_ref,
                commit_sha="",
                retrieved_at=retrieved_at,
                error=GitHubContextError(reason, message),
            )

        try:
            commit = self._client.get_commit(connection.owner, connection.repo, requested_ref)
        except GitHubError as exc:
            code = _error_code_for(exc, _RESOLUTION_ERROR_CODES, ERROR_UNEXPECTED)
            return failure(code, _safe_message(exc))

        sha = str(getattr(commit, "sha", "") or "").strip()
        if not sha:
            return failure(
                ERROR_UNRESOLVED_REF,
                f"Could not resolve ref {requested_ref!r} to a commit SHA.",
            )
        return sha

    # -- candidate discovery ------------------------------------------------

    def _candidate_paths(
        self, incident_path: Path
    ) -> Tuple[List[str], List[SkippedFile], bool, Optional[str]]:
        """Derive safe, de-duplicated repository-relative candidate paths.

        Candidates come only from the incident bundle: the existing ``service/``
        source-file convention and the file paths named by ``git_diff.patch``.
        No repository tree listing is performed.
        """
        ordered: List[str] = []
        seen: set[str] = set()
        skipped: List[SkippedFile] = []

        raw: List[str] = []
        for source_file in iter_source_files(incident_path):
            try:
                raw.append(Path(source_file).relative_to(incident_path).as_posix())
            except ValueError:  # pragma: no cover - defensive
                continue

        diff_path = incident_path / DIFF_PATH
        if diff_path.is_file():
            diff = load_git_diff(diff_path)
            raw.extend(entry for entry in diff.changed_files if isinstance(entry, str))

        for candidate in raw:
            if not is_safe_repo_path(candidate):
                skipped.append(SkippedFile(str(candidate), SKIP_UNSAFE_PATH, None))
                continue
            normalised = candidate.strip()
            if normalised in seen:
                continue
            seen.add(normalised)
            ordered.append(normalised)

        truncated = False
        truncation_reason: Optional[str] = None
        if len(ordered) > self._max_files:
            ordered = ordered[: self._max_files]
            truncated = True
            truncation_reason = TRUNCATED_MAX_FILES

        return ordered, skipped, truncated, truncation_reason

    # -- evidence generation ------------------------------------------------

    @staticmethod
    def _findings_for(path: str, text: str, source: str):
        """Return deterministic findings detected in retrieved *text*."""
        lines = text.splitlines()
        findings: List[Any] = []
        if path.endswith(".py"):
            findings.extend(_ast_patterns(path, text, lines))
            findings.extend(_line_patterns(path, "code", lines, source_kind="code"))
        else:
            findings.extend(_line_patterns(path, source, lines, source_kind=source))
        return _dedupe_findings(findings)

    @staticmethod
    def _build_evidence(
        *,
        path: str,
        finding: Any,
        connection: GitHubConnection,
        requested_ref: str,
        commit_sha: str,
        retrieved_at: str,
        content_hash: str,
        evidence_id: str,
    ) -> Dict[str, Any]:
        """Build one ``EV-CODE-*`` evidence item with full source provenance."""
        anchor = _line_anchor(finding.reference)
        source_url = _blob_url(connection.owner, connection.repo, commit_sha, path)
        if anchor:
            source_url = f"{source_url}{anchor}"

        pattern = (finding.metadata or {}).get("pattern")
        short_sha = commit_sha[:_SHORT_SHA_LENGTH]
        if pattern:
            interpretation = (
                f"Deterministic pattern '{pattern}' detected in repository file "
                f"{path} retrieved at commit {short_sha}."
            )
        else:
            interpretation = f"Retrieved repository file {path} at commit {short_sha}."

        return {
            "evidence_id": evidence_id,
            "source": finding.source,
            "reference": finding.reference,
            "type": finding.finding_type,
            "excerpt": _clip_excerpt(finding.excerpt),
            "interpretation": interpretation,
            "github": {
                "owner": connection.owner,
                "repo": connection.repo,
                "connection_id": connection.connection_id,
                "ref": requested_ref,
                "commit_sha": commit_sha,
                "path": path,
                "source_url": source_url,
                "retrieved_at": retrieved_at,
                "content_hash": content_hash,
            },
        }


def collect_github_context(
    client: Any,
    connection: GitHubConnection,
    incident_dir: Union[str, Path],
    ref: Optional[str] = None,
    *,
    start_index: int = 1,
    max_files: int = MAX_CANDIDATE_FILES,
    max_file_bytes: int = MAX_FILE_BYTES,
    max_total_bytes: int = MAX_TOTAL_BYTES,
    now: Optional[Callable[[], str]] = None,
) -> GitHubContextResult:
    """Convenience wrapper around :class:`GitHubContextProducer`."""
    producer = GitHubContextProducer(
        client,
        max_files=max_files,
        max_file_bytes=max_file_bytes,
        max_total_bytes=max_total_bytes,
        start_index=start_index,
        now=now,
    )
    return producer.collect(connection, incident_dir, ref=ref)


__all__ = [
    "BLOB_URL_TEMPLATE",
    "CONFIG_EXTENSIONS",
    "ERROR_AUTHENTICATION",
    "ERROR_CONFIGURATION",
    "ERROR_INVALID_REF",
    "ERROR_INVALID_RESPONSE",
    "ERROR_NOT_FOUND",
    "ERROR_PERMISSION",
    "ERROR_RATE_LIMITED",
    "ERROR_UNAVAILABLE",
    "ERROR_UNEXPECTED",
    "ERROR_UNRESOLVED_REF",
    "GitHubContextError",
    "GitHubContextProducer",
    "GitHubContextResult",
    "MAX_CANDIDATE_FILES",
    "MAX_EXCERPT_CHARS",
    "MAX_FILE_BYTES",
    "MAX_TOTAL_BYTES",
    "RetrievedFile",
    "SKIP_BINARY",
    "SKIP_MISSING",
    "SKIP_OVERSIZED",
    "SKIP_TOTAL_LIMIT",
    "SKIP_UNREADABLE",
    "SKIP_UNSAFE_PATH",
    "SkippedFile",
    "TRUNCATED_MAX_FILES",
    "TRUNCATED_TOTAL_LIMIT",
    "collect_github_context",
    "is_safe_repo_path",
]
