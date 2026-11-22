"""Focused tests for the deterministic GitHub context producer (Mission 27.3.3).

These tests never touch the network: every request is served by an
``httpx.MockTransport`` handler and no real GitHub token is required.

Coverage:
* Default branch and explicit ref resolution.
* Commit SHA pinning across every file request.
* Candidate discovery from ``service/`` and ``git_diff.patch``.
* Duplicate elimination and unsafe path rejection.
* Missing, binary, oversized and unreadable files.
* Per-file and total context limits, and file-count truncation.
* Rate-limit early termination and no retries.
* Evidence ID allocation, uniqueness and bundle-offset continuation.
* Source URLs and provenance.
* Content hashes matching retrieved content.
* Absence of language-model usage and of incident-bundle mutation.
* Structured failure reporting for every GitHub failure class.
"""

from __future__ import annotations

import base64
import hashlib
import inspect
import json
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import httpx
import pytest

import agents.github_context as github_context
from agents.github_context import (
    ERROR_AUTHENTICATION,
    ERROR_INVALID_REF,
    ERROR_NOT_FOUND,
    ERROR_PERMISSION,
    ERROR_RATE_LIMITED,
    ERROR_UNAVAILABLE,
    ERROR_UNRESOLVED_REF,
    MAX_CANDIDATE_FILES,
    SKIP_BINARY,
    SKIP_MISSING,
    SKIP_OVERSIZED,
    SKIP_TOTAL_LIMIT,
    SKIP_UNSAFE_PATH,
    TRUNCATED_MAX_FILES,
    TRUNCATED_TOTAL_LIMIT,
    GitHubContextProducer,
    GitHubContextResult,
    collect_github_context,
    is_safe_repo_path,
)
from core.integrations.github_client import GitHubClient
from core.integrations.repository_store import GitHubConnection

#: A distinctive, well-formed classic PAT-style token used throughout.
TOKEN = "ghp_" + "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q7R8"

#: The immutable commit SHA every retrieval must be pinned to.
COMMIT_SHA = "c0ffee1234567890abcdef1234567890abcdef12"

CONNECTION_ID = "gh-octo-demo-0badc0ffee"

#: Source content that the shared deterministic detectors flag twice
#: (``high_retry_count`` and ``long_timeout``).
PATTERN_SOURCE = "MAX_RETRIES = 7\nTIMEOUT = 60\n"

#: Source content that the deterministic detectors do not flag.
CLEAN_SOURCE = "def add(a, b):\n    return a + b\n"

Handler = Callable[[httpx.Request], httpx.Response]


# ============================================================================
# Fixtures and helpers
# ============================================================================

def _connection(default_branch: str = "main") -> GitHubConnection:
    return GitHubConnection(
        connection_id=CONNECTION_ID,
        label="demo",
        owner="octo",
        repo="demo",
        default_branch=default_branch,
    )


def _commit_payload(sha: str = COMMIT_SHA) -> Dict[str, Any]:
    return {
        "sha": sha,
        "commit": {"message": "Add auth", "author": {"name": "Dev", "date": "2026-01-01T00:00:00Z"}},
        "author": {"login": "octo"},
        "html_url": "https://github.com/octo/demo/commit/" + sha,
    }


def _file_payload(path: str, text: str) -> Dict[str, Any]:
    data = text.encode("utf-8")
    return {
        "type": "file",
        "path": path,
        "size": len(data),
        "sha": "blob" + hashlib.sha1(data).hexdigest()[:12],
        "encoding": "base64",
        "content": base64.b64encode(data).decode("ascii"),
    }


def _binary_file_payload(path: str, raw: bytes) -> Dict[str, Any]:
    return {
        "type": "file",
        "path": path,
        "size": len(raw),
        "sha": "blob" + hashlib.sha1(raw).hexdigest()[:12],
        "encoding": "base64",
        "content": base64.b64encode(raw).decode("ascii"),
    }


def _client(handler: Handler) -> GitHubClient:
    return GitHubClient(token=TOKEN, transport=httpx.MockTransport(handler))


def _handler(
    files: Optional[Dict[str, Any]] = None,
    *,
    requests: Optional[List[httpx.Request]] = None,
    commit_sha: str = COMMIT_SHA,
    commit_status: int = 200,
    commit_body: Optional[Dict[str, Any]] = None,
    contents_status: Optional[Callable[[str], Optional[int]]] = None,
) -> Handler:
    """Build a MockTransport handler serving a repo, a revision and its files.

    ``files`` maps repository-relative paths to either source text (``str``) or
    a raw payload mapping (used for binary content).
    """
    table = files or {}

    def handler(request: httpx.Request) -> httpx.Response:
        if requests is not None:
            requests.append(request)
        path = request.url.path
        if "/commits/" in path:
            if commit_status != 200:
                return httpx.Response(commit_status, json={"message": "error"})
            body = commit_body if commit_body is not None else _commit_payload(commit_sha)
            return httpx.Response(200, json=body)
        if "/contents/" in path:
            relative = path.split("/contents/", 1)[1]
            if contents_status is not None:
                status = contents_status(relative)
                if status is not None:
                    return httpx.Response(status, json={"message": "error"})
            payload = table.get(relative)
            if isinstance(payload, str):
                return httpx.Response(200, json=_file_payload(relative, payload))
            if isinstance(payload, dict):
                return httpx.Response(200, json=payload)
            return httpx.Response(404, json={"message": "Not Found"})
        return httpx.Response(404, json={"message": "Not Found"})

    return handler


def _bundle(
    tmp_path: Path,
    *,
    service: Optional[Dict[str, str]] = None,
    diff: Optional[str] = None,
) -> Path:
    root = tmp_path / "incident-001"
    (root / "service").mkdir(parents=True, exist_ok=True)
    for name, content in (service or {}).items():
        target = root / "service" / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    if diff is not None:
        (root / "git_diff.patch").write_text(diff, encoding="utf-8")
    return root


def _contents_paths(requests: List[httpx.Request]) -> List[str]:
    return [
        request.url.path.split("/contents/", 1)[1]
        for request in requests
        if "/contents/" in request.url.path
    ]


def _contents_refs(requests: List[httpx.Request]) -> List[Optional[str]]:
    return [
        request.url.params.get("ref")
        for request in requests
        if "/contents/" in request.url.path
    ]


def _commit_requests(requests: List[httpx.Request]) -> List[httpx.Request]:
    return [request for request in requests if "/commits/" in request.url.path]


# ============================================================================
# Revision resolution
# ============================================================================

def test_default_branch_resolution(tmp_path: Path) -> None:
    requests: List[httpx.Request] = []
    bundle = _bundle(tmp_path, service={"app.py": PATTERN_SOURCE})
    client = _client(_handler({"service/app.py": PATTERN_SOURCE}, requests=requests))

    result = collect_github_context(client, _connection("release/1.0"), bundle)

    assert isinstance(result, GitHubContextResult)
    assert result.requested_ref == "release/1.0"
    assert result.commit_sha == COMMIT_SHA
    assert result.ok
    commits = _commit_requests(requests)
    assert len(commits) == 1
    assert commits[0].url.path.endswith("/repos/octo/demo/commits/release/1.0")


def test_explicit_ref_resolution_overrides_default_branch(tmp_path: Path) -> None:
    requests: List[httpx.Request] = []
    bundle = _bundle(tmp_path, service={"app.py": PATTERN_SOURCE})
    client = _client(_handler({"service/app.py": PATTERN_SOURCE}, requests=requests))

    result = collect_github_context(client, _connection("main"), bundle, ref="feature/add-auth")

    assert result.requested_ref == "feature/add-auth"
    commits = _commit_requests(requests)
    assert len(commits) == 1
    # The client percent-encodes the slash exactly once.
    assert commits[0].url.raw_path.decode().endswith("/commits/feature%2Fadd-auth")


def test_commit_sha_is_pinned_for_every_file_request(tmp_path: Path) -> None:
    requests: List[httpx.Request] = []
    files = {"service/app.py": PATTERN_SOURCE, "service/worker.py": PATTERN_SOURCE}
    bundle = _bundle(tmp_path, service={"app.py": PATTERN_SOURCE, "worker.py": PATTERN_SOURCE})
    client = _client(_handler(files, requests=requests))

    result = collect_github_context(client, _connection(), bundle)

    assert result.retrieved_file_count == 2
    refs = _contents_refs(requests)
    assert refs, "expected at least one contents request"
    assert all(ref == COMMIT_SHA for ref in refs)


def test_resolved_sha_differs_from_requested_ref_is_used(tmp_path: Path) -> None:
    requests: List[httpx.Request] = []
    resolved = "9f2b0000000000000000000000000000000000aa"
    bundle = _bundle(tmp_path, service={"app.py": PATTERN_SOURCE})
    client = _client(
        _handler({"service/app.py": PATTERN_SOURCE}, requests=requests, commit_sha=resolved)
    )

    result = collect_github_context(client, _connection("main"), bundle)

    assert result.requested_ref == "main"
    assert result.commit_sha == resolved
    assert all(ref == resolved for ref in _contents_refs(requests))


# ============================================================================
# Candidate discovery
# ============================================================================

def test_candidates_come_from_service_and_diff(tmp_path: Path) -> None:
    requests: List[httpx.Request] = []
    diff = (
        "diff --git a/service/worker.py b/service/worker.py\n"
        "--- a/service/worker.py\n"
        "+++ b/service/worker.py\n"
        "@@ -1,2 +1,2 @@\n"
        " import time\n"
        "+TIMEOUT = 120\n"
    )
    bundle = _bundle(tmp_path, service={"app.py": PATTERN_SOURCE}, diff=diff)
    files = {"service/app.py": PATTERN_SOURCE, "service/worker.py": PATTERN_SOURCE}
    client = _client(_handler(files, requests=requests))

    result = collect_github_context(client, _connection(), bundle)

    assert _contents_paths(requests) == ["service/app.py", "service/worker.py"]
    assert [item.path for item in result.files] == ["service/app.py", "service/worker.py"]


def test_duplicate_candidates_are_fetched_once(tmp_path: Path) -> None:
    requests: List[httpx.Request] = []
    diff = (
        "diff --git a/service/app.py b/service/app.py\n"
        "--- a/service/app.py\n"
        "+++ b/service/app.py\n"
        "@@ -1,1 +1,2 @@\n"
        " import os\n"
        "+TIMEOUT = 120\n"
    )
    bundle = _bundle(tmp_path, service={"app.py": PATTERN_SOURCE}, diff=diff)
    client = _client(_handler({"service/app.py": PATTERN_SOURCE}, requests=requests))

    result = collect_github_context(client, _connection(), bundle)

    assert _contents_paths(requests) == ["service/app.py"]
    assert result.retrieved_file_count == 1


def test_unsafe_candidate_paths_are_rejected_without_a_request(tmp_path: Path) -> None:
    requests: List[httpx.Request] = []
    diff = (
        "diff --git a/../secrets.txt b/../secrets.txt\n"
        "--- a/../secrets.txt\n"
        "+++ b/../secrets.txt\n"
        "@@ -1,1 +1,2 @@\n"
        " old\n"
        "+secret\n"
    )
    bundle = _bundle(tmp_path, diff=diff)
    client = _client(_handler({}, requests=requests))

    result = collect_github_context(client, _connection(), bundle)

    assert _contents_paths(requests) == []
    assert [item.reason for item in result.skipped] == [SKIP_UNSAFE_PATH]
    assert result.evidence == []


@pytest.mark.parametrize(
    "candidate",
    ["/etc/passwd", "../secrets.txt", "a/../../b", "a\\b.py", "", "a//b", "service/", None, 7],
)
def test_is_safe_repo_path_rejects_unsafe_values(candidate: Any) -> None:
    assert is_safe_repo_path(candidate) is False


@pytest.mark.parametrize("candidate", ["service/app.py", "a/b/c.txt", "git_diff.patch"])
def test_is_safe_repo_path_accepts_repository_paths(candidate: str) -> None:
    assert is_safe_repo_path(candidate) is True


# ============================================================================
# Bounded retrieval
# ============================================================================

def test_file_count_is_capped_and_reported_as_truncated(tmp_path: Path) -> None:
    requests: List[httpx.Request] = []
    count = MAX_CANDIDATE_FILES + 5
    service = {f"f{index:02d}.py": PATTERN_SOURCE for index in range(count)}
    files = {f"service/f{index:02d}.py": PATTERN_SOURCE for index in range(count)}
    bundle = _bundle(tmp_path, service=service)
    client = _client(_handler(files, requests=requests))

    result = collect_github_context(client, _connection(), bundle)

    assert len(_contents_paths(requests)) == MAX_CANDIDATE_FILES
    assert result.retrieved_file_count == MAX_CANDIDATE_FILES
    assert result.truncated is True
    assert result.truncation_reason == TRUNCATED_MAX_FILES


def test_oversized_file_is_skipped_by_the_producer_limit(tmp_path: Path) -> None:
    bundle = _bundle(tmp_path, service={"app.py": PATTERN_SOURCE})
    client = _client(_handler({"service/app.py": PATTERN_SOURCE}))
    producer = GitHubContextProducer(client, max_file_bytes=10)

    result = producer.collect(_connection(), bundle)

    assert result.retrieved_file_count == 0
    assert [item.reason for item in result.skipped] == [SKIP_OVERSIZED]
    assert result.total_bytes == 0


def test_file_larger_than_the_client_limit_is_skipped_as_oversized(tmp_path: Path) -> None:
    bundle = _bundle(tmp_path, service={"app.py": PATTERN_SOURCE})
    huge = "x" * (1_048_576 + 10)
    client = _client(_handler({"service/app.py": huge}))

    result = collect_github_context(client, _connection(), bundle)

    assert [item.reason for item in result.skipped] == [SKIP_OVERSIZED]
    assert result.evidence == []


def test_total_context_limit_stops_retrieval(tmp_path: Path) -> None:
    requests: List[httpx.Request] = []
    files = {
        "service/a.py": PATTERN_SOURCE,
        "service/b.py": PATTERN_SOURCE,
        "service/c.py": PATTERN_SOURCE,
    }
    bundle = _bundle(
        tmp_path,
        service={"a.py": PATTERN_SOURCE, "b.py": PATTERN_SOURCE, "c.py": PATTERN_SOURCE},
    )
    client = _client(_handler(files, requests=requests))
    producer = GitHubContextProducer(client, max_total_bytes=len(PATTERN_SOURCE.encode()) + 1)

    result = producer.collect(_connection(), bundle)

    assert result.retrieved_file_count == 1
    assert result.truncated is True
    assert result.truncation_reason == TRUNCATED_TOTAL_LIMIT
    assert [item.reason for item in result.skipped] == [SKIP_TOTAL_LIMIT]
    # Retrieval stopped: the third file was never requested.
    assert len(_contents_paths(requests)) == 2


def test_missing_file_is_skipped(tmp_path: Path) -> None:
    bundle = _bundle(tmp_path, service={"app.py": PATTERN_SOURCE, "gone.py": PATTERN_SOURCE})
    client = _client(_handler({"service/app.py": PATTERN_SOURCE}))

    result = collect_github_context(client, _connection(), bundle)

    assert result.retrieved_file_count == 1
    assert [item.reason for item in result.skipped] == [SKIP_MISSING]
    assert result.skipped[0].path == "service/gone.py"


def test_binary_file_is_skipped(tmp_path: Path) -> None:
    bundle = _bundle(tmp_path, service={"app.py": PATTERN_SOURCE})
    client = _client(
        _handler({"service/app.py": _binary_file_payload("service/app.py", b"\xff\xfe\x00\x01")})
    )

    result = collect_github_context(client, _connection(), bundle)

    assert result.retrieved_file_count == 0
    assert [item.reason for item in result.skipped] == [SKIP_BINARY]
    assert result.evidence == []


def test_rate_limit_stops_retrieval_immediately(tmp_path: Path) -> None:
    requests: List[httpx.Request] = []
    bundle = _bundle(
        tmp_path,
        service={"a.py": PATTERN_SOURCE, "b.py": PATTERN_SOURCE, "c.py": PATTERN_SOURCE},
    )
    client = _client(
        _handler({}, requests=requests, contents_status=lambda _path: 429)
    )

    result = collect_github_context(client, _connection(), bundle)

    assert result.error is not None
    assert result.error.reason == ERROR_RATE_LIMITED
    assert len(_contents_paths(requests)) == 1
    assert result.evidence == []


def test_requests_are_not_retried_on_failure(tmp_path: Path) -> None:
    requests: List[httpx.Request] = []
    bundle = _bundle(tmp_path, service={"app.py": PATTERN_SOURCE})
    client = _client(_handler({}, requests=requests, contents_status=lambda _path: 500))

    result = collect_github_context(client, _connection(), bundle)

    assert result.error is not None
    assert result.error.reason == ERROR_UNAVAILABLE
    assert len(_contents_paths(requests)) == 1


# ============================================================================
# Evidence generation
# ============================================================================

def test_evidence_ids_start_at_start_index_and_are_unique(tmp_path: Path) -> None:
    bundle = _bundle(
        tmp_path,
        service={"a.py": PATTERN_SOURCE, "b.py": PATTERN_SOURCE},
    )
    files = {"service/a.py": PATTERN_SOURCE, "service/b.py": PATTERN_SOURCE}
    client = _client(_handler(files))
    producer = GitHubContextProducer(client, start_index=41)

    result = producer.collect(_connection(), bundle)

    ids = [item["evidence_id"] for item in result.evidence]
    assert ids == ["EV-CODE-041", "EV-CODE-042", "EV-CODE-043", "EV-CODE-044"]
    assert len(ids) == len(set(ids))
    assert all(item["evidence_id"].startswith("EV-CODE-") for item in result.evidence)


def test_evidence_is_grounded_in_retrieved_content(tmp_path: Path) -> None:
    bundle = _bundle(tmp_path, service={"app.py": PATTERN_SOURCE})
    client = _client(_handler({"service/app.py": PATTERN_SOURCE}))

    result = collect_github_context(client, _connection(), bundle)

    assert len(result.evidence) == 2
    for item in result.evidence:
        assert item["type"] == "suspicious_pattern"
        assert item["source"] == "code"
        assert item["reference"].startswith("service/app.py:")
        # The excerpt is verbatim retrieved text, never invented.
        assert item["excerpt"] in PATTERN_SOURCE
    assert {item["interpretation"].split("'")[1] for item in result.evidence} == {
        "high_retry_count",
        "long_timeout",
    }


def test_clean_source_produces_no_evidence_but_is_still_retrieved(tmp_path: Path) -> None:
    bundle = _bundle(tmp_path, service={"app.py": CLEAN_SOURCE})
    client = _client(_handler({"service/app.py": CLEAN_SOURCE}))

    result = collect_github_context(client, _connection(), bundle)

    assert result.evidence == []
    assert result.retrieved_file_count == 1
    assert result.files[0].evidence_ids == ()


def test_no_language_model_usage_in_module() -> None:
    source = inspect.getsource(github_context)
    assert "get_llm_client" not in source
    assert "generate_structured" not in source
    assert "llm_client" not in source


# ============================================================================
# Provenance
# ============================================================================

def test_provenance_object_is_complete(tmp_path: Path) -> None:
    bundle = _bundle(tmp_path, service={"app.py": PATTERN_SOURCE})
    client = _client(_handler({"service/app.py": PATTERN_SOURCE}))

    result = collect_github_context(client, _connection("main"), bundle, ref="feature/add-auth")

    assert result.evidence
    for item in result.evidence:
        provenance = item["github"]
        assert provenance["owner"] == "octo"
        assert provenance["repo"] == "demo"
        assert provenance["connection_id"] == CONNECTION_ID
        assert provenance["ref"] == "feature/add-auth"
        assert provenance["commit_sha"] == COMMIT_SHA
        assert provenance["path"] == "service/app.py"
        assert provenance["retrieved_at"] == result.retrieved_at
        assert provenance["content_hash"].startswith("sha256:")
        assert provenance["source_url"].startswith(
            f"https://github.com/octo/demo/blob/{COMMIT_SHA}/service/app.py"
        )


def test_source_url_includes_the_line_anchor(tmp_path: Path) -> None:
    bundle = _bundle(tmp_path, service={"app.py": PATTERN_SOURCE})
    client = _client(_handler({"service/app.py": PATTERN_SOURCE}))

    result = collect_github_context(client, _connection(), bundle)

    urls = {item["github"]["source_url"] for item in result.evidence}
    assert urls == {
        f"https://github.com/octo/demo/blob/{COMMIT_SHA}/service/app.py#L1",
        f"https://github.com/octo/demo/blob/{COMMIT_SHA}/service/app.py#L2",
    }


def test_content_hash_matches_retrieved_content(tmp_path: Path) -> None:
    bundle = _bundle(tmp_path, service={"app.py": PATTERN_SOURCE})
    client = _client(_handler({"service/app.py": PATTERN_SOURCE}))

    result = collect_github_context(client, _connection(), bundle)

    expected = "sha256:" + hashlib.sha256(PATTERN_SOURCE.encode("utf-8")).hexdigest()
    assert result.files[0].content_hash == expected
    assert result.total_bytes == len(PATTERN_SOURCE.encode("utf-8"))
    assert all(item["github"]["content_hash"] == expected for item in result.evidence)


def test_result_is_serialisable_and_credential_free(tmp_path: Path) -> None:
    bundle = _bundle(tmp_path, service={"app.py": PATTERN_SOURCE})
    client = _client(_handler({"service/app.py": PATTERN_SOURCE}))

    result = collect_github_context(client, _connection(), bundle)
    payload = json.dumps(result.to_dict())

    assert TOKEN not in payload
    assert "credential_ref" not in payload
    decoded = json.loads(payload)
    assert decoded["retrieved_file_count"] == 1
    assert decoded["error"] is None


# ============================================================================
# Failure behaviour
# ============================================================================

def test_repository_not_found_returns_structured_error(tmp_path: Path) -> None:
    bundle = _bundle(tmp_path, service={"app.py": PATTERN_SOURCE})
    client = _client(_handler({}, commit_status=404))

    result = collect_github_context(client, _connection(), bundle)

    assert result.evidence == []
    assert result.commit_sha == ""
    assert result.error is not None
    assert result.error.reason == ERROR_NOT_FOUND
    assert result.ok is False


def test_revoked_credentials_return_structured_error(tmp_path: Path) -> None:
    bundle = _bundle(tmp_path, service={"app.py": PATTERN_SOURCE})
    client = _client(_handler({}, commit_status=401))

    result = collect_github_context(client, _connection(), bundle)

    assert result.error is not None
    assert result.error.reason == ERROR_AUTHENTICATION


def test_invalid_ref_returns_structured_error(tmp_path: Path) -> None:
    bundle = _bundle(tmp_path, service={"app.py": PATTERN_SOURCE})
    client = _client(_handler({}, commit_status=422))

    result = collect_github_context(client, _connection(), bundle, ref="bad ref")

    assert result.error is not None
    assert result.error.reason == ERROR_INVALID_REF


def test_unresolved_ref_returns_structured_error(tmp_path: Path) -> None:
    bundle = _bundle(tmp_path, service={"app.py": PATTERN_SOURCE})
    client = _client(_handler({}, commit_body={"commit": {"message": "no sha"}}))

    result = collect_github_context(client, _connection(), bundle)

    assert result.error is not None
    assert result.error.reason == ERROR_UNRESOLVED_REF
    assert result.commit_sha == ""


def test_missing_permissions_return_structured_error(tmp_path: Path) -> None:
    bundle = _bundle(tmp_path, service={"app.py": PATTERN_SOURCE})
    client = _client(
        _handler({"service/app.py": PATTERN_SOURCE}, contents_status=lambda _path: 403)
    )

    result = collect_github_context(client, _connection(), bundle)

    assert result.error is not None
    assert result.error.reason in {ERROR_PERMISSION, ERROR_RATE_LIMITED}


def test_unreadable_file_is_skipped_and_others_continue(tmp_path: Path) -> None:
    bundle = _bundle(tmp_path, service={"a.py": PATTERN_SOURCE, "b.py": PATTERN_SOURCE})

    def contents_status(relative: str) -> Optional[int]:
        return 404 if relative == "service/b.py" else None

    client = _client(
        _handler(
            {"service/a.py": PATTERN_SOURCE, "service/b.py": PATTERN_SOURCE},
            contents_status=contents_status,
        )
    )

    result = collect_github_context(client, _connection(), bundle)

    assert result.retrieved_file_count == 1
    assert [item.reason for item in result.skipped] == [SKIP_MISSING]
    assert result.evidence


def test_missing_incident_directory_raises(tmp_path: Path) -> None:
    client = _client(_handler({}))

    with pytest.raises(FileNotFoundError):
        collect_github_context(client, _connection(), tmp_path / "does-not-exist")


def test_incident_bundle_is_never_modified(tmp_path: Path) -> None:
    diff = (
        "diff --git a/service/app.py b/service/app.py\n"
        "--- a/service/app.py\n"
        "+++ b/service/app.py\n"
        "@@ -1,1 +1,2 @@\n"
        " import os\n"
        "+TIMEOUT = 120\n"
    )
    bundle = _bundle(tmp_path, service={"app.py": PATTERN_SOURCE}, diff=diff)

    def snapshot() -> Dict[str, Any]:
        return {
            str(path.relative_to(bundle)): (
                path.read_bytes(),
                path.stat().st_mtime_ns,
            )
            for path in sorted(bundle.rglob("*"))
            if path.is_file()
        }

    before = snapshot()
    client = _client(_handler({"service/app.py": PATTERN_SOURCE}))

    collect_github_context(client, _connection(), bundle)

    assert snapshot() == before


def test_start_index_must_be_positive(tmp_path: Path) -> None:
    client = _client(_handler({}))
    with pytest.raises(ValueError):
        GitHubContextProducer(client, start_index=0)


# ============================================================================
# Result shape
# ============================================================================

def test_result_reports_repository_identity_and_skipped_details(tmp_path: Path) -> None:
    bundle = _bundle(tmp_path, service={"a.py": PATTERN_SOURCE, "gone.py": PATTERN_SOURCE})
    client = _client(_handler({"service/a.py": PATTERN_SOURCE}))

    result = collect_github_context(client, _connection(), bundle)

    assert (result.owner, result.repo) == ("octo", "demo")
    assert result.connection_id == CONNECTION_ID
    assert result.retrieved_at
    assert result.error is None
    assert result.skipped[0].to_dict()["path"] == "service/gone.py"
    assert result.files[0].to_dict()["line_count"] == PATTERN_SOURCE.count("\n")
