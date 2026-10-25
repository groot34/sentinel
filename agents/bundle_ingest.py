"""Safe incident bundle ingestion for Sentinel 2.0 (Mission 19).

This module owns everything needed to turn a JSON-inline incident bundle
submitted through the API into a validated, atomically published bundle
directory under ``incidents_root``:

* the ingestion **resource limits** (environment-configurable, with documented
  defaults) — file count, per-file bytes and total bytes;
* the **path policy** — a strict allowlist of relative POSIX paths a client may
  submit, rejecting absolute paths, traversal segments, backslashes, drive
  prefixes, NUL bytes, empty segments, over-long / over-deep paths and reserved
  evaluation artefacts;
* a **strict JSON decoder** that rejects duplicate object keys; and
* the **ingestion service** that stages files in a unique temporary directory,
  runs the Mission 18 preflight validator on the staged bundle, and publishes
  a **new** bundle onto ``incidents_root`` safely.

Design rules
------------
* Filesystem operations live here, never in the API route, so the write
  behaviour can be unit tested in isolation.
* Nothing but regular UTF-8 text files is ever written, and every write is
  containment-checked against the freshly created staging root before it
  happens — client-supplied paths and symlinks are never followed.
* Validation and publication are ordered so a client can never observe a
  partially written bundle: the staging directory is removed on every failure
  path and a single rename is the only operation that makes a bundle visible.
* **Replacement is unsupported.** ``overwrite=true`` is rejected outright with
  a stable conflict rather than attempting a best-effort directory swap: there
  is no portable *crash-safe*, cross-process replacement primitive, so Sentinel
  refuses to guess. An incident is created once; a duplicate ID conflicts.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, Mapping, Optional, Tuple

from agents.bundle_validator import require_valid_incident_bundle

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class IngestionError(Exception):
    """Base class for incident-bundle ingestion failures."""


class IngestionDisabledError(IngestionError):
    """Raised when the ingestion endpoint is invoked but not enabled."""


class MalformedRequestError(IngestionError):
    """Raised when the request body is not a well-formed JSON object."""


class DuplicateJsonKeyError(MalformedRequestError):
    """Raised when the request body contains a duplicate JSON object key."""


class InvalidBundlePathError(IngestionError):
    """Raised when a submitted file path violates the ingestion path policy."""


class PayloadTooLargeError(IngestionError):
    """Raised when the submitted bundle exceeds a configured resource limit."""


class IncidentConflictError(IngestionError):
    """Raised when the target incident already exists and overwrite was not allowed."""


class IncidentBusyError(IngestionError):
    """Raised when the target incident is running or actively leased."""


class OverwriteUnsupportedError(IngestionError):
    """Raised when a client requests ``overwrite=true``.

    Replacement is deliberately unsupported: no portable crash-safe,
    cross-process directory-swap primitive exists, so a best-effort swap could
    destroy the previous valid bundle if the process died mid-replacement.
    Sentinel refuses the request with a stable 409 instead of weakening the
    guarantee. A duplicate incident ID conflicts; an incident is created once.
    """


class StorageUnavailableError(IngestionError):
    """Raised when the bundle could not be staged or published on disk."""


# ---------------------------------------------------------------------------
# Resource limits (environment-configurable, documented defaults)
# ---------------------------------------------------------------------------

#: Maximum number of files in one submitted bundle.
DEFAULT_INGEST_MAX_FILES = 200
#: Maximum UTF-8 encoded size of a single file (2 MiB).
DEFAULT_INGEST_MAX_FILE_BYTES = 2 * 1024 * 1024
#: Maximum UTF-8 encoded size of the whole bundle (20 MiB).
DEFAULT_INGEST_MAX_TOTAL_BYTES = 20 * 1024 * 1024
#: Maximum size of the raw JSON request body (24 MiB). Larger than the total
#: content limit so that JSON structural/escaping overhead never rejects a
#: bundle that is itself within the content limits.
DEFAULT_INGEST_MAX_REQUEST_BYTES = 24 * 1024 * 1024

ENV_INGEST_MAX_FILES = "SENTINEL_INGEST_MAX_FILES"
ENV_INGEST_MAX_FILE_BYTES = "SENTINEL_INGEST_MAX_FILE_BYTES"
ENV_INGEST_MAX_TOTAL_BYTES = "SENTINEL_INGEST_MAX_TOTAL_BYTES"
ENV_INGEST_MAX_REQUEST_BYTES = "SENTINEL_INGEST_MAX_REQUEST_BYTES"

#: Opt-in flag that enables the ingestion endpoint. Disabled by default.
ENV_ALLOW_INGESTION = "SENTINEL_ALLOW_INCIDENT_INGESTION"

_TRUE_VALUES = frozenset({"1", "true", "yes", "on"})


@dataclass(frozen=True)
class IngestionLimits:
    """Effective resource limits applied to one submitted bundle."""

    max_files: int = DEFAULT_INGEST_MAX_FILES
    max_file_bytes: int = DEFAULT_INGEST_MAX_FILE_BYTES
    max_total_bytes: int = DEFAULT_INGEST_MAX_TOTAL_BYTES
    max_request_bytes: int = DEFAULT_INGEST_MAX_REQUEST_BYTES


def _env_positive_int(name: str, default: int) -> int:
    """Read a positive integer from the environment, falling back to *default*."""
    raw = os.getenv(name)
    if raw is None or not str(raw).strip():
        return default
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


def load_ingestion_limits() -> IngestionLimits:
    """Build the effective :class:`IngestionLimits` from the environment."""
    return IngestionLimits(
        max_files=_env_positive_int(ENV_INGEST_MAX_FILES, DEFAULT_INGEST_MAX_FILES),
        max_file_bytes=_env_positive_int(ENV_INGEST_MAX_FILE_BYTES, DEFAULT_INGEST_MAX_FILE_BYTES),
        max_total_bytes=_env_positive_int(ENV_INGEST_MAX_TOTAL_BYTES, DEFAULT_INGEST_MAX_TOTAL_BYTES),
        max_request_bytes=_env_positive_int(ENV_INGEST_MAX_REQUEST_BYTES, DEFAULT_INGEST_MAX_REQUEST_BYTES),
    )


def ingestion_enabled(value: Optional[str] = None) -> bool:
    """Return True when the ingestion endpoint is explicitly enabled.

    Reads ``SENTINEL_ALLOW_INCIDENT_INGESTION`` unless *value* is supplied.
    The endpoint is **disabled by default**; any value other than a recognised
    truthy token (``1``, ``true``, ``yes``, ``on``, case-insensitive) leaves it
    disabled.
    """
    if value is None:
        value = os.getenv(ENV_ALLOW_INGESTION, "")
    return str(value).strip().lower() in _TRUE_VALUES


# ---------------------------------------------------------------------------
# Path policy
# ---------------------------------------------------------------------------

#: Relative POSIX paths a client may submit verbatim.
ALLOWED_EXACT_FILES: frozenset = frozenset({
    "logs/application.log",
    "metrics/metrics.csv",
    "git_diff.patch",
    "docker-compose.yml",
})

#: Every file beneath this prefix is permitted (e.g. ``service/app.py``).
ALLOWED_SUBTREE_PREFIX = "service/"

#: Evaluation-only artefacts that must never be ingestible.
RESERVED_FILENAMES: frozenset = frozenset({
    "ground_truth.md",
    "ground_truth.json",
    "results_baseline.csv",
    "baseline_summary.json",
})

#: Maximum length of one submitted relative path (characters).
MAX_PATH_LENGTH = 512
#: Maximum number of path segments.
MAX_PATH_DEPTH = 16

_DRIVE_PREFIX_RE = re.compile(r"^[A-Za-z]:")


def validate_bundle_relative_path(relative_path: Any) -> str:
    """Validate one submitted relative POSIX path against the ingestion policy.

    Args:
        relative_path: The client-supplied key from the ``files`` mapping.

    Returns:
        The validated path unchanged (it is already normalised: no ``.``, ``..``
        or empty segments survive validation).

    Raises:
        InvalidBundlePathError: The path is unsafe, malformed, reserved, or not
            on the ingestion allowlist.
    """
    if not isinstance(relative_path, str):
        raise InvalidBundlePathError("Each file path must be a string.")
    if not relative_path:
        raise InvalidBundlePathError("File path cannot be empty.")
    if "\x00" in relative_path:
        raise InvalidBundlePathError("File path must not contain NUL bytes.")
    if "\\" in relative_path:
        raise InvalidBundlePathError(
            f"Backslashes are not permitted in file paths: {relative_path!r}"
        )
    if _DRIVE_PREFIX_RE.match(relative_path):
        raise InvalidBundlePathError(
            f"Drive prefixes are not permitted in file paths: {relative_path!r}"
        )
    if relative_path.startswith("/"):
        raise InvalidBundlePathError(
            f"Absolute file paths are not permitted: {relative_path!r}"
        )
    if len(relative_path) > MAX_PATH_LENGTH:
        raise InvalidBundlePathError(
            f"File path exceeds the maximum length of {MAX_PATH_LENGTH} characters."
        )

    segments = relative_path.split("/")
    if len(segments) > MAX_PATH_DEPTH:
        raise InvalidBundlePathError(
            f"File path exceeds the maximum depth of {MAX_PATH_DEPTH} segments."
        )
    for segment in segments:
        if segment == "":
            raise InvalidBundlePathError(
                f"File path contains an empty segment: {relative_path!r}"
            )
        if segment in (".", ".."):
            raise InvalidBundlePathError(
                f"File path contains a relative segment: {relative_path!r}"
            )
        if segment in RESERVED_FILENAMES:
            raise InvalidBundlePathError(
                f"File path {relative_path!r} is a reserved evaluation artefact "
                "and cannot be ingested."
            )

    if relative_path in ALLOWED_EXACT_FILES:
        return relative_path
    if relative_path.startswith(ALLOWED_SUBTREE_PREFIX) and len(segments) >= 2:
        return relative_path
    raise InvalidBundlePathError(
        f"File path {relative_path!r} is not on the ingestion allowlist."
    )


# ---------------------------------------------------------------------------
# Strict JSON decoding
# ---------------------------------------------------------------------------

def _reject_duplicate_keys(pairs: Any) -> Dict[str, Any]:
    """``object_pairs_hook`` that rejects duplicate keys in any JSON object."""
    result: Dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise DuplicateJsonKeyError(f"Duplicate JSON key detected: {key!r}")
        result[key] = value
    return result


def parse_json_object_strict(raw: str) -> Dict[str, Any]:
    """Decode *raw* as a JSON object, rejecting duplicate keys.

    Raises:
        DuplicateJsonKeyError: The body contains a duplicate object key.
        MalformedRequestError: The body is not valid JSON, or is not an object.
    """
    try:
        parsed = json.loads(raw, object_pairs_hook=_reject_duplicate_keys)
    except DuplicateJsonKeyError:
        raise
    except json.JSONDecodeError as exc:
        raise MalformedRequestError("Malformed JSON request body.") from exc
    if not isinstance(parsed, dict):
        raise MalformedRequestError("Request body must be a JSON object.")
    return parsed


# ---------------------------------------------------------------------------
# Ingestion service
# ---------------------------------------------------------------------------

#: Serialises the "check-then-publish" critical section. Callers may supply a
#: stronger lock (e.g. the API's active-investigation registry lock) so that a
#: bundle can never be published while an investigation for the same ID is
#: being claimed.
_DEFAULT_PUBLISH_LOCK = threading.Lock()

#: Per-ID in-flight guard: two simultaneous ingestions of the same ID cannot
#: both succeed.
_INFLIGHT_LOCK = threading.Lock()
_INFLIGHT_IDS: set = set()


@dataclass(frozen=True)
class IngestedBundle:
    """Outcome of a successful bundle ingestion."""

    incident_id: str
    evidence_sources: Tuple[str, ...]


def _validate_submission(
    files: Mapping[str, str],
    limits: IngestionLimits,
) -> Dict[str, str]:
    """Apply the path policy and resource limits to a submission.

    Paths are validated first (unsafe input is a client error), then byte
    limits are counted over UTF-8 encoded content **before** any staging file
    is created. Character counts are never used.
    """
    validated: Dict[str, str] = {}
    for raw_path, content in files.items():
        validated[validate_bundle_relative_path(raw_path)] = content

    if len(validated) > limits.max_files:
        raise PayloadTooLargeError(
            f"Bundle contains {len(validated)} files, exceeding the maximum of "
            f"{limits.max_files}."
        )

    total_bytes = 0
    for path, content in validated.items():
        size = len(content.encode("utf-8"))
        if size > limits.max_file_bytes:
            raise PayloadTooLargeError(
                f"File {path!r} is {size} bytes, exceeding the per-file maximum "
                f"of {limits.max_file_bytes} bytes."
            )
        total_bytes += size

    if total_bytes > limits.max_total_bytes:
        raise PayloadTooLargeError(
            f"Bundle content is {total_bytes} bytes, exceeding the total maximum "
            f"of {limits.max_total_bytes} bytes."
        )
    return validated


def _reserve_ingestion(incident_id: str) -> None:
    """Register an in-flight ingestion, refusing a concurrent one for the same ID."""
    with _INFLIGHT_LOCK:
        if incident_id in _INFLIGHT_IDS:
            raise IncidentConflictError(
                f"An ingestion for incident '{incident_id}' is already in progress."
            )
        _INFLIGHT_IDS.add(incident_id)


def _release_ingestion(incident_id: str) -> None:
    with _INFLIGHT_LOCK:
        _INFLIGHT_IDS.discard(incident_id)


def _remove_tree(path: Optional[Path]) -> None:
    """Best-effort removal of a staging tree; never raises."""
    if path is None:
        return
    try:
        shutil.rmtree(path, ignore_errors=True)
    except Exception:  # pragma: no cover - defensive
        logger.warning("Failed to remove ingestion staging directory")


def _write_staged_files(
    staged_root: Path,
    resolved_staged_root: Path,
    validated: Mapping[str, str],
) -> None:
    """Write every submitted file beneath the freshly created staging root.

    Every destination is containment-checked against the staging root before it
    is opened, and files are created with exclusive-create semantics so no
    pre-existing entry (or symlink) is ever followed.
    """
    for rel_path, content in validated.items():
        dest = staged_root / rel_path
        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
        except FileExistsError as exc:
            raise InvalidBundlePathError(
                f"File path {rel_path!r} conflicts with another submitted path."
            ) from exc
        except OSError as exc:
            raise StorageUnavailableError(
                f"Could not create directories for {rel_path!r}."
            ) from exc

        try:
            resolved_dest = dest.resolve()
        except OSError as exc:
            raise StorageUnavailableError(f"Could not resolve {rel_path!r}.") from exc
        if resolved_staged_root not in resolved_dest.parents:
            raise InvalidBundlePathError(
                f"File path {rel_path!r} escapes the incident bundle root."
            )

        try:
            with open(dest, "x", encoding="utf-8") as handle:
                handle.write(content)
        except OSError as exc:
            raise StorageUnavailableError(f"Could not write {rel_path!r}.") from exc


def _publish_new_bundle(staged_root: Path, target: Path) -> None:
    """Atomically publish a validated staged bundle at *target*.

    Must be called while holding the publish lock. A single ``os.rename`` is the
    only operation that makes the bundle visible, so a client can never observe
    a partially written bundle, and the previous contents at *target* — if any —
    are never modified: an existing entry (regular directory, file, or symlink)
    is reported as a conflict and left completely untouched.

    The rename is the sole publish step, so it — not the in-process lock — is
    what makes publication exclusive **across processes**: two concurrent
    ingestions of the same new ID cannot both succeed on one filesystem,
    because the loser's rename fails against the now-existing directory and is
    mapped to a conflict. This holds unconditionally because a published bundle
    always contains at least one file, so the target is never an *empty*
    directory — the only kind POSIX ``rename`` would overwrite (on Windows
    ``os.rename`` never replaces an existing entry at all). The ingestion
    service rejects an empty bundle up front (see
    :func:`ingest_incident_bundle`) so this guarantee does not depend on the
    caller, and a losing publish never mixes, merges with, or mutates the
    winner's files — only the winner's rename touches the target.
    """
    if target.is_symlink() or target.exists():
        raise IncidentConflictError(f"Incident '{target.name}' already exists.")

    try:
        os.rename(staged_root, target)
    except OSError as exc:
        # Lost a cross-process race, or the target appeared concurrently.
        if target.is_symlink() or target.exists():
            raise IncidentConflictError(f"Incident '{target.name}' already exists.") from exc
        raise StorageUnavailableError("Could not publish the incident bundle.") from exc


def ingest_incident_bundle(
    incidents_root: Path | str,
    incident_id: str,
    files: Mapping[str, str],
    overwrite: bool = False,
    *,
    limits: Optional[IngestionLimits] = None,
    registry_lock: Optional[Any] = None,
    registry_guard: Optional[Callable[[], None]] = None,
) -> IngestedBundle:
    """Validate, stage and atomically publish one **new** incident bundle.

    Args:
        incidents_root: Directory that holds incident bundles.
        incident_id: Target incident ID (already validated by the request model).
        files: Mapping of relative POSIX paths to UTF-8 text content.
        overwrite: Rejected when True (see :class:`OverwriteUnsupportedError`).
            Retained in the signature so the request contract can surface a
            stable error instead of silently ignoring a client's intent.
        limits: Effective resource limits (defaults to the environment config).
        registry_lock: Lock held across the check-then-publish critical section.
            Defaults to a module-level lock; the API passes its active-claim
            lock so a running/leased incident cannot be published mid-investigation.
        registry_guard: Callable invoked under *registry_lock* immediately before
            publishing; it must raise to veto the publish.

    Returns:
        The :class:`IngestedBundle` describing the published bundle.

    Raises:
        OverwriteUnsupportedError: ``overwrite`` was requested. Raised before any
            staging file is created, so a rejected request writes nothing.
        MalformedRequestError: ``files`` is empty. Enforced here (as well as in
            the request model) so a published bundle is never an empty
            directory, which is what makes the exclusive-publish rename
            unconditional.
        InvalidBundlePathError: A submitted path violates the path policy.
        PayloadTooLargeError: A resource limit was exceeded.
        IncidentConflictError: The incident already exists, a concurrent
            ingestion is in flight, or the target path is a symlink.
        IncidentBusyError: Raised by *registry_guard* when the incident is busy.
        StorageUnavailableError: A staging or publish filesystem operation failed.
        InvalidIncidentBundleError: The staged bundle fails preflight validation.
    """
    if overwrite:
        raise OverwriteUnsupportedError(
            "Replacing an existing incident bundle is not supported; submit a "
            "new incident_id instead. (overwrite=true is rejected.)"
        )

    effective_limits = limits or load_ingestion_limits()
    root = Path(incidents_root)

    if not root.is_dir():
        raise StorageUnavailableError("Incident storage root is unavailable.")

    validated = _validate_submission(files, effective_limits)
    if not validated:
        # The request model already rejects an empty ``files`` map, but the
        # ingestion service owns the guarantee: a published bundle must be
        # non-empty so a concurrent same-ID publish can never be replaced by
        # ``os.rename`` (see _publish_new_bundle).
        raise MalformedRequestError("An incident bundle must contain at least one file.")

    _reserve_ingestion(incident_id)
    staging_container: Optional[Path] = None
    try:
        resolved_root = root.resolve()
        try:
            staging_container = Path(tempfile.mkdtemp(prefix=".ingest-", dir=str(root)))
        except OSError as exc:
            raise StorageUnavailableError("Could not create a staging directory.") from exc

        staged_root = staging_container / incident_id
        try:
            staged_root.mkdir()
        except OSError as exc:
            raise StorageUnavailableError("Could not create the staging bundle root.") from exc

        resolved_staged_root = staged_root.resolve()
        if resolved_root not in resolved_staged_root.parents:
            raise StorageUnavailableError("Staging directory escapes the incidents root.")

        _write_staged_files(staged_root, resolved_staged_root, validated)

        # Mission 18 preflight on the staged bundle before it is ever published.
        validation = require_valid_incident_bundle(staged_root, expected_id=incident_id)

        lock = registry_lock if registry_lock is not None else _DEFAULT_PUBLISH_LOCK
        with lock:
            if registry_guard is not None:
                registry_guard()
            _publish_new_bundle(staged_root, root / incident_id)

        return IngestedBundle(
            incident_id=incident_id,
            evidence_sources=tuple(validation.evidence_sources),
        )
    finally:
        _release_ingestion(incident_id)
        _remove_tree(staging_container)
