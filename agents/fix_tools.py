"""Fix Tools — deterministic, zero-LLM safety validation for fix proposals.

All functions in this module must remain LLM-free.  They validate that a
generated FixProposal is structurally sound, evidence-traceable, and safe
before it reaches the Human Approval Gate.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

import jsonschema

SCHEMA_PATH = Path(__file__).parent.parent / "schemas" / "fix_proposal_schema.json"
APPROVAL_SCHEMA_PATH = Path(__file__).parent.parent / "schemas" / "approval_schema.json"

HUMAN_APPROVAL_NOTICE = "AWAITING HUMAN APPROVAL — this fix has not been applied."

# Patterns that indicate a proposal is claiming the fix was already applied.
_APPLIED_INDICATORS = [
    re.compile(r"\balready\s+applied\b", re.IGNORECASE),
    re.compile(r"\bfix\s+has\s+been\s+applied\b", re.IGNORECASE),
    re.compile(r"\bpatch\s+applied\b", re.IGNORECASE),
    re.compile(r"\bchanges?\s+committed\b", re.IGNORECASE),
    re.compile(r"\bpushed\s+to\s+(?:main|master|production|prod)\b", re.IGNORECASE),
    re.compile(r"\bdeployed\b", re.IGNORECASE),
]

# Patterns that indicate destructive/dangerous operations in patch text.
_DESTRUCTIVE_PATCH_PATTERNS = [
    re.compile(r"\bos\.remove\b"),
    re.compile(r"\bshutil\.rmtree\b"),
    re.compile(r"\bsubprocess\.(call|run|Popen|check_call|check_output)\b"),
    re.compile(r"\bos\.system\b"),
    re.compile(r"\beval\s*\("),
    re.compile(r"\bexec\s*\("),
    re.compile(r"DROP\s+TABLE", re.IGNORECASE),
    re.compile(r"DELETE\s+FROM", re.IGNORECASE),
    re.compile(r"TRUNCATE\s+TABLE", re.IGNORECASE),
    re.compile(r"\brm\s+-rf\b"),
    re.compile(r"\bgit\s+(push|commit|reset|clean)\b"),
]

# Only incident service/* files may be targeted by a patch.
_ALLOWED_FILE_PREFIX = re.compile(r"^service/")


class ProposalValidationError(Exception):
    """Raised when a fix proposal fails deterministic safety validation."""


def load_fix_proposal_schema() -> Dict[str, Any]:
    with open(SCHEMA_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def load_approval_schema() -> Dict[str, Any]:
    with open(APPROVAL_SCHEMA_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


# ---------------------------------------------------------------------------
# Individual checks (each raises ProposalValidationError on failure)
# ---------------------------------------------------------------------------

def check_hypothesis_exists(
    proposal: Dict[str, Any],
    available_hypotheses: Dict[str, Any],
) -> None:
    """Rule 1: Referenced hypothesis_id must exist in the supplied hypotheses bundle."""
    hyp_id = proposal.get("hypothesis_id")
    if not isinstance(hyp_id, str):
        raise ProposalValidationError("proposal.hypothesis_id is missing or not a string.")
    all_ids = {h["hypothesis_id"] for h in available_hypotheses.get("hypotheses", []) if isinstance(h, dict)}
    if hyp_id not in all_ids:
        raise ProposalValidationError(
            f"Proposal references hypothesis {hyp_id!r} which does not exist in the supplied bundle. "
            f"Known IDs: {sorted(all_ids)}"
        )


def check_hypothesis_eligible(
    proposal: Dict[str, Any],
    verification_results: Dict[str, Any],
) -> None:
    """Rule 2: Referenced hypothesis must be CONFIRMED (not REJECTED or INCONCLUSIVE)."""
    hyp_id = proposal.get("hypothesis_id")
    verdict_map: Dict[str, str] = {}
    for r in verification_results.get("results", []):
        if isinstance(r, dict):
            verdict_map[r.get("hypothesis_id", "")] = r.get("verdict", "")
    if hyp_id not in verdict_map:
        raise ProposalValidationError(
            f"Hypothesis {hyp_id!r} has no verification result. "
            "Only hypotheses with a CONFIRMED verdict are eligible for a fix proposal."
        )
    verdict = verdict_map[hyp_id]
    if verdict != "CONFIRMED":
        raise ProposalValidationError(
            f"Hypothesis {hyp_id!r} verdict is {verdict!r}. "
            "Only CONFIRMED hypotheses are eligible for a fix proposal."
        )


def check_evidence_ids_exist(
    proposal: Dict[str, Any],
    all_evidence_ids: Set[str],
) -> None:
    """Rule 3: Every evidence_id in the proposal must exist in the supplied evidence."""
    ev_ids = proposal.get("evidence_ids") or []
    unknown = [eid for eid in ev_ids if eid not in all_evidence_ids]
    if unknown:
        raise ProposalValidationError(
            f"Proposal references unknown evidence IDs: {unknown}. "
            f"Available IDs: {sorted(all_evidence_ids)}"
        )
    if not ev_ids:
        raise ProposalValidationError("Proposal must reference at least one evidence ID.")


def check_referenced_files_exist(
    proposal: Dict[str, Any],
    incident_dir: Path,
) -> None:
    """Rule 4: Every file listed in changes must exist under the incident directory."""
    for change in proposal.get("changes") or []:
        if not isinstance(change, dict):
            continue
        rel_path = change.get("file")
        if not isinstance(rel_path, str) or not rel_path.strip():
            raise ProposalValidationError("A change entry is missing a 'file' field.")
        target = incident_dir / rel_path
        if not target.exists():
            raise ProposalValidationError(
                f"Proposed change references file {rel_path!r} which does not exist "
                f"under incident directory {incident_dir}."
            )


def check_source_locations_valid(
    proposal: Dict[str, Any],
    incident_dir: Path,
) -> None:
    """Rule 5: If start_line / end_line are given, verify they are within the file's length."""
    for change in proposal.get("changes") or []:
        if not isinstance(change, dict):
            continue
        rel_path = change.get("file")
        start_line = change.get("start_line")
        end_line = change.get("end_line")
        if start_line is None and end_line is None:
            continue
        if not isinstance(rel_path, str):
            continue
        target = incident_dir / rel_path
        if not target.exists():
            continue  # already caught by check_referenced_files_exist
        try:
            lines = target.read_text(encoding="utf-8", errors="replace").splitlines()
        except Exception:
            continue
        file_len = len(lines)
        if start_line is not None and not isinstance(start_line, int):
            raise ProposalValidationError(
                f"start_line for {rel_path!r} must be an integer or null."
            )
        if end_line is not None and not isinstance(end_line, int):
            raise ProposalValidationError(
                f"end_line for {rel_path!r} must be an integer or null."
            )
        if isinstance(start_line, int) and start_line < 1:
            raise ProposalValidationError(
                f"start_line {start_line} for {rel_path!r} is less than 1 (lines are 1-indexed)."
            )
        if isinstance(end_line, int) and end_line > file_len:
            raise ProposalValidationError(
                f"end_line {end_line} for {rel_path!r} exceeds file length {file_len}."
            )
        if isinstance(start_line, int) and isinstance(end_line, int) and start_line > end_line:
            raise ProposalValidationError(
                f"start_line {start_line} > end_line {end_line} for {rel_path!r}."
            )


def check_patch_targets_allowed_files(
    proposal: Dict[str, Any],
) -> None:
    """Rule 6: Patch and change file references must target service/* files only.

    This ensures the proposal cannot reference infrastructure, eval data,
    ground truth, or test harness files.
    """
    for change in proposal.get("changes") or []:
        if not isinstance(change, dict):
            continue
        rel_path = change.get("file", "")
        # Normalise Windows-style paths
        rel_path_norm = rel_path.replace("\\", "/")
        if not _ALLOWED_FILE_PREFIX.match(rel_path_norm):
            raise ProposalValidationError(
                f"Proposed change targets {rel_path!r} which is outside the allowed "
                "'service/' directory. Fix proposals may only reference incident service files."
            )


def check_patch_not_destructive(
    proposal: Dict[str, Any],
) -> None:
    """Rule 7: Reject proposals whose patch text contains obviously destructive operations."""
    patch = proposal.get("patch") or ""
    after_texts = [c.get("after", "") for c in (proposal.get("changes") or []) if isinstance(c, dict)]
    combined = "\n".join([patch] + after_texts)
    for pattern in _DESTRUCTIVE_PATCH_PATTERNS:
        m = pattern.search(combined)
        if m:
            raise ProposalValidationError(
                f"Proposal patch contains a potentially destructive operation: {m.group()!r}. "
                "The Fix Proposal Agent must not include shell/system calls, git operations, "
                "or data-destruction statements."
            )


def check_proposal_not_claiming_applied(
    proposal: Dict[str, Any],
) -> None:
    """Rule 8: The proposal must not claim the fix has already been applied."""
    texts_to_scan = [
        proposal.get("summary", ""),
        proposal.get("rationale", ""),
        proposal.get("expected_effect", ""),
        proposal.get("patch", ""),
    ]
    for change in proposal.get("changes") or []:
        if isinstance(change, dict):
            texts_to_scan.append(change.get("description", ""))
    combined = " ".join(texts_to_scan)
    for pattern in _APPLIED_INDICATORS:
        m = pattern.search(combined)
        if m:
            raise ProposalValidationError(
                f"Proposal text appears to claim the fix was already applied: {m.group()!r}. "
                "Proposals must describe a future action, not a completed one."
            )


def check_approval_notice_present(
    proposal: Dict[str, Any],
) -> None:
    """Rule 9: The human_approval_notice must be the exact canonical string."""
    notice = proposal.get("human_approval_notice", "")
    if notice != HUMAN_APPROVAL_NOTICE:
        raise ProposalValidationError(
            f"human_approval_notice is missing or incorrect. "
            f"Expected exactly: {HUMAN_APPROVAL_NOTICE!r}"
        )


def check_status_is_proposed(
    proposal: Dict[str, Any],
) -> None:
    """Rule 10: Status must be PROPOSED before human approval."""
    status = proposal.get("status")
    if status != "PROPOSED":
        raise ProposalValidationError(
            f"Proposal status must be 'PROPOSED' before the approval gate, got {status!r}."
        )


# ---------------------------------------------------------------------------
# Schema validation
# ---------------------------------------------------------------------------

def validate_proposal_schema(proposal: Dict[str, Any]) -> None:
    """Validate against fix_proposal_schema.json. Raises ProposalValidationError on failure."""
    schema = load_fix_proposal_schema()
    try:
        jsonschema.validate(instance=proposal, schema=schema)
    except jsonschema.ValidationError as exc:
        raise ProposalValidationError(f"Schema validation failed: {exc.message}") from exc


def validate_approval_record_schema(record: Dict[str, Any]) -> None:
    """Validate against approval_schema.json. Raises ProposalValidationError on failure."""
    schema = load_approval_schema()
    try:
        jsonschema.validate(instance=record, schema=schema)
    except jsonschema.ValidationError as exc:
        raise ProposalValidationError(f"Approval schema validation failed: {exc.message}") from exc


# ---------------------------------------------------------------------------
# Master validator
# ---------------------------------------------------------------------------

def validate_proposal(
    proposal: Dict[str, Any],
    incident_dir: Path,
    available_hypotheses: Dict[str, Any],
    verification_results: Dict[str, Any],
    all_evidence_ids: Set[str],
) -> Tuple[bool, List[str]]:
    """Run all deterministic safety checks against a proposal.

    Returns:
        (is_valid, errors): True + empty list if all checks pass;
                            False + list of error strings otherwise.
    """
    errors: List[str] = []
    checks = [
        lambda: check_approval_notice_present(proposal),
        lambda: check_status_is_proposed(proposal),
        lambda: check_hypothesis_exists(proposal, available_hypotheses),
        lambda: check_hypothesis_eligible(proposal, verification_results),
        lambda: check_evidence_ids_exist(proposal, all_evidence_ids),
        lambda: check_referenced_files_exist(proposal, incident_dir),
        lambda: check_source_locations_valid(proposal, incident_dir),
        lambda: check_patch_targets_allowed_files(proposal),
        lambda: check_patch_not_destructive(proposal),
        lambda: check_proposal_not_claiming_applied(proposal),
        lambda: validate_proposal_schema(proposal),
    ]
    for check_fn in checks:
        try:
            check_fn()
        except ProposalValidationError as exc:
            errors.append(str(exc))
    return (len(errors) == 0, errors)


# ---------------------------------------------------------------------------
# Evidence ID helpers
# ---------------------------------------------------------------------------

def collect_all_evidence_ids(
    logs_evidence: Optional[Dict[str, Any]] = None,
    metrics_evidence: Optional[Dict[str, Any]] = None,
    code_evidence: Optional[Dict[str, Any]] = None,
) -> Set[str]:
    """Gather all EV-* IDs from the supplied evidence bundles."""
    ids: Set[str] = set()
    for bundle in (logs_evidence, metrics_evidence, code_evidence):
        if not isinstance(bundle, dict):
            continue
        for ev in bundle.get("evidence") or []:
            if isinstance(ev, dict) and isinstance(ev.get("evidence_id"), str):
                ids.add(ev["evidence_id"])
    return ids


def filter_confirmed_hypotheses(
    hypotheses: Dict[str, Any],
    verification_results: Dict[str, Any],
) -> List[Dict[str, Any]]:
    """Return only hypotheses with a CONFIRMED verdict."""
    verdict_map: Dict[str, str] = {}
    for r in verification_results.get("results", []):
        if isinstance(r, dict):
            verdict_map[r.get("hypothesis_id", "")] = r.get("verdict", "")
    confirmed = []
    for h in hypotheses.get("hypotheses", []):
        if isinstance(h, dict):
            hyp_id = h.get("hypothesis_id", "")
            if verdict_map.get(hyp_id) == "CONFIRMED":
                confirmed.append(h)
    return confirmed
