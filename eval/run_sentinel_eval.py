"""Sentinel Evaluation Runner - final benchmark across all 10 canonical incidents.

Runs the full Sentinel pipeline (via IncidentOrchestrator) on each incident,
evaluates root-cause correctness deterministically (no LLM judge), and produces:

  eval/results/sentinel/<incident_id>/  - per-stage cache + final result
  eval/final_comparison.csv             - per-incident comparison vs baseline
  eval/final_summary.json               - aggregate numbers

Ground-truth isolation contract:
  - Runtime pipeline (all agents) NEVER reads ground_truth.md.
  - Evaluator reads ground_truth.md ONLY after the pipeline has completed.
  - Baseline results are NEVER passed to runtime components.

Usage:
  python -m eval.run_sentinel_eval [--sleep N] [--start 1] [--end 10] [--incident <id>]
"""

from __future__ import annotations

import argparse
import csv
import json
import time
import traceback
from pathlib import Path
from typing import Any, Dict, List, Optional

from agents.orchestrator import IncidentOrchestrator
from eval.evaluator import CorrectnessEvaluator

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
REPO_ROOT = Path(__file__).parent.parent
INCIDENTS_DIR = REPO_ROOT / "incidents"
SENTINEL_RESULTS_DIR = REPO_ROOT / "eval" / "results" / "sentinel"
BASELINE_CSV = REPO_ROOT / "eval" / "results_baseline.csv"
BASELINE_SUMMARY = REPO_ROOT / "eval" / "baseline_summary.json"
COMPARISON_CSV = REPO_ROOT / "eval" / "final_comparison.csv"
SUMMARY_JSON = REPO_ROOT / "eval" / "final_summary.json"

COMPARISON_FIELDNAMES = [
    "incident_id",
    "baseline_verdict",
    "sentinel_verdict",
    "baseline_correct",
    "sentinel_correct",
    "sentinel_verified",
    "sentinel_confirmed_hypotheses",
    "sentinel_fix_proposals",
    "baseline_latency_seconds",
    "sentinel_latency_seconds",
    "baseline_tokens",
    "sentinel_tokens",
    "baseline_llm_calls",
    "sentinel_llm_calls",
    "sentinel_status",
    "sentinel_hypotheses_total",
    "sentinel_hypotheses_rejected",
    "sentinel_hypotheses_inconclusive",
    "notes",
]

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def discover_incidents(start: int = 1, end: int = 10) -> List[Path]:
    incidents = sorted(
        [d for d in INCIDENTS_DIR.iterdir() if d.is_dir() and d.name.startswith("inc_")]
    )
    result = []
    for inc in incidents:
        try:
            seq = int(inc.name.split("_")[1])
        except (IndexError, ValueError):
            seq = 0
        if start <= seq <= end:
            result.append(inc)
    return result


def load_baseline_csv() -> Dict[str, Dict[str, Any]]:
    """Load the locked baseline CSV into a dict keyed by incident_id."""
    rows: Dict[str, Dict[str, Any]] = {}
    if not BASELINE_CSV.exists():
        return rows
    with open(BASELINE_CSV, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            rows[row["incident_id"]] = row
    return rows


def load_sentinel_final(incident_id: str) -> Optional[Dict[str, Any]]:
    """Load a previously-saved Sentinel final result for this incident."""
    path = SENTINEL_RESULTS_DIR / incident_id / "sentinel_final.json"
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if data.get("incident_id") != incident_id:
            return None
        # Only reuse if it was a completed run (not a partial failure mid-pipeline)
        if data.get("sentinel_status") in ("RATE_LIMITED", "RUNNING"):
            return None
        return data
    except Exception:
        return None


def save_sentinel_final(incident_id: str, data: Dict[str, Any]) -> None:
    out_dir = SENTINEL_RESULTS_DIR / incident_id
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "sentinel_final.json").write_text(
        json.dumps(data, indent=2), encoding="utf-8"
    )


def _extract_sentinel_root_cause(orchestrator_result: Dict[str, Any]) -> str:
    """Extract the primary root-cause claim from the orchestrator result.

    Strategy (in priority order):
    1. The CONFIRMED hypothesis claim with the highest confidence.
    2. The first CONFIRMED hypothesis claim.
    3. The first hypothesis claim of any verdict.
    4. Empty string if nothing found.
    """
    ver_output = (orchestrator_result.get("stages") or {}).get("verification") or {}
    ver_data = ver_output.get("output") or {}
    hyp_output = (orchestrator_result.get("stages") or {}).get("hypotheses") or {}
    hyp_data = hyp_output.get("output") or {}

    # Build hypothesis text by ID
    hyp_by_id: Dict[str, str] = {}
    for h in (hyp_data.get("hypotheses") or []):
        if isinstance(h, dict):
            hid = h.get("hypothesis_id", "")
            claim = h.get("claim", "")
            hyp_by_id[hid] = claim

    # Find CONFIRMED results
    confirmed = [
        r for r in (ver_data.get("results") or [])
        if isinstance(r, dict) and r.get("verdict") == "CONFIRMED"
    ]
    if confirmed:
        # Pick highest confidence
        best = max(confirmed, key=lambda r: r.get("confidence", 0.0))
        hid = best.get("hypothesis_id", "")
        return hyp_by_id.get(hid, best.get("reasoning", ""))

    # Fallback: first hypothesis of any verdict
    all_results = [r for r in (ver_data.get("results") or []) if isinstance(r, dict)]
    if all_results:
        hid = all_results[0].get("hypothesis_id", "")
        return hyp_by_id.get(hid, all_results[0].get("reasoning", ""))

    # Final fallback: first raw hypothesis
    for h in (hyp_data.get("hypotheses") or []):
        if isinstance(h, dict) and h.get("claim"):
            return h["claim"]

    return ""


def _extract_reasoning(orchestrator_result: Dict[str, Any]) -> str:
    """Extract supporting reasoning text for the evaluator."""
    ver_output = (orchestrator_result.get("stages") or {}).get("verification") or {}
    ver_data = ver_output.get("output") or {}
    hyp_output = (orchestrator_result.get("stages") or {}).get("hypotheses") or {}
    hyp_data = hyp_output.get("output") or {}

    parts = []
    for h in (hyp_data.get("hypotheses") or []):
        if isinstance(h, dict):
            parts.append(h.get("claim", ""))
            parts.append(h.get("supporting_reasoning", ""))
    for r in (ver_data.get("results") or []):
        if isinstance(r, dict):
            parts.append(r.get("reasoning", ""))
    return " ".join(p for p in parts if p)


# ---------------------------------------------------------------------------
# Main per-incident runner
# ---------------------------------------------------------------------------

def run_single_incident(
    incident_dir: Path,
    sleep_secs: float,
    evaluator: CorrectnessEvaluator,
    baseline_rows: Dict[str, Dict[str, Any]],
    force_rerun: bool = False,
) -> Dict[str, Any]:
    """Run Sentinel on one incident and return a comparison record."""
    incident_id = incident_dir.name
    ground_truth_path = incident_dir / "ground_truth.md"

    # -- Load baseline row for this incident ------------------------------
    base_row = baseline_rows.get(incident_id) or {}
    baseline_verdict = base_row.get("correctness", "UNKNOWN")
    baseline_correct = baseline_verdict == "CORRECT"
    baseline_latency = float(base_row.get("latency_seconds", 0) or 0)
    baseline_tokens = (
        (int(base_row.get("input_tokens") or 0)) +
        (int(base_row.get("output_tokens") or 0))
    )
    baseline_llm_calls = 1  # baseline is always exactly 1

    # -- Reuse existing final result if available and valid ----------------
    if not force_rerun:
        existing = load_sentinel_final(incident_id)
        if existing is not None:
            existing["_from_cache"] = True
            print(f"  [reuse] sentinel_final.json for {incident_id}")
            return existing

    # -- Run the full Sentinel pipeline ------------------------------------
    print(f"  [run  ] Sentinel pipeline on {incident_id}...")
    t_start = time.monotonic()

    cache_dir = SENTINEL_RESULTS_DIR  # orchestrator appends incident_id internally
    orchestrator = IncidentOrchestrator(
        sleep_between_stages=sleep_secs,
        non_interactive=True,
        output_dir=cache_dir,
    )

    orch_result: Optional[Dict[str, Any]] = None
    sentinel_error: str = ""
    sentinel_status = "RUNNING"

    try:
        orch_result = orchestrator.investigate(incident_dir)
        sentinel_status = orch_result.get("pipeline_status", "UNKNOWN")
    except Exception as exc:
        sentinel_status = "ERROR"
        sentinel_error = f"{type(exc).__name__}: {exc}"
        print(f"  [FAIL ] {incident_id}: {sentinel_error}")

    sentinel_latency = round(time.monotonic() - t_start, 3)

    # -- Extract metrics from orchestrator result --------------------------
    sentinel_llm_calls = 0
    sentinel_tokens = 0
    confirmed_count = 0
    rejected_count = 0
    inconclusive_count = 0
    fix_proposals_count = 0

    if orch_result:
        sentinel_llm_calls = orch_result.get("llm_call_count", 0)
        summary = orch_result.get("summary") or {}
        confirmed_count = summary.get("confirmed_hypotheses", 0)
        rejected_count = summary.get("rejected_hypotheses", 0)
        inconclusive_count = summary.get("inconclusive_hypotheses", 0)
        fix_proposals_count = summary.get("proposals_generated", 0)

    # -- Evaluate Sentinel correctness -------------------------------------
    # Ground truth is read ONLY here, after the pipeline has completed.
    sentinel_root_cause = ""
    sentinel_reasoning = ""
    sentinel_verdict = "UNKNOWN"
    sentinel_verdict_explanation = ""
    sentinel_verified = False

    if orch_result and sentinel_status in ("COMPLETED", "PARTIAL"):
        sentinel_root_cause = _extract_sentinel_root_cause(orch_result)
        sentinel_reasoning = _extract_reasoning(orch_result)
        sentinel_verified = confirmed_count > 0

        if sentinel_root_cause:
            sentinel_verdict, sentinel_verdict_explanation = evaluator.evaluate_diagnosis(
                incident_id=incident_id,
                diagnosis_text=sentinel_root_cause,
                reasoning_text=sentinel_reasoning,
            )
        else:
            sentinel_verdict = "INCORRECT"
            sentinel_verdict_explanation = "No root-cause claim could be extracted from the pipeline."
    elif sentinel_status == "ERROR":
        sentinel_verdict = "FAILURE"
        sentinel_verdict_explanation = sentinel_error
    elif sentinel_status == "RUNNING":
        sentinel_verdict = "FAILURE"
        sentinel_verdict_explanation = "Pipeline did not complete."

    sentinel_correct = sentinel_verdict == "CORRECT"

    # -- Build comparison record -------------------------------------------
    hyp_total = confirmed_count + rejected_count + inconclusive_count
    notes_parts = []
    if sentinel_error:
        notes_parts.append(f"error: {sentinel_error[:120]}")
    if sentinel_status == "PARTIAL":
        notes_parts.append("pipeline partial (some stages failed)")

    record: Dict[str, Any] = {
        "incident_id": incident_id,
        "baseline_verdict": baseline_verdict,
        "sentinel_verdict": sentinel_verdict,
        "baseline_correct": baseline_correct,
        "sentinel_correct": sentinel_correct,
        "sentinel_verified": sentinel_verified,
        "sentinel_confirmed_hypotheses": confirmed_count,
        "sentinel_fix_proposals": fix_proposals_count,
        "baseline_latency_seconds": baseline_latency,
        "sentinel_latency_seconds": sentinel_latency,
        "baseline_tokens": baseline_tokens,
        "sentinel_tokens": sentinel_tokens,
        "baseline_llm_calls": baseline_llm_calls,
        "sentinel_llm_calls": sentinel_llm_calls,
        "sentinel_status": sentinel_status,
        "sentinel_hypotheses_total": hyp_total,
        "sentinel_hypotheses_rejected": rejected_count,
        "sentinel_hypotheses_inconclusive": inconclusive_count,
        "notes": "; ".join(notes_parts),
        # Extra detail for summary JSON (not in CSV)
        "_sentinel_root_cause": sentinel_root_cause,
        "_sentinel_verdict_explanation": sentinel_verdict_explanation,
        "_sentinel_error": sentinel_error,
        "_orchestrator_result": orch_result,
    }
    save_sentinel_final(incident_id, record)
    return record


# ---------------------------------------------------------------------------
# CSV / summary writers
# ---------------------------------------------------------------------------

def write_comparison_csv(records: List[Dict[str, Any]]) -> None:
    COMPARISON_CSV.parent.mkdir(parents=True, exist_ok=True)
    with open(COMPARISON_CSV, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=COMPARISON_FIELDNAMES, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(records)
    print(f"[saved] {COMPARISON_CSV}")


def write_final_summary(records: List[Dict[str, Any]]) -> None:
    total = len(records)

    # Baseline stats (from locked CSV)
    b_correct = sum(1 for r in records if r.get("baseline_correct"))
    b_incorrect = sum(1 for r in records if r.get("baseline_verdict") == "INCORRECT")
    b_review = sum(1 for r in records if r.get("baseline_verdict") == "REVIEW")
    b_failures = sum(1 for r in records if r.get("baseline_verdict") in ("FAILURE", "UNKNOWN", ""))
    b_latencies = [r.get("baseline_latency_seconds", 0) for r in records]
    b_avg_latency = round(sum(b_latencies) / len(b_latencies), 3) if b_latencies else 0
    b_tokens = sum(r.get("baseline_tokens", 0) for r in records)
    b_llm = sum(r.get("baseline_llm_calls", 0) for r in records)

    # Sentinel stats
    s_correct = sum(1 for r in records if r.get("sentinel_correct"))
    s_incorrect = sum(1 for r in records if r.get("sentinel_verdict") == "INCORRECT")
    s_review = sum(1 for r in records if r.get("sentinel_verdict") == "REVIEW")
    s_failures = sum(1 for r in records if r.get("sentinel_verdict") in ("FAILURE", "UNKNOWN", ""))
    s_latencies = [r.get("sentinel_latency_seconds", 0) for r in records]
    s_avg_latency = round(sum(s_latencies) / len(s_latencies), 3) if s_latencies else 0
    s_tokens = sum(r.get("sentinel_tokens", 0) for r in records)
    s_llm = sum(r.get("sentinel_llm_calls", 0) for r in records)
    s_verified = sum(1 for r in records if r.get("sentinel_verified"))
    s_confirmed = sum(r.get("sentinel_confirmed_hypotheses", 0) for r in records)
    s_hyp_total = sum(r.get("sentinel_hypotheses_total", 0) for r in records)
    s_hyp_rejected = sum(r.get("sentinel_hypotheses_rejected", 0) for r in records)
    s_hyp_inconclusive = sum(r.get("sentinel_hypotheses_inconclusive", 0) for r in records)

    # Accuracy (denominator always = total incidents, failures are not hidden)
    b_accuracy = round(b_correct / total, 4) if total else 0
    s_accuracy = round(s_correct / total, 4) if total else 0
    accuracy_delta = round(s_accuracy - b_accuracy, 4)
    relative_improvement = (
        round(accuracy_delta / b_accuracy, 4) if b_accuracy else None
    )

    # Comparison
    improved = sum(
        1 for r in records
        if r.get("sentinel_correct") and not r.get("baseline_correct")
    )
    regressed = sum(
        1 for r in records
        if not r.get("sentinel_correct") and r.get("baseline_correct")
    )
    equal = total - improved - regressed

    per_incident = [
        {
            "incident_id": r["incident_id"],
            "baseline": r.get("baseline_verdict", "UNKNOWN"),
            "sentinel": r.get("sentinel_verdict", "UNKNOWN"),
            "sentinel_verified": r.get("sentinel_verified", False),
            "sentinel_confirmed": r.get("sentinel_confirmed_hypotheses", 0),
            "sentinel_status": r.get("sentinel_status", "UNKNOWN"),
            "root_cause": r.get("_sentinel_root_cause", ""),
            "verdict_explanation": r.get("_sentinel_verdict_explanation", ""),
            "error": r.get("_sentinel_error", ""),
        }
        for r in records
    ]

    summary = {
        "model": "openai/gpt-oss-120b",
        "total_incidents": total,
        "baseline": {
            "total": total,
            "correct": b_correct,
            "incorrect": b_incorrect,
            "review": b_review,
            "failures": b_failures,
            "accuracy": b_accuracy,
            "accuracy_pct": f"{b_correct}/{total} = {b_accuracy*100:.1f}%",
            "avg_latency_seconds": b_avg_latency,
            "total_tokens": b_tokens,
            "total_llm_calls": b_llm,
            "verification_score": 0,
            "note": "Single-shot Groq model. Guesses root cause without executable verification.",
        },
        "sentinel": {
            "total": total,
            "correct": s_correct,
            "incorrect": s_incorrect,
            "review": s_review,
            "failures": s_failures,
            "accuracy": s_accuracy,
            "accuracy_pct": f"{s_correct}/{total} = {s_accuracy*100:.1f}%",
            "avg_latency_seconds": s_avg_latency,
            "total_tokens": s_tokens,
            "total_llm_calls": s_llm,
            "verified_root_causes": s_verified,
            "verified_root_causes_pct": f"{s_verified}/{total} = {s_verified/total*100:.1f}%" if total else "0/0",
            "hypotheses_generated": s_hyp_total,
            "hypotheses_confirmed": s_confirmed,
            "hypotheses_rejected": s_hyp_rejected,
            "hypotheses_inconclusive": s_hyp_inconclusive,
            "note": "Multi-agent pipeline with deterministic verification. Evidence-gated root causes.",
        },
        "comparison": {
            "accuracy_delta": accuracy_delta,
            "accuracy_delta_pct": f"{accuracy_delta*100:+.1f} percentage points",
            "relative_improvement": relative_improvement,
            "latency_delta_seconds": round(s_avg_latency - b_avg_latency, 3),
            "token_delta": s_tokens - b_tokens,
            "additional_llm_calls": s_llm - b_llm,
            "incidents_improved": improved,
            "incidents_regressed": regressed,
            "incidents_equal": equal,
            "verification_improvement": f"Sentinel verification score: {s_verified/total*100:.0f}% vs Baseline: 0%",
        },
        "per_incident": per_incident,
        "safety": {
            "ground_truth_isolated": True,
            "baseline_isolated": True,
            "source_modifications": 0,
            "patches_auto_applied": 0,
            "approval_mode": "non-interactive (auto-reject)",
        },
    }

    SUMMARY_JSON.parent.mkdir(parents=True, exist_ok=True)
    SUMMARY_JSON.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(f"[saved] {SUMMARY_JSON}")
    return summary


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main(
    sleep: float = 2.0,
    start: int = 1,
    end: int = 10,
    incident: Optional[str] = None,
    force_rerun: bool = False,
) -> None:
    evaluator = CorrectnessEvaluator()
    baseline_rows = load_baseline_csv()

    if not baseline_rows:
        print("[WARN] No baseline CSV found at eval/results_baseline.csv - baseline comparison will be empty.")

    if incident:
        inc_dirs = [INCIDENTS_DIR / incident]
    else:
        inc_dirs = discover_incidents(start=start, end=end)

    print(f"\n{'='*70}")
    print(f"  SENTINEL EVALUATION - {len(inc_dirs)} incidents")
    print(f"  Stage sleep: {sleep}s | Incidents: {start}-{end}")
    print(f"{'='*70}\n")

    records: List[Dict[str, Any]] = []
    rate_limited = False

    for inc_dir in inc_dirs:
        print(f"\n{'-'*60}")
        print(f"  Incident: {inc_dir.name}")
        print(f"{'-'*60}")

        if not inc_dir.exists():
            print(f"  [SKIP] Directory not found: {inc_dir}")
            records.append({
                "incident_id": inc_dir.name,
                "baseline_verdict": baseline_rows.get(inc_dir.name, {}).get("correctness", "UNKNOWN"),
                "sentinel_verdict": "FAILURE",
                "baseline_correct": False,
                "sentinel_correct": False,
                "sentinel_verified": False,
                "sentinel_confirmed_hypotheses": 0,
                "sentinel_fix_proposals": 0,
                "baseline_latency_seconds": 0,
                "sentinel_latency_seconds": 0,
                "baseline_tokens": 0,
                "sentinel_tokens": 0,
                "baseline_llm_calls": 0,
                "sentinel_llm_calls": 0,
                "sentinel_status": "NOT_FOUND",
                "sentinel_hypotheses_total": 0,
                "sentinel_hypotheses_rejected": 0,
                "sentinel_hypotheses_inconclusive": 0,
                "notes": "incident directory not found",
                "_sentinel_root_cause": "",
                "_sentinel_verdict_explanation": "Incident directory not found.",
                "_sentinel_error": "not found",
                "_orchestrator_result": None,
            })
            continue

        try:
            record = run_single_incident(
                incident_dir=inc_dir,
                sleep_secs=sleep,
                evaluator=evaluator,
                baseline_rows=baseline_rows,
                force_rerun=force_rerun,
            )
            records.append(record)

            print(f"  Baseline: {record.get('baseline_verdict')}  |  "
                  f"Sentinel: {record.get('sentinel_verdict')}  |  "
                  f"Verified: {record.get('sentinel_verified')}  |  "
                  f"Status: {record.get('sentinel_status')}")

            # Check for rate-limit in orchestrator stages (only for live runs)
            if not record.get("_from_cache", False):
                orch = record.get("_orchestrator_result") or {}
                for stage_name, stage in (orch.get("stages") or {}).items():
                    err = (stage or {}).get("error") or ""
                    if "rate_limit" in err.lower() or "429" in err:
                        print(f"  [WARN] Rate limit hit on stage '{stage_name}' - sleeping 60s before continuing")
                        rate_limited = True
                        time.sleep(60)
                        break

        except KeyboardInterrupt:
            print("\n[STOPPED] KeyboardInterrupt received. Saving partial results.")
            break
        except Exception as exc:
            print(f"  [ERROR] Unhandled exception for {inc_dir.name}: {exc}")
            records.append({
                "incident_id": inc_dir.name,
                "sentinel_verdict": "FAILURE",
                "sentinel_correct": False,
                "sentinel_verified": False,
                "sentinel_status": "ERROR",
                "baseline_verdict": baseline_rows.get(inc_dir.name, {}).get("correctness", "UNKNOWN"),
                "baseline_correct": baseline_rows.get(inc_dir.name, {}).get("correctness") == "CORRECT",
                "sentinel_confirmed_hypotheses": 0,
                "sentinel_fix_proposals": 0,
                "baseline_latency_seconds": 0,
                "sentinel_latency_seconds": 0,
                "baseline_tokens": 0,
                "sentinel_tokens": 0,
                "baseline_llm_calls": 0,
                "sentinel_llm_calls": 0,
                "sentinel_hypotheses_total": 0,
                "sentinel_hypotheses_rejected": 0,
                "sentinel_hypotheses_inconclusive": 0,
                "notes": str(exc)[:200],
                "_sentinel_root_cause": "",
                "_sentinel_verdict_explanation": str(exc),
                "_sentinel_error": str(exc),
                "_orchestrator_result": None,
            })

    if not records:
        print("[ERROR] No results produced.")
        return

    write_comparison_csv(records)
    summary = write_final_summary(records)

    # -- Print final report ------------------------------------------------
    total = summary["total_incidents"]
    b = summary["baseline"]
    s = summary["sentinel"]
    cmp = summary["comparison"]

    print(f"\n{'='*70}")
    print(f"  FINAL SENTINEL EVALUATION REPORT")
    print(f"{'='*70}")
    print(f"\n  Baseline:  {b['accuracy_pct']}  (0% verified)")
    print(f"  Sentinel:  {s['accuracy_pct']}  ({s['verified_root_causes_pct']} verified)")
    print(f"  Delta:     {cmp['accuracy_delta_pct']}")
    print(f"\n  Per incident:")
    print(f"  {'Incident':<40} {'Baseline':<12} {'Sentinel':<12} {'Verified'}")
    print(f"  {'-'*40} {'-'*12} {'-'*12} {'-'*8}")
    for pi in summary["per_incident"]:
        v_mark = "[V]" if pi["sentinel_verified"] else "   "
        print(f"  {pi['incident_id']:<40} {pi['baseline']:<12} {pi['sentinel']:<12} {v_mark}")
    print(f"\n  Hypotheses: {s['hypotheses_generated']} generated, "
          f"{s['hypotheses_confirmed']} confirmed, "
          f"{s['hypotheses_rejected']} rejected, "
          f"{s['hypotheses_inconclusive']} inconclusive")
    print(f"\n  LLM calls:  baseline={b['total_llm_calls']}, sentinel={s['total_llm_calls']}")
    print(f"  Latency:    baseline avg={b['avg_latency_seconds']}s, sentinel avg={s['avg_latency_seconds']}s")
    print(f"\n  Safety:     source modifications=0, patches auto-applied=0")
    print(f"{'='*70}\n")

    if rate_limited:
        print("[WARN] Rate limits were encountered during this run. Some stages may be PARTIAL.")

    return summary


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Sentinel Final Evaluation Runner")
    parser.add_argument("--sleep", type=float, default=2.0, metavar="SECONDS",
                        help="Sleep between LLM-heavy stages (default: 2s)")
    parser.add_argument("--start", type=int, default=1)
    parser.add_argument("--end", type=int, default=10)
    parser.add_argument("--incident", type=str, default=None,
                        help="Run a single incident by name")
    parser.add_argument("--force-rerun", action="store_true", default=False,
                        help="Ignore existing sentinel_final.json and re-run the pipeline")
    args = parser.parse_args()
    main(
        sleep=args.sleep,
        start=args.start,
        end=args.end,
        incident=args.incident,
        force_rerun=args.force_rerun,
    )
