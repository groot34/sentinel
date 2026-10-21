"""Deterministic preflight validation for Sentinel incident bundles.

This module owns the *incident bundle contract* — the set of files the evidence
agents and the orchestrator actually read from ``incidents/<incident_id>/`` —
and validates a bundle before any LLM-backed stage runs, so invalid input is
rejected before credits are spent.

Design rules
------------
* Zero LLM, zero network, read-only. Validation never mutates incident files.
* Only the two structured evidence files (``logs/application.log`` and
  ``metrics/metrics.csv``) are ever opened, and only by their canonical relative
  paths. The rest of the bundle is never enumerated, so evaluation-only
  artefacts (``ground_truth.md``, ``results_baseline.csv``, ``baseline_summary.json``)
  can never be reached from here.
* Path safety is enforced by (a) the canonical incident-ID check and (b) a
  symlink-escape check: a bundle entry that resolves outside the bundle root is
  rejected. Containment is verified *before* any evidence file is opened, so a
  canonical path that escapes the root is reported but never read.
* Fatal errors stop the investigation before it starts. Warnings do not. An
  evidence-free bundle is *structurally valid* — it yields an empty
  investigation — and is reported as a warning, preserving the existing
  partial-evidence behaviour of every evidence agent.

Contract
--------
Required
    * The bundle root exists, is a directory, and is not a symlink.
    * The bundle root is named with a valid incident ID
      (``^[a-zA-Z0-9][a-zA-Z0-9_-]{0,127}$``).
    * Every entry that *is* present has the expected type and stays inside the
      bundle root.
    * Every source directory that *is* present contains its expected file
      (``logs/`` requires ``logs/application.log``; ``metrics/`` requires
      ``metrics/metrics.csv``). Removing the directory declares the channel
      absent.
    * ``metrics/metrics.csv`` satisfies the metrics CSV contract when present:
      non-empty, header row present, ``timestamp`` column present, every
      ``timestamp`` value an ISO-8601 UTC instant ending in ``Z``
      (``2026-08-28T14:00:00Z``), and every remaining column numeric (row
      numbers are reported on failure).
Optional
    * Every evidence source. Any subset — including none — is acceptable.
    * ``git_diff.patch``, ``service/``, ``docker-compose.yml``.
    * ``logs/application.log`` content format is deliberately permissive; an
      empty log file is a warning, not an error.
Never read
    * Evaluation-only artefacts (see the design rules above).
"""

from __future__ import annotations

import argparse
import csv
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import List, Optional, Set, Tuple

from agents.code_tools import DIFF_PATH, SERVICE_DIR
from agents.log_tools import DEFAULT_RELATIVE_LOG
from agents.metric_tools import DEFAULT_RELATIVE_METRICS, MetricTable, load_metrics
from core.persistence.repository import PersistenceError, validate_investigation_id


# ---------------------------------------------------------------------------
# Contract constants (single source of truth shared with the evidence agents)
# ---------------------------------------------------------------------------

LOG_DIR = "logs"
METRICS_DIR = "metrics"
COMPOSE_FILE = "docker-compose.yml"

LOG_FILE = DEFAULT_RELATIVE_LOG            # logs/application.log
METRICS_FILE = DEFAULT_RELATIVE_METRICS    # metrics/metrics.csv

#: Channel directories that declare intent: when present, their expected file
#: must also be present.
CHANNEL_DIRS: Tuple[Tuple[str, str], ...] = (
    (LOG_DIR, LOG_FILE),
    (METRICS_DIR, METRICS_FILE),
)

#: Entries that count as evidence when present.
EVIDENCE_ENTRIES: Tuple[str, ...] = (LOG_FILE, METRICS_FILE, DIFF_PATH, SERVICE_DIR)

#: Entries that must be regular files when present.
FILE_ENTRIES: Tuple[str, ...] = (LOG_FILE, METRICS_FILE, DIFF_PATH, COMPOSE_FILE)

#: Entries that must be directories when present.
DIR_ENTRIES: Tuple[str, ...] = (LOG_DIR, METRICS_DIR, SERVICE_DIR)

SEVERITY_ERROR = "error"
SEVERITY_WARNING = "warning"

#: Documented metrics timestamp format: an ISO-8601 UTC instant ending in ``Z``.
#: Fractional seconds are permitted; explicit numeric offsets (``+00:00``) are not,
#: because the contract and every fixture use the ``Z`` form.
_ISO_UTC_TIMESTAMP_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z$")
TIMESTAMP_EXAMPLE = "2026-08-28T14:00:00Z"


class InvalidIncidentBundleError(Exception):
    """Raised when an incident bundle fails deterministic preflight validation."""


@dataclass(frozen=True)
class BundleIssue:
    """One preflight finding, always naming the affected bundle entry."""

    severity: str
    code: str
    path: str
    detail: str

    def as_message(self) -> str:
        return f"{self.path}: {self.detail}"


@dataclass(frozen=True)
class BundleValidationResult:
    """Outcome of validating one incident bundle."""

    incident_id: str
    bundle_root: str
    evidence_sources: Tuple[str, ...]
    issues: Tuple[BundleIssue, ...]

    @property
    def errors(self) -> Tuple[BundleIssue, ...]:
        return tuple(i for i in self.issues if i.severity == SEVERITY_ERROR)

    @property
    def warnings(self) -> Tuple[BundleIssue, ...]:
        return tuple(i for i in self.issues if i.severity == SEVERITY_WARNING)

    @property
    def ok(self) -> bool:
        """True when the bundle is valid (warnings do not invalidate a bundle)."""
        return not self.errors

    def error_messages(self) -> List[str]:
        return [issue.as_message() for issue in self.errors]

    def warning_messages(self) -> List[str]:
        return [issue.as_message() for issue in self.warnings]


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def _check_containment(
    root: Path, resolved_root: Path, relative_path: str
) -> Optional[BundleIssue]:
    """Reject a bundle entry that is a symlink escaping the bundle root."""
    entry = root / relative_path
    if not entry.is_symlink():
        return None
    resolved = entry.resolve()
    if resolved == resolved_root or resolved_root in resolved.parents:
        return None
    return BundleIssue(
        SEVERITY_ERROR,
        "bundle_escapes_root",
        relative_path,
        f"{relative_path} is a symlink that resolves outside the incident bundle root.",
    )


def _is_readable_evidence_path(
    root: Path,
    resolved_root: Path,
    relative_path: str,
    escaping: Set[str],
) -> bool:
    """Return True only when a canonical evidence path is safe to open.

    Returns False when the entry, or its parent directory, is already known to
    escape the bundle root, and as defence in depth whenever the fully-resolved
    path lands outside the root (e.g. via a symlinked parent directory). Callers
    must not open the path when this returns False — the escape is already
    reported as a validation issue.
    """
    parent = relative_path.split("/", 1)[0]
    if relative_path in escaping or parent in escaping:
        return False
    try:
        resolved = (root / relative_path).resolve()
    except OSError:
        return False
    return resolved == resolved_root or resolved_root in resolved.parents


def _is_valid_utc_timestamp(value: str) -> bool:
    """True when ``value`` is the documented ISO-8601 UTC timestamp form."""
    text = (value or "").strip()
    if not _ISO_UTC_TIMESTAMP_RE.match(text):
        return False
    try:
        datetime.fromisoformat(text[:-1] + "+00:00")
    except ValueError:
        return False
    return True


def _validate_metric_timestamps(table: MetricTable) -> List[BundleIssue]:
    """Reject the first data row whose timestamp is not ISO-8601 UTC (``Z``)."""
    rel = METRICS_FILE
    for timestamp, row_number in zip(table.timestamps, table.row_numbers):
        if _is_valid_utc_timestamp(timestamp):
            continue
        return [
            BundleIssue(
                SEVERITY_ERROR,
                "metrics_timestamp_invalid",
                rel,
                f"{rel} has an invalid timestamp at file row {row_number}: "
                f"{timestamp!r}; expected an ISO-8601 UTC timestamp ending in 'Z' "
                f"(e.g. {TIMESTAMP_EXAMPLE}).",
            )
        ]
    return []


def _validate_metrics_csv(path: Path) -> List[BundleIssue]:
    """Apply the metrics CSV contract to a present metrics file."""
    rel = METRICS_FILE
    try:
        if path.stat().st_size == 0:
            return [
                BundleIssue(
                    SEVERITY_ERROR,
                    "metrics_csv_empty",
                    rel,
                    f"{rel} is empty; the metrics CSV contract requires a header row.",
                )
            ]
        table = load_metrics(path)
    except UnicodeDecodeError as exc:
        return [
            BundleIssue(
                SEVERITY_ERROR,
                "metrics_csv_unreadable",
                rel,
                f"{rel} is not valid UTF-8 text: {exc}",
            )
        ]
    except csv.Error as exc:
        return [
            BundleIssue(
                SEVERITY_ERROR,
                "metrics_csv_invalid",
                rel,
                f"{rel} is not well-formed CSV: {exc}",
            )
        ]
    except ValueError as exc:
        return [
            BundleIssue(
                SEVERITY_ERROR,
                "metrics_csv_invalid",
                rel,
                f"{rel} does not satisfy the metrics CSV contract: {exc}",
            )
        ]
    except OSError as exc:
        return [
            BundleIssue(
                SEVERITY_ERROR,
                "metrics_csv_unreadable",
                rel,
                f"{rel} could not be read: {exc}",
            )
        ]
    return _validate_metric_timestamps(table)


def validate_incident_bundle(
    incident_dir: Path | str,
    expected_id: Optional[str] = None,
) -> BundleValidationResult:
    """Validate an incident bundle without raising on bundle-content problems.

    Never raises for a malformed or incomplete bundle: findings are returned as
    :class:`BundleIssue` entries so callers can report all problems at once.
    """
    root = Path(incident_dir)
    incident_id = root.name

    if not root.exists():
        return BundleValidationResult(
            incident_id=incident_id,
            bundle_root=str(root),
            evidence_sources=(),
            issues=(
                BundleIssue(
                    SEVERITY_ERROR,
                    "bundle_missing",
                    ".",
                    f"Incident bundle directory not found: {root}",
                ),
            ),
        )

    if not root.is_dir():
        return BundleValidationResult(
            incident_id=incident_id,
            bundle_root=str(root),
            evidence_sources=(),
            issues=(
                BundleIssue(
                    SEVERITY_ERROR,
                    "bundle_not_a_directory",
                    ".",
                    f"Incident bundle path is not a directory: {root}",
                ),
            ),
        )

    issues: List[BundleIssue] = []

    # --- Identity and boundary ------------------------------------------
    try:
        validate_investigation_id(incident_id)
    except PersistenceError as exc:
        issues.append(
            BundleIssue(
                SEVERITY_ERROR,
                "invalid_incident_id",
                ".",
                f"Incident directory name {incident_id!r} is not a valid incident ID: {exc}",
            )
        )

    if expected_id is not None and expected_id != incident_id:
        issues.append(
            BundleIssue(
                SEVERITY_ERROR,
                "incident_id_mismatch",
                ".",
                f"Incident directory name {incident_id!r} does not match the "
                f"expected investigation ID {expected_id!r}.",
            )
        )

    if root.is_symlink():
        issues.append(
            BundleIssue(
                SEVERITY_ERROR,
                "bundle_symlink",
                ".",
                "Incident bundle root is a symlink; symlinked bundles are not permitted.",
            )
        )

    resolved_root = root.resolve()
    escaping_entries: Set[str] = set()
    for rel in DIR_ENTRIES + FILE_ENTRIES:
        escape = _check_containment(root, resolved_root, rel)
        if escape is not None:
            escaping_entries.add(rel)
            issues.append(escape)

    # --- Entry types ----------------------------------------------------
    for rel in DIR_ENTRIES:
        entry = root / rel
        if entry.exists() and not entry.is_dir():
            issues.append(
                BundleIssue(
                    SEVERITY_ERROR,
                    "not_a_directory",
                    rel,
                    f"{rel} exists but is not a directory.",
                )
            )

    for rel in FILE_ENTRIES:
        entry = root / rel
        if entry.exists() and not entry.is_file():
            issues.append(
                BundleIssue(
                    SEVERITY_ERROR,
                    "not_a_file",
                    rel,
                    f"{rel} exists but is not a regular file.",
                )
            )

    # --- Declared channels must contain their expected file -------------
    for rel_dir, rel_file in CHANNEL_DIRS:
        if (root / rel_dir).is_dir() and not (root / rel_file).exists():
            issues.append(
                BundleIssue(
                    SEVERITY_ERROR,
                    "required_file_missing",
                    rel_file,
                    f"{rel_file} is missing although the {rel_dir}/ directory exists. "
                    f"Add {rel_file}, or remove the {rel_dir}/ directory to declare "
                    f"the channel absent.",
                )
            )

    # --- Structured evidence contracts ----------------------------------
    # Containment was verified above, before any file is opened: a canonical
    # evidence path that escapes the bundle root is reported but never read.
    if _is_readable_evidence_path(root, resolved_root, METRICS_FILE, escaping_entries):
        metrics_path = root / METRICS_FILE
        if metrics_path.is_file():
            issues.extend(_validate_metrics_csv(metrics_path))

    if _is_readable_evidence_path(root, resolved_root, LOG_FILE, escaping_entries):
        log_path = root / LOG_FILE
        if log_path.is_file():
            try:
                log_text = log_path.read_text(encoding="utf-8", errors="ignore")
            except OSError as exc:
                issues.append(
                    BundleIssue(
                        SEVERITY_ERROR,
                        "log_unreadable",
                        LOG_FILE,
                        f"{LOG_FILE} could not be read: {exc}",
                    )
                )
            else:
                if not log_text.strip():
                    issues.append(
                        BundleIssue(
                            SEVERITY_WARNING,
                            "empty_log_file",
                            LOG_FILE,
                            f"{LOG_FILE} is empty; the logs evidence channel will "
                            f"contribute nothing.",
                        )
                    )

    # --- Evidence coverage (informational) ------------------------------
    evidence_sources = tuple(rel for rel in EVIDENCE_ENTRIES if (root / rel).exists())
    if not evidence_sources:
        issues.append(
            BundleIssue(
                SEVERITY_WARNING,
                "no_evidence_sources",
                ".",
                "Bundle contains no evidence sources ("
                + ", ".join(EVIDENCE_ENTRIES)
                + "); the investigation will produce an evidence-free result.",
            )
        )

    return BundleValidationResult(
        incident_id=incident_id,
        bundle_root=str(root),
        evidence_sources=evidence_sources,
        issues=tuple(issues),
    )


def require_valid_incident_bundle(
    incident_dir: Path | str,
    expected_id: Optional[str] = None,
) -> BundleValidationResult:
    """Validate a bundle and raise on fatal problems.

    Returns the result (including warnings) when the bundle is valid.

    Raises:
        FileNotFoundError: the bundle root does not exist.
        NotADirectoryError: the bundle root is not a directory.
        InvalidIncidentBundleError: the bundle fails any other preflight check.
    """
    root = Path(incident_dir)
    result = validate_incident_bundle(root, expected_id=expected_id)
    if result.ok:
        return result

    codes = {issue.code for issue in result.errors}
    if "bundle_missing" in codes:
        raise FileNotFoundError(f"Incident directory not found: {root}")
    if "bundle_not_a_directory" in codes:
        raise NotADirectoryError(f"Path is not a directory: {root}")

    raise InvalidIncidentBundleError(
        f"Invalid incident bundle at {root}: " + "; ".join(result.error_messages())
    )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Sentinel incident bundle preflight validator (zero LLM). "
            "Checks an incident bundle against the documented contract before "
            "any investigation is started."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python -m agents.bundle_validator incidents/inc_01_n_plus_one_query
  python -m agents.bundle_validator incidents/inc_01_n_plus_one_query --incident-id inc_01_n_plus_one_query
""",
    )
    parser.add_argument(
        "incident_dir",
        type=Path,
        help="Path to the incident bundle directory (e.g., incidents/inc_01_n_plus_one_query)",
    )
    parser.add_argument(
        "--incident-id",
        default=None,
        help="Expected incident ID; must match the bundle directory name.",
    )
    return parser


def _cli() -> None:
    args = _build_parser().parse_args()
    result = validate_incident_bundle(args.incident_dir, expected_id=args.incident_id)

    print(f"Bundle:        {result.bundle_root}")
    print(f"Incident ID:   {result.incident_id}")
    print(
        "Evidence:      "
        + (", ".join(result.evidence_sources) if result.evidence_sources else "(none)")
    )

    for issue in result.warnings:
        print(f"[warning] {issue.code}: {issue.as_message()}")
    for issue in result.errors:
        print(f"[error]   {issue.code}: {issue.as_message()}")

    if result.ok:
        print("Result:        VALID")
        raise SystemExit(0)

    print("Result:        INVALID")
    raise SystemExit(1)


if __name__ == "__main__":
    _cli()
