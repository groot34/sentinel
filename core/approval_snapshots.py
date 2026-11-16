"""Immutable evidence snapshots for human approval decisions (Mission 26.2).

When a human approves or rejects a fix proposal, Sentinel must preserve the
exact investigation context that was available to the reviewer at the moment of
the decision. A snapshot is captured once, derived deterministically from the
persisted investigation state, and then never rewritten — later evidence,
hypothesis, verification or proposal changes cannot alter it.

This module owns the *representation* concerns only:

* a canonical, byte-stable JSON serialization of a snapshot document;
* a deterministic SHA-256 integrity digest over that canonical form; and
* an :class:`ApprovalSnapshot` transport record used by the persistence layer.

The integrity digest is an audit aid for detecting corruption or tampering. It
is deliberately **not** an access-control mechanism.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Dict, Optional

#: Schema version of the snapshot document. Bumped only for a breaking change
#: to the document shape; stored alongside every snapshot so a future reader can
#: interpret an older record without guessing.
SNAPSHOT_SCHEMA_VERSION = 1

#: Name of the integrity digest algorithm recorded in API responses.
INTEGRITY_ALGORITHM = "sha256"


class SnapshotIntegrityError(Exception):
    """Raised when a stored approval snapshot fails integrity verification.

    Signals that the persisted snapshot no longer matches the digest recorded
    at capture time — i.e. it was corrupted or tampered with after the decision.
    Distinct from "no snapshot exists" so callers can return an explicit
    integrity failure rather than silently substituting current evidence.
    """


def canonical_json(document: Dict[str, Any]) -> str:
    """Serialize *document* to a canonical, byte-stable JSON string.

    Keys are sorted and separators are compacted so two logically identical
    documents always produce the same bytes — the prerequisite for a stable
    integrity digest across a PostgreSQL JSONB round trip (which may reorder
    object keys).
    """
    return json.dumps(
        document,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )


def compute_integrity_hash(document: Dict[str, Any]) -> str:
    """Return the hex SHA-256 digest of the canonical form of *document*."""
    return hashlib.sha256(canonical_json(document).encode("utf-8")).hexdigest()


def verify_integrity(document: Dict[str, Any], expected_hash: str) -> bool:
    """Return True when *document* still matches *expected_hash*.

    A mismatch means the stored snapshot was corrupted or tampered with after it
    was captured.
    """
    if not isinstance(expected_hash, str) or not expected_hash:
        return False
    return hmac_safe_compare(compute_integrity_hash(document), expected_hash)


def hmac_safe_compare(left: str, right: str) -> bool:
    """Constant-time comparison of two digest strings."""
    import hmac

    return hmac.compare_digest(left.encode("utf-8"), right.encode("utf-8"))


@dataclass(frozen=True)
class ApprovalSnapshot:
    """Transport record for one immutable approval-decision snapshot.

    ``payload`` is the full snapshot document; ``integrity_hash`` is derived
    from it at capture time and re-verified on retrieval.
    """

    investigation_id: str
    proposal_id: str
    decision: str
    reviewer: str
    decided_at: str
    snapshot_version: int
    integrity_hash: str
    payload: Dict[str, Any]


def build_snapshot_document(
    *,
    investigation_id: str,
    decision_context: Dict[str, Any],
    proposal_context: Optional[Dict[str, Any]],
    hypothesis_context: Optional[Dict[str, Any]],
    evidence_context: list,
    verification_context: list,
) -> Dict[str, Any]:
    """Assemble the canonical snapshot document from already-projected context.

    Every section is supplied by the caller from the persisted investigation
    state as it stood at decision time; nothing is invented or back-filled.
    """
    return {
        "snapshot_version": SNAPSHOT_SCHEMA_VERSION,
        "investigation_id": investigation_id,
        "proposal_id": decision_context.get("proposal_id"),
        "decision": decision_context,
        "proposal": proposal_context,
        "hypothesis": hypothesis_context,
        "evidence": list(evidence_context),
        "verification": list(verification_context),
    }


def create_snapshot(
    *,
    investigation_id: str,
    decision_context: Dict[str, Any],
    proposal_context: Optional[Dict[str, Any]],
    hypothesis_context: Optional[Dict[str, Any]],
    evidence_context: list,
    verification_context: list,
) -> ApprovalSnapshot:
    """Build a snapshot document and return it with its integrity digest."""
    payload = build_snapshot_document(
        investigation_id=investigation_id,
        decision_context=decision_context,
        proposal_context=proposal_context,
        hypothesis_context=hypothesis_context,
        evidence_context=evidence_context,
        verification_context=verification_context,
    )
    return ApprovalSnapshot(
        investigation_id=investigation_id,
        proposal_id=str(decision_context.get("proposal_id") or ""),
        decision=str(decision_context.get("decision") or ""),
        reviewer=str(decision_context.get("reviewer") or ""),
        decided_at=str(decision_context.get("timestamp") or ""),
        snapshot_version=SNAPSHOT_SCHEMA_VERSION,
        integrity_hash=compute_integrity_hash(payload),
        payload=payload,
    )
