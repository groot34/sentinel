"""Sentinel Baseline Evaluation Runner.

Runs the single-shot baseline investigator across synthetic incident bundles,
evaluates diagnoses deterministically, saves per-incident results, and produces
a summary JSON file.

Ground truth isolation guarantee:
  1. Baseline runs on incident evidence ONLY (ground_truth.md excluded at runtime).
  2. Evaluator reads ground_truth.md ONLY after baseline output has been produced.
  3. ground_truth content is NEVER passed into the baseline LLM prompt.

Usage:
  # Full evaluation across all 10 incidents
  python -m eval.run_eval --mode baseline

  # Single incident
  python -m eval.run_eval --mode baseline --incident inc_01_n_plus_one_query

  # Partial range with sleep between calls (rate-limit friendly)
  python -m eval.run_eval --mode baseline --start 1 --end 5 --sleep 5
"""

import argparse
import csv
import json
import time
import traceback
from pathlib import Path
from typing import Any, Dict, List, Optional

from baseline.baseline_agent import BaselineAgent
from core.llm import LLMError, LLMRateLimitError, get_llm_client
from eval.evaluator import CorrectnessEvaluator

# -------------------------------------------------------------------------
# Paths
# -------------------------------------------------------------------------
REPO_ROOT = Path(__file__).parent.parent
INCIDENTS_DIR = REPO_ROOT / "incidents"
RESULTS_DIR = REPO_ROOT / "eval" / "results" / "baseline"
RESULTS_CSV = REPO_ROOT / "eval" / "results_baseline.csv"
SUMMARY_JSON = REPO_ROOT / "eval" / "baseline_summary.json"

CSV_FIELDNAMES = [
    "incident_id",
    "baseline_status",
    "baseline_root_cause",
    "ground_truth_root_cause",
    "correctness",
    "correctness_explanation",
    "evidence_count",
    "latency_seconds",
    "input_tokens",
    "output_tokens",
    "model",
    "error",
]


def discover_incidents(incidents_dir: Path, start: int = 1, end: int = 10) -> List[Path]:
    """Return sorted incident directories filtered to the requested range."""
    all_incidents = sorted(
        [d for d in incidents_dir.iterdir() if d.is_dir() and d.name.startswith("inc_")]
    )
    # Extract sequence number from folder name (inc_01_... -> 1)
    filtered = []
    for inc in all_incidents:
        try:
            seq = int(inc.name.split("_")[1])
        except (IndexError, ValueError):
            seq = 0
        if start <= seq <= end:
            filtered.append(inc)
    return filtered


def load_existing_results(results_dir: Path) -> Dict[str, bool]:
    """Return a set of incident IDs that already have a complete result file."""
    completed: Dict[str, bool] = {}
    if not results_dir.exists():
        return completed
    for f in results_dir.glob("*.json"):
        # A result file is complete if it contains a baseline_status field
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            if "baseline_status" in data and data["baseline_status"] != "RUNNING":
                completed[f.stem] = True
        except Exception:
            pass
    return completed


def run_single_incident(
    incident_dir: Path,
    agent: BaselineAgent,
    evaluator: CorrectnessEvaluator,
    results_dir: Path,
) -> Dict[str, Any]:
    """Run baseline and evaluate one incident. Returns the result record."""
    incident_id = incident_dir.name
    ground_truth_path = incident_dir / "ground_truth.md"

    result: Dict[str, Any] = {
        "incident_id": incident_id,
        "baseline_status": "RUNNING",
        "baseline_root_cause": "",
        "ground_truth_root_cause": "",
        "correctness": "",
        "correctness_explanation": "",
        "evidence_count": 0,
        "latency_seconds": 0.0,
        "input_tokens": None,
        "output_tokens": None,
        "model": "",
        "error": "",
    }

    # ── Step 1: Run baseline (ground_truth.md is excluded inside the agent) ──
    t_start = time.monotonic()
    try:
        diagnosis = agent.diagnose(incident_dir)
        latency = time.monotonic() - t_start

        result["baseline_status"] = "SUCCESS"
        result["baseline_root_cause"] = diagnosis.get("root_cause_guess", "")
        result["evidence_count"] = len(diagnosis.get("evidence", []))
        result["latency_seconds"] = round(latency, 3)

        # Token telemetry lives on the LLM client's last response (best effort)
        # We patch via the agent's client if available
        client = agent.llm_client
        if hasattr(client, "_last_response") and client._last_response:
            lr = client._last_response
            result["input_tokens"] = lr.prompt_tokens
            result["output_tokens"] = lr.completion_tokens
            result["model"] = lr.model or client.model
        else:
            result["model"] = getattr(client, "model", "unknown")

    except LLMRateLimitError as e:
        result["baseline_status"] = "RATE_LIMITED"
        result["error"] = str(e)[:300]
        print(f"  [!] Rate limited on {incident_id}: {e}")
        return result
    except LLMError as e:
        result["baseline_status"] = "LLM_ERROR"
        result["error"] = str(e)[:300]
        print(f"  [X] LLM error on {incident_id}: {e}")
        return result
    except Exception as e:
        result["baseline_status"] = "ERROR"
        result["error"] = traceback.format_exc()[:500]
        print(f"  [X] Unexpected error on {incident_id}: {e}")
        return result

    # ── Step 2: Read ground truth (ONLY after baseline has produced its answer) ──
    gt_root_cause = evaluator.extract_ground_truth_root_cause(ground_truth_path)
    result["ground_truth_root_cause"] = gt_root_cause

    # ── Step 3: Evaluate correctness deterministically ──
    correctness, explanation = evaluator.evaluate_diagnosis(
        incident_id=incident_id,
        diagnosis_text=result["baseline_root_cause"],
        reasoning_text=diagnosis.get("reasoning", ""),
    )
    result["correctness"] = correctness
    result["correctness_explanation"] = explanation

    # ── Step 4: Save raw baseline output (no ground_truth inside) ──
    results_dir.mkdir(parents=True, exist_ok=True)
    raw_output = {
        "incident_id": incident_id,
        "baseline_root_cause": result["baseline_root_cause"],
        "reasoning": diagnosis.get("reasoning", ""),
        "confidence": diagnosis.get("confidence"),
        "evidence": diagnosis.get("evidence", []),
        "suggested_mitigation": diagnosis.get("suggested_mitigation", ""),
        "latency_seconds": result["latency_seconds"],
        "input_tokens": result["input_tokens"],
        "output_tokens": result["output_tokens"],
        "model": result["model"],
    }
    (results_dir / f"{incident_id}.json").write_text(
        json.dumps(raw_output, indent=2), encoding="utf-8"
    )

    return result


def write_csv(records: List[Dict[str, Any]], csv_path: Path) -> None:
    """Write evaluation records to CSV, merging with any existing completed rows."""
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_FIELDNAMES, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(records)


def write_summary(records: List[Dict[str, Any]], summary_path: Path, model: str) -> None:
    """Compute and write baseline_summary.json."""
    total = len(records)
    correct = sum(1 for r in records if r.get("correctness") == "CORRECT")
    incorrect = sum(1 for r in records if r.get("correctness") == "INCORRECT")
    review = sum(1 for r in records if r.get("correctness") == "REVIEW")
    failures = sum(1 for r in records if r.get("baseline_status") not in ("SUCCESS",))

    evaluated = correct + incorrect + review
    accuracy = round(correct / evaluated, 4) if evaluated > 0 else None

    latencies = [r["latency_seconds"] for r in records if r.get("latency_seconds", 0) > 0]
    avg_latency = round(sum(latencies) / len(latencies), 3) if latencies else None

    input_tokens = [r["input_tokens"] for r in records if r.get("input_tokens") is not None]
    output_tokens = [r["output_tokens"] for r in records if r.get("output_tokens") is not None]
    total_input = sum(input_tokens) if input_tokens else None
    total_output = sum(output_tokens) if output_tokens else None

    summary = {
        "model_used": model,
        "total_incidents": total,
        "evaluated": evaluated,
        "correct": correct,
        "incorrect": incorrect,
        "review": review,
        "failures": failures,
        "accuracy": accuracy,
        "accuracy_note": "correct / (correct + incorrect + review)",
        "average_latency_seconds": avg_latency,
        "total_input_tokens": total_input,
        "total_output_tokens": total_output,
        "cost": "unavailable — free Groq tier does not report billing cost",
        "evaluation_method": "deterministic keyword matching against canonical incident criteria",
        "fairness_lock": "Baseline and Sentinel must use the same model for comparison.",
    }
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    summary_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")


def run_baseline_eval(
    incidents_dir: Path,
    specific_incident: Optional[str] = None,
    start: int = 1,
    end: int = 10,
    sleep_seconds: float = 2.0,
    resume: bool = True,
) -> List[Dict[str, Any]]:
    """Run baseline evaluation with resume support and rate-limit awareness."""
    print(f"\n{'='*60}")
    print("SENTINEL BASELINE EVALUATION")
    print(f"{'='*60}")

    # Discover incidents
    if specific_incident:
        inc_path = incidents_dir / specific_incident
        if not inc_path.exists():
            raise FileNotFoundError(f"Incident directory not found: {inc_path}")
        to_run = [inc_path]
    else:
        to_run = discover_incidents(incidents_dir, start=start, end=end)

    print(f"Incidents to evaluate: {len(to_run)}")

    # Check for already-completed results (resume support)
    completed = load_existing_results(RESULTS_DIR) if resume else {}
    if completed:
        print(f"Resuming: {len(completed)} already completed")

    # Create agent & evaluator (shared across incidents)
    from core.llm import LLMConfigurationError
    try:
        agent = BaselineAgent()
    except LLMConfigurationError as e:
        print(f"\n[Configuration Error] {e}")
        print("\nTo run the real evaluation:")
        print("  1. Copy .env.example to .env")
        print("  2. Edit .env and set: GROQ_API_KEY=gsk_your_key_here")
        print("  3. Re-run: python -m eval.run_eval --mode baseline --sleep 3")
        raise
    evaluator = CorrectnessEvaluator()
    model_used = getattr(agent.llm_client, "model", "unknown")

    all_records: List[Dict[str, Any]] = []

    for i, inc_dir in enumerate(to_run):
        inc_id = inc_dir.name
        print(f"\n[{i+1}/{len(to_run)}] {inc_id}")

        if inc_id in completed:
            # Load the already-completed raw result
            try:
                raw = json.loads((RESULTS_DIR / f"{inc_id}.json").read_text(encoding="utf-8"))
                # Rebuild eval row from raw + ground truth
                gt = evaluator.extract_ground_truth_root_cause(inc_dir / "ground_truth.md")
                correctness, explanation = evaluator.evaluate_diagnosis(
                    inc_id, raw.get("baseline_root_cause", ""), raw.get("reasoning", "")
                )
                record = {
                    "incident_id": inc_id,
                    "baseline_status": "SUCCESS",
                    "baseline_root_cause": raw.get("baseline_root_cause", ""),
                    "ground_truth_root_cause": gt,
                    "correctness": correctness,
                    "correctness_explanation": explanation,
                    "evidence_count": len(raw.get("evidence", [])),
                    "latency_seconds": raw.get("latency_seconds", 0),
                    "input_tokens": raw.get("input_tokens"),
                    "output_tokens": raw.get("output_tokens"),
                    "model": raw.get("model", model_used),
                    "error": "",
                }
                all_records.append(record)
                print(f"  [RESUMED] from cached result: {correctness}")
                continue
            except Exception:
                pass  # Fall through to re-run

        record = run_single_incident(inc_dir, agent, evaluator, RESULTS_DIR)
        all_records.append(record)

        status_icon = {"SUCCESS": "[OK]", "RATE_LIMITED": "[WARN]", "LLM_ERROR": "[FAIL]", "ERROR": "[FAIL]"}.get(
            record["baseline_status"], "[?]"
        )
        print(
            f"  {status_icon} {record['baseline_status']} | "
            f"Correctness: {record.get('correctness', 'N/A')} | "
            f"Latency: {record.get('latency_seconds', 0):.1f}s"
        )

        # Persist CSV after each incident (so partial runs are saved)
        write_csv(all_records, RESULTS_CSV)

        # Sleep between calls to respect free Groq rate limits
        if i < len(to_run) - 1 and record["baseline_status"] == "SUCCESS":
            print(f"  ... sleeping {sleep_seconds}s")
            time.sleep(sleep_seconds)

    # Final summary
    write_csv(all_records, RESULTS_CSV)
    write_summary(all_records, SUMMARY_JSON, model_used)

    # Print quick summary
    correct = sum(1 for r in all_records if r.get("correctness") == "CORRECT")
    incorrect = sum(1 for r in all_records if r.get("correctness") == "INCORRECT")
    review = sum(1 for r in all_records if r.get("correctness") == "REVIEW")
    failures = sum(1 for r in all_records if r.get("baseline_status") not in ("SUCCESS",))
    evaluated = correct + incorrect + review
    accuracy = f"{correct}/{evaluated} ({100*correct/evaluated:.0f}%)" if evaluated > 0 else "N/A"

    print(f"\n{'='*60}")
    print("BASELINE EVALUATION COMPLETE")
    print(f"  Model      : {model_used}")
    print(f"  Total      : {len(all_records)}")
    print(f"  Correct    : {correct}")
    print(f"  Incorrect  : {incorrect}")
    print(f"  Review     : {review}")
    print(f"  Failures   : {failures}")
    print(f"  Accuracy   : {accuracy}")
    print(f"  Results    : {RESULTS_CSV}")
    print(f"  Summary    : {SUMMARY_JSON}")
    print(f"{'='*60}\n")

    return all_records


def main() -> None:
    parser = argparse.ArgumentParser(description="Sentinel Evaluation Benchmark Runner")
    parser.add_argument(
        "--mode",
        choices=["baseline", "advanced", "compare"],
        default="baseline",
        help="Evaluation execution mode",
    )
    parser.add_argument(
        "--incidents-dir",
        default=str(INCIDENTS_DIR),
        help="Path to synthetic incident bundles directory",
    )
    parser.add_argument(
        "--incident",
        default=None,
        help="Run evaluation on a single incident ID (e.g. inc_01_n_plus_one_query)",
    )
    parser.add_argument(
        "--start",
        type=int,
        default=1,
        help="Starting incident sequence number (inclusive, default: 1)",
    )
    parser.add_argument(
        "--end",
        type=int,
        default=10,
        help="Ending incident sequence number (inclusive, default: 10)",
    )
    parser.add_argument(
        "--sleep",
        type=float,
        default=2.0,
        help="Seconds to sleep between incident calls (rate-limit protection, default: 2)",
    )
    parser.add_argument(
        "--no-resume",
        action="store_true",
        help="Disable resume — re-run all incidents even if results already exist",
    )
    args = parser.parse_args()

    inc_dir = Path(args.incidents_dir)

    if args.mode == "baseline":
        run_baseline_eval(
            incidents_dir=inc_dir,
            specific_incident=args.incident,
            start=args.start,
            end=args.end,
            sleep_seconds=args.sleep,
            resume=not args.no_resume,
        )
    elif args.mode == "advanced":
        print("[Evaluation] Advanced Sentinel evaluation not yet implemented.")
    else:
        print("[Evaluation] Comparative evaluation not yet implemented.")


if __name__ == "__main__":
    main()
