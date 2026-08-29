"""Human Approval Gate — explicit, interactive state machine for fix proposals.

State machine:
    PROPOSED → PENDING_APPROVAL → APPROVED | REJECTED

Safety rules:
- Default is always REJECTED.
- Non-interactive mode defaults to REJECTED.
- Only an explicit affirmative ("y", "Y", "yes", "YES") produces APPROVED.
- EOF, empty input, timeout, and anything else produces REJECTED.
- Approval does NOT apply the patch. It only records the human decision.
- No source files are modified.
- No git state is modified.
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from typing import Any, Dict, Optional

import jsonschema

from agents.fix_tools import (
    HUMAN_APPROVAL_NOTICE,
    ProposalValidationError,
    load_approval_schema,
    validate_approval_record_schema,
)

# Verdicts for the approval gate state machine
STATE_PROPOSED = "PROPOSED"
STATE_PENDING = "PENDING_APPROVAL"
STATE_APPROVED = "APPROVED"
STATE_REJECTED = "REJECTED"

_AFFIRMATIVE = frozenset({"y", "yes"})


def _utc_now() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def _is_interactive() -> bool:
    """Return True only when stdin is a real TTY (i.e. a human is at the keyboard)."""
    return sys.stdin.isatty()


def _build_approval_record(
    proposal_id: str,
    decision: str,  # "approved" | "rejected"
    approved_by: str = "human",
    notes: Optional[str] = None,
) -> Dict[str, Any]:
    status = STATE_APPROVED if decision == "approved" else STATE_REJECTED
    record: Dict[str, Any] = {
        "proposal_id": proposal_id,
        "status": status,
        "decision": decision,
        "approved_by": approved_by,
        "timestamp": _utc_now(),
    }
    if notes is not None:
        record["notes"] = notes
    validate_approval_record_schema(record)
    return record


class ApprovalGate:
    """Human approval gate with a strict default-reject policy.

    The gate can be driven interactively (CLI) or non-interactively (tests,
    CI, non-TTY environments). In all non-interactive cases the answer is
    REJECTED immediately.
    """

    def __init__(self, interactive: Optional[bool] = None) -> None:
        """
        Args:
            interactive: Override the TTY detection. Pass True to force
                         interactive mode (tests), False to force non-interactive.
                         Leave None to auto-detect via sys.stdin.isatty().
        """
        if interactive is None:
            self._interactive = _is_interactive()
        else:
            self._interactive = interactive

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def review(
        self,
        proposal: Dict[str, Any],
        approved_by: str = "human",
        _answer: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Present the proposal to a human and return an approval record.

        Args:
            proposal: A FixProposal dict (status must be PROPOSED).
            approved_by: Identity string for the approver record.
            _answer: Inject an answer directly (used by tests to bypass stdin).
                     If supplied, interactive mode is bypassed regardless of
                     the ``interactive`` flag set at construction time.

        Returns:
            Approval record dict with status APPROVED or REJECTED.

        Raises:
            ProposalValidationError: If the proposal is not in PROPOSED status.
        """
        proposal_id = proposal.get("proposal_id", "UNKNOWN")
        status = proposal.get("status")
        if status != STATE_PROPOSED:
            raise ProposalValidationError(
                f"Cannot review proposal {proposal_id}: "
                f"status is {status!r}, expected 'PROPOSED'."
            )

        # Present the proposal summary to the user (always printed)
        self._print_proposal_summary(proposal)

        # Determine answer
        if _answer is not None:
            # Injected answer (test path)
            answer = _answer
        elif self._interactive:
            answer = self._prompt_for_decision(proposal_id)
        else:
            # Non-interactive: default REJECTED
            print(
                f"\n[ApprovalGate] Non-interactive mode detected. "
                f"Proposal {proposal_id} automatically REJECTED.",
                file=sys.stderr,
            )
            answer = ""

        decision, notes = self._evaluate_answer(answer)
        record = _build_approval_record(
            proposal_id=proposal_id,
            decision=decision,
            approved_by=approved_by,
            notes=notes,
        )
        self._print_decision(proposal_id, record["status"])
        return record

    def review_all(
        self,
        proposals_bundle: Dict[str, Any],
        approved_by: str = "human",
        _answers: Optional[Dict[str, str]] = None,
    ) -> Dict[str, Any]:
        """Review all proposals in a FixProposalAgent output bundle.

        Args:
            proposals_bundle: Output from FixProposalAgent.propose_fix().
            approved_by: Identity string for each approval record.
            _answers: Map of proposal_id -> answer string (test injection).

        Returns:
            Dict with:
                - incident_id
                - approval_records: list of approval record dicts
                - summary: counts of approved/rejected
        """
        incident_id = proposals_bundle.get("incident_id", "unknown")
        proposals = proposals_bundle.get("proposals") or []
        records = []
        for proposal in proposals:
            pid = proposal.get("proposal_id", "UNKNOWN")
            injected = (_answers or {}).get(pid)
            record = self.review(proposal, approved_by=approved_by, _answer=injected)
            records.append(record)

        approved_count = sum(1 for r in records if r.get("status") == STATE_APPROVED)
        rejected_count = sum(1 for r in records if r.get("status") == STATE_REJECTED)

        return {
            "incident_id": incident_id,
            "approval_records": records,
            "summary": {
                "total": len(records),
                "approved": approved_count,
                "rejected": rejected_count,
            },
        }

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _print_proposal_summary(self, proposal: Dict[str, Any]) -> None:
        pid = proposal.get("proposal_id", "?")
        hid = proposal.get("hypothesis_id", "?")
        summary = proposal.get("summary", "(no summary)")
        rationale = proposal.get("rationale", "")
        ev_ids = proposal.get("evidence_ids") or []
        changes = proposal.get("changes") or []
        risks = proposal.get("risks") or []

        print("\n" + "=" * 70)
        print(f"  FIX PROPOSAL: {pid}")
        print(f"  Hypothesis:   {hid}")
        print(f"  Incident:     {proposal.get('incident_id', '?')}")
        print("=" * 70)
        print(f"\nSummary:\n  {summary}")
        print(f"\nRationale:\n  {rationale}")
        print(f"\nEvidence IDs: {ev_ids}")
        if changes:
            print(f"\nProposed changes ({len(changes)}):")
            for ch in changes:
                f = ch.get("file", "?")
                desc = ch.get("description", "")
                sl = ch.get("start_line")
                el = ch.get("end_line")
                loc = f" (lines {sl}–{el})" if sl and el else ""
                print(f"  [{f}{loc}] {desc}")
        if risks:
            print("\nRisks:")
            for r in risks:
                print(f"  ⚠  {r}")
        val_plan = proposal.get("validation_plan") or []
        if val_plan:
            print("\nValidation plan:")
            for i, step in enumerate(val_plan, 1):
                print(f"  {i}. {step}")
        print(f"\nRollback plan:\n  {proposal.get('rollback_plan', '')}")
        patch = proposal.get("patch", "")
        if patch and patch.strip():
            preview = patch.strip()[:600]
            print(f"\nPatch preview (PROPOSED — not applied):\n{preview}")
            if len(patch.strip()) > 600:
                print("  ... [patch truncated for display]")
        print(f"\n⚠  {HUMAN_APPROVAL_NOTICE}")
        print("=" * 70)

    def _prompt_for_decision(self, proposal_id: str) -> str:
        """Read a line from stdin. Handles EOF gracefully."""
        try:
            answer = input(f"\nApprove proposal {proposal_id}? [y/N]: ")
        except EOFError:
            answer = ""
        except KeyboardInterrupt:
            answer = ""
        return answer

    def _evaluate_answer(self, answer: str) -> tuple[str, Optional[str]]:
        """Map raw input to ('approved'|'rejected', notes)."""
        stripped = (answer or "").strip().lower()
        if stripped in _AFFIRMATIVE:
            return "approved", None
        if stripped in ("n", "no"):
            return "rejected", "Explicit rejection."
        if stripped == "":
            return "rejected", "No input provided — defaulting to rejected."
        return "rejected", f"Unrecognized input {answer!r} — defaulting to rejected."

    def _print_decision(self, proposal_id: str, status: str) -> None:
        marker = "✅ APPROVED" if status == STATE_APPROVED else "❌ REJECTED"
        print(f"\n  Decision for {proposal_id}: {marker}")
        if status == STATE_APPROVED:
            print(
                "  ⚠  IMPORTANT: Approval records the human decision ONLY.\n"
                "     The patch has NOT been applied. No files were modified.\n"
                "     No git commits were made."
            )
