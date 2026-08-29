"""Fix Proposal Agent — generates a structured, human-reviewable remediation proposal
for each CONFIRMED hypothesis.

Safety contract:
- ONE Groq structured-generation call per proposal.
- The proposal is NEVER automatically applied.
- Ground truth files are NEVER read.
- Baseline results are NEVER read.
- The returned proposal always has status='PROPOSED' and the mandatory
  human_approval_notice.
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

from agents.fix_tools import (
    HUMAN_APPROVAL_NOTICE,
    ProposalValidationError,
    collect_all_evidence_ids,
    filter_confirmed_hypotheses,
    validate_proposal,
)
from core.llm import (
    GroqLLMClient,
    LLMError,
    LLMJSONParseError,
    get_llm_client,
)

SCHEMA_PATH = Path(__file__).parent.parent / "schemas" / "fix_proposal_schema.json"

_FIX_ID_RE = re.compile(r"^FIX-\d{3}$")


def _load_fix_schema() -> Dict[str, Any]:
    import json as _json
    with open(SCHEMA_PATH, "r", encoding="utf-8") as f:
        return _json.load(f)


def _read_service_files(incident_dir: Path) -> str:
    """Return a concatenated snippet of service source files (read-only)."""
    service_dir = incident_dir / "service"
    if not service_dir.is_dir():
        return "(no service directory found)"
    snippets: List[str] = []
    for py_file in sorted(service_dir.rglob("*.py")):
        rel = py_file.relative_to(incident_dir)
        rel_str = str(rel).replace("\\", "/")
        # Skip __pycache__ and test helpers
        if "__pycache__" in rel_str or rel_str.endswith("__init__.py"):
            continue
        if "/tests/" in rel_str:
            continue
        try:
            content = py_file.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        # Truncate very large files to keep context manageable
        if len(content) > 4000:
            content = content[:4000] + "\n... [truncated]"
        snippets.append(f"### File: service/{py_file.name}\n{content}")
    return "\n\n".join(snippets) if snippets else "(no Python service files found)"


def _read_git_diff(incident_dir: Path) -> str:
    """Read the git_diff.patch file for this incident (read-only)."""
    patch_file = incident_dir / "git_diff.patch"
    if not patch_file.exists():
        return "(no git_diff.patch found)"
    try:
        return patch_file.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return "(could not read git_diff.patch)"


def _evidence_summary_lines(
    logs: Optional[Dict[str, Any]],
    metrics: Optional[Dict[str, Any]],
    code: Optional[Dict[str, Any]],
) -> List[str]:
    lines: List[str] = []
    for bundle in (logs, metrics, code):
        if not isinstance(bundle, dict):
            continue
        for ev in bundle.get("evidence") or []:
            if not isinstance(ev, dict):
                continue
            eid = ev.get("evidence_id", "?")
            src = ev.get("source", "?")
            ref = ev.get("reference", "")
            exc = (ev.get("excerpt") or "").strip().replace("\n", " ⏎ ")[:200]
            interp = (ev.get("interpretation") or "").strip()[:200]
            lines.append(f"  {eid} [{src}] {ref}: {exc}")
            if interp:
                lines.append(f"    → {interp}")
    return lines


def _hypothesis_lines(hyp: Dict[str, Any], verification_result: Optional[Dict[str, Any]]) -> List[str]:
    lines = [
        f"  hypothesis_id: {hyp.get('hypothesis_id')}",
        f"  claim: {hyp.get('claim', '')}",
        f"  evidence_ids: {hyp.get('evidence_ids', [])}",
        f"  supporting_reasoning: {hyp.get('supporting_reasoning', '')}",
    ]
    if verification_result:
        lines.append(f"  verdict: {verification_result.get('verdict')}  confidence: {verification_result.get('confidence')}")
        lines.append(f"  verification_reasoning: {verification_result.get('reasoning', '')}")
        for chk in (verification_result.get("checks") or [])[:4]:
            lines.append(f"    check {chk.get('check_id')}: {chk.get('result')} — {chk.get('description', '')[:120]}")
    return lines


def _build_prompt(
    incident_id: str,
    hypothesis: Dict[str, Any],
    verification_result: Optional[Dict[str, Any]],
    source_code: str,
    git_diff: str,
    evidence_lines: List[str],
    proposal_id: str,
    all_evidence_ids: Set[str],
) -> str:
    hyp_lines = _hypothesis_lines(hypothesis, verification_result)
    ev_id_list = sorted(hypothesis.get("evidence_ids") or [])

    parts = [
        f"Incident: {incident_id}",
        f"Proposal ID to assign: {proposal_id}",
        "",
        "=== CONFIRMED HYPOTHESIS ===",
        *hyp_lines,
        "",
        f"Evidence IDs available for reference: {', '.join(sorted(all_evidence_ids))}",
        "Evidence IDs supporting this hypothesis (use these in evidence_ids):",
        f"  {ev_id_list}",
        "",
        "=== EVIDENCE SUMMARY ===",
        *(evidence_lines or ["(none)"]),
        "",
        "=== INCIDENT SERVICE SOURCE CODE (read-only, DO NOT write to these files) ===",
        source_code[:6000],
        "",
        "=== GIT DIFF (shows the change that introduced the bug) ===",
        git_diff[:3000],
        "",
        "=== YOUR TASK ===",
        "Generate a fix proposal JSON object with these exact fields and constraints:",
        f"  proposal_id: \"{proposal_id}\"   (must be exactly this value)",
        f"  hypothesis_id: \"{hypothesis.get('hypothesis_id')}\"   (must be exactly this value)",
        f"  incident_id: \"{incident_id}\"",
        "  status: \"PROPOSED\"   (must be exactly this value)",
        f"  human_approval_notice: \"{HUMAN_APPROVAL_NOTICE}\"   (must be exactly this string)",
        "  summary: one-sentence description of the proposed change",
        "  rationale: explanation of why this change fixes the confirmed root cause",
        "  changes: array of file change objects (file, start_line|null, end_line|null, description, before, after)",
        "    - file must be a relative path starting with 'service/' (e.g. 'service/app.py')",
        "    - before: the EXACT code snippet to be replaced (copy from source above)",
        "    - after: the proposed replacement code",
        "    - If you cannot determine the exact line numbers, set start_line and end_line to null",
        "  patch: the proposed unified diff in standard git diff format — CLEARLY LABELLED AS PROPOSED",
        "  expected_effect: observable improvement after this fix",
        "  risks: array of strings listing potential regressions or side effects",
        "  validation_plan: array of concrete steps to verify the fix works correctly",
        "  rollback_plan: how to safely revert if the fix causes a regression",
        f"  evidence_ids: {ev_id_list}   (use exactly these IDs, no others)",
        "",
        "CRITICAL RULES:",
        "  - DO NOT claim the fix has been applied, deployed, or committed.",
        "  - DO NOT include shell commands, os.system(), subprocess calls, git commands, rm, DROP TABLE.",
        "  - DO NOT reference files outside service/.",
        "  - DO NOT reference evaluation files, ground truth files, or benchmark files.",
        "  - DO NOT fabricate evidence IDs not listed above.",
        "  - The proposal is READ-ONLY output. It will be reviewed by a human before any action.",
        "",
        "Respond with a valid JSON object only — no markdown, no extra text.",
    ]
    return "\n".join(parts)


def _assign_fix_id(index: int) -> str:
    return f"FIX-{index:03d}"


def _normalise_proposal(
    raw: Any,
    proposal_id: str,
    hypothesis_id: str,
    incident_id: str,
    ev_ids: List[str],
) -> Dict[str, Any]:
    """Force-set the immutable fields that the LLM must not override."""
    if not isinstance(raw, dict):
        raise LLMJSONParseError("Fix proposal LLM response is not a JSON object.")
    raw["proposal_id"] = proposal_id
    raw["hypothesis_id"] = hypothesis_id
    raw["incident_id"] = incident_id
    raw["status"] = "PROPOSED"
    raw["human_approval_notice"] = HUMAN_APPROVAL_NOTICE

    # Ensure evidence_ids are valid (only from the hypothesis)
    given_ev = raw.get("evidence_ids")
    if not isinstance(given_ev, list) or not given_ev:
        raw["evidence_ids"] = ev_ids

    # changes must be a non-empty list
    changes = raw.get("changes")
    if not isinstance(changes, list) or len(changes) == 0:
        raw["changes"] = [
            {
                "file": "service/app.py",
                "start_line": None,
                "end_line": None,
                "description": "See patch for details.",
                "before": "",
                "after": "",
            }
        ]

    # Ensure required string fields have a fallback value
    for key in ("summary", "rationale", "patch", "expected_effect", "rollback_plan"):
        if not raw.get(key):
            raw[key] = f"(see proposal details for {key})"

    # Ensure list fields exist
    if not isinstance(raw.get("risks"), list):
        raw["risks"] = []
    if not isinstance(raw.get("validation_plan"), list) or not raw["validation_plan"]:
        raw["validation_plan"] = ["Run existing unit tests against the patched service."]

    # Force start_line/end_line to None if they're not integers or are absent
    for change in raw.get("changes") or []:
        if isinstance(change, dict):
            for line_key in ("start_line", "end_line"):
                if not isinstance(change.get(line_key), int):
                    change[line_key] = None

    return raw


class FixProposalAgent:
    """Generates remediation proposals for CONFIRMED hypotheses.

    Safety contract:
    - Exactly one Groq structured call per confirmed hypothesis.
    - The proposal is NEVER automatically applied.
    - Human approval is NEVER simulated by Groq.
    - Ground truth and baseline files are NEVER read.
    """

    HUMAN_APPROVAL_NOTICE = HUMAN_APPROVAL_NOTICE

    def __init__(self, llm_client: Optional[GroqLLMClient] = None) -> None:
        self.llm_client = llm_client or get_llm_client()
        self._schema = _load_fix_schema()

    def propose_fix(
        self,
        incident_dir: Path | str,
        hypotheses: Dict[str, Any],
        verification_results: Dict[str, Any],
        logs_evidence: Optional[Dict[str, Any]] = None,
        metrics_evidence: Optional[Dict[str, Any]] = None,
        code_evidence: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Generate fix proposals for all CONFIRMED hypotheses in the incident.

        Args:
            incident_dir: Path to the incident bundle directory.
            hypotheses: Output from HypothesisEngine.generate_hypotheses().
            verification_results: Output from VerificationAgent.verify().
            logs_evidence: Output from LogsAgent (optional).
            metrics_evidence: Output from MetricsAgent (optional).
            code_evidence: Output from CodeAgent (optional).

        Returns:
            Dict with:
                - incident_id
                - proposals: list of FixProposal dicts (one per confirmed hypothesis)
                - validation_errors: dict mapping proposal_id -> list of errors (empty if valid)
                - skipped_hypotheses: list of non-CONFIRMED hypothesis IDs with their verdicts
        """
        incident_path = Path(incident_dir)
        if not incident_path.is_dir():
            raise FileNotFoundError(f"Incident directory not found: {incident_dir}")
        incident_id = incident_path.name

        all_ev_ids = collect_all_evidence_ids(logs_evidence, metrics_evidence, code_evidence)
        confirmed = filter_confirmed_hypotheses(hypotheses, verification_results)

        # Track which hypotheses are skipped
        verdict_map: Dict[str, str] = {}
        for r in verification_results.get("results", []):
            if isinstance(r, dict):
                verdict_map[r.get("hypothesis_id", "")] = r.get("verdict", "")
        skipped: List[Dict[str, str]] = []
        for h in hypotheses.get("hypotheses", []):
            if isinstance(h, dict):
                hid = h.get("hypothesis_id", "")
                v = verdict_map.get(hid, "UNKNOWN")
                if v != "CONFIRMED":
                    skipped.append({"hypothesis_id": hid, "verdict": v})

        if not confirmed:
            return {
                "incident_id": incident_id,
                "proposals": [],
                "validation_errors": {},
                "skipped_hypotheses": skipped,
                "message": "No CONFIRMED hypotheses found. No fix proposals generated.",
            }

        # Read source context once (read-only)
        source_code = _read_service_files(incident_path)
        git_diff = _read_git_diff(incident_path)
        evidence_lines = _evidence_summary_lines(logs_evidence, metrics_evidence, code_evidence)

        # Build verification result lookup
        ver_by_hyp: Dict[str, Dict[str, Any]] = {}
        for r in verification_results.get("results", []):
            if isinstance(r, dict):
                ver_by_hyp[r.get("hypothesis_id", "")] = r

        proposals: List[Dict[str, Any]] = []
        validation_errors: Dict[str, List[str]] = {}

        for idx, hyp in enumerate(confirmed, start=1):
            proposal_id = _assign_fix_id(idx)
            hyp_id = hyp.get("hypothesis_id", f"HYP-{idx:03d}")
            ev_ids_for_hyp = list(hyp.get("evidence_ids") or [])
            ver_result = ver_by_hyp.get(hyp_id)

            prompt = _build_prompt(
                incident_id=incident_id,
                hypothesis=hyp,
                verification_result=ver_result,
                source_code=source_code,
                git_diff=git_diff,
                evidence_lines=evidence_lines,
                proposal_id=proposal_id,
                all_evidence_ids=all_ev_ids,
            )
            system_prompt = (
                "You are a Sentinel fix proposal engineer. "
                "You generate precise, evidence-backed remediation proposals for confirmed incident root causes. "
                "You never apply fixes, never execute code, never commit changes, and never fabricate evidence. "
                "Respond with a valid JSON object only — no markdown, no commentary."
            )

            try:
                resp = self.llm_client.generate_structured(
                    prompt=prompt,
                    schema=self._schema,
                    system_prompt=system_prompt,
                    temperature=0.0,
                )
                raw = resp.get_structured()
            except LLMJSONParseError as exc:
                # Create a minimal safe fallback proposal
                raw = {}

            proposal = _normalise_proposal(
                raw=raw,
                proposal_id=proposal_id,
                hypothesis_id=hyp_id,
                incident_id=incident_id,
                ev_ids=ev_ids_for_hyp,
            )

            is_valid, errors = validate_proposal(
                proposal=proposal,
                incident_dir=incident_path,
                available_hypotheses=hypotheses,
                verification_results=verification_results,
                all_evidence_ids=all_ev_ids,
            )
            if errors:
                validation_errors[proposal_id] = errors

            proposals.append(proposal)

        return {
            "incident_id": incident_id,
            "proposals": proposals,
            "validation_errors": validation_errors,
            "skipped_hypotheses": skipped,
        }


def _load_json(path: Path) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _cli() -> None:
    parser = argparse.ArgumentParser(description="Sentinel Fix Proposal Agent CLI")
    parser.add_argument("incident_dir", type=Path, help="Path to the incident bundle directory")
    parser.add_argument("--hypotheses", type=Path, required=True, help="Hypotheses JSON file")
    parser.add_argument("--verification", type=Path, required=True, help="Verification results JSON file")
    parser.add_argument("--logs", type=Path)
    parser.add_argument("--metrics", type=Path)
    parser.add_argument("--code", type=Path)
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()

    agent = FixProposalAgent()
    hypotheses = _load_json(args.hypotheses)
    verification = _load_json(args.verification)
    logs = _load_json(args.logs) if args.logs and args.logs.exists() else None
    metrics = _load_json(args.metrics) if args.metrics and args.metrics.exists() else None
    code = _load_json(args.code) if args.code and args.code.exists() else None

    result = agent.propose_fix(
        incident_dir=args.incident_dir,
        hypotheses=hypotheses,
        verification_results=verification,
        logs_evidence=logs,
        metrics_evidence=metrics,
        code_evidence=code,
    )
    text = json.dumps(result, indent=2)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(text, encoding="utf-8")
    else:
        print(text)


if __name__ == "__main__":
    _cli()
