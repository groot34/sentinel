"""Run the Fix Proposal Agent + Approval Gate on incidents 01, 04, 07, 10.

Uses real Groq for proposal generation only.
Approval gate is driven non-interactively:
  - inc_01: approve FIX-001
  - inc_04: reject FIX-001
  - inc_07: approve FIX-001
  - inc_10: approve FIX-001, reject FIX-002 (if multiple)

Verifies that NO source files change after any approval.
Saves results under eval/sample_runs/.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict, Optional

REPO = Path(__file__).parent
INCIDENTS = REPO / "incidents"
SAMPLE_RUNS = REPO / "eval" / "sample_runs"

# Approval answers per incident: proposal_id -> answer
APPROVAL_ANSWERS: Dict[str, Dict[str, str]] = {
    "01": {"FIX-001": "y", "FIX-002": "n", "FIX-003": "n", "FIX-004": "n"},
    "04": {"FIX-001": "n", "FIX-002": "n"},
    "07": {"FIX-001": "y", "FIX-002": "n"},
    "10": {"FIX-001": "y", "FIX-002": "n", "FIX-003": "n", "FIX-004": "n"},
}


def _load_json(path: Path) -> Optional[Dict[str, Any]]:
    if not path.exists():
        return None
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _snapshot_service_files(inc_dir: Path) -> Dict[str, str]:
    """Record the content of all service/*.py files."""
    service_dir = inc_dir / "service"
    snapshots: Dict[str, str] = {}
    if service_dir.is_dir():
        for py_file in sorted(service_dir.rglob("*.py")):
            try:
                snapshots[str(py_file)] = py_file.read_text(encoding="utf-8")
            except OSError:
                pass
    return snapshots


def _verify_no_source_change(
    inc_dir: Path,
    before: Dict[str, str],
    after: Dict[str, str],
    label: str,
) -> None:
    for path, content in before.items():
        current = after.get(path, "MISSING")
        if current != content:
            raise RuntimeError(
                f"[{label}] SOURCE FILE CHANGED AFTER APPROVAL: {path}\n"
                "This is a critical safety violation."
            )
    new_files = set(after.keys()) - set(before.keys())
    if new_files:
        raise RuntimeError(
            f"[{label}] NEW FILES APPEARED after approval: {new_files}"
        )
    print(f"  [safety-check] No source files changed after approval. ✅")


def process_incident(inc_name: str, inc_suffix: str) -> None:
    inc_dir = INCIDENTS / inc_name
    print(f"\n=== Fix Proposal: {inc_name} ===")

    # Load existing verification output (or run from scratch if missing)
    ver_path = SAMPLE_RUNS / f"hypothesis_verification_inc_{inc_suffix}.json"
    if not ver_path.exists():
        print(f"  [ERROR] No verification output found at {ver_path.name}. Run run_hypothesis_verification.py first.")
        return

    combined = _load_json(ver_path)
    if combined is None:
        print(f"  [ERROR] Failed to load {ver_path.name}")
        return

    hypotheses = combined.get("hypotheses")
    verification = combined.get("verification")
    logs_evidence = combined.get("logs_evidence")
    metrics_evidence = combined.get("metrics_evidence")
    code_evidence = combined.get("code_evidence")

    if not hypotheses or not verification:
        print(f"  [ERROR] Missing hypotheses or verification in {ver_path.name}")
        return

    # Snapshot source files before
    before_snap = _snapshot_service_files(inc_dir)

    # Run Fix Proposal Agent
    from agents.fix_proposal_agent import FixProposalAgent
    agent = FixProposalAgent()
    print(f"  [run  ] FixProposalAgent...")
    result = agent.propose_fix(
        incident_dir=inc_dir,
        hypotheses=hypotheses,
        verification_results=verification,
        logs_evidence=logs_evidence,
        metrics_evidence=metrics_evidence,
        code_evidence=code_evidence,
    )

    print(f"  [info ] {len(result['proposals'])} proposal(s) generated.")
    for p in result["proposals"]:
        pid = p["proposal_id"]
        hid = p["hypothesis_id"]
        summary = p.get("summary", "")[:80]
        errs = result["validation_errors"].get(pid, [])
        err_str = f" [{len(errs)} validation error(s)]" if errs else " [valid]"
        print(f"    {pid} → {hid}: {summary}{err_str}")
    if result["skipped_hypotheses"]:
        for sk in result["skipped_hypotheses"]:
            print(f"    skip: {sk['hypothesis_id']} ({sk['verdict']})")

    # Save proposal output
    proposal_out = SAMPLE_RUNS / f"fix_proposal_inc_{inc_suffix}.json"
    SAMPLE_RUNS.mkdir(parents=True, exist_ok=True)
    proposal_out.write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(f"  [save ] -> {proposal_out.name}")

    # Run Approval Gate
    from agents.approval_gate import ApprovalGate
    answers = APPROVAL_ANSWERS.get(inc_suffix, {})
    gate = ApprovalGate(interactive=False)
    print(f"  [run  ] ApprovalGate (non-interactive)...")
    gate_result = gate.review_all(result, _answers=answers)

    after_snap = _snapshot_service_files(inc_dir)
    _verify_no_source_change(inc_dir, before_snap, after_snap, inc_name)

    # Save approval output
    approval_out = SAMPLE_RUNS / f"approval_inc_{inc_suffix}.json"
    approval_out.write_text(json.dumps(gate_result, indent=2), encoding="utf-8")
    print(f"  [save ] -> {approval_out.name}")

    for r in gate_result["approval_records"]:
        print(f"    {r['proposal_id']}: {r['status']}")
    print(f"  Summary: approved={gate_result['summary']['approved']}, rejected={gate_result['summary']['rejected']}")


def main() -> None:
    targets = [
        ("inc_01_n_plus_one_query", "01"),
        ("inc_04_memory_leak", "04"),
        ("inc_07_retry_storm", "07"),
        ("inc_10_multi_symptom_cascade", "10"),
    ]
    for inc_name, inc_suffix in targets:
        process_incident(inc_name, inc_suffix)

    print("\n=== Fix Proposal Run Complete ===")
    print("All source files verified unchanged after approval gates.")


if __name__ == "__main__":
    main()
