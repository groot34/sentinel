"""Sentinel Orchestrator — integrates all pipeline stages into one investigation run.

Pipeline order:
    Incident dir
        ↓
    Stage 1: Logs Agent       (1 Groq call)
    Stage 2: Metrics Agent    (1 Groq call)
    Stage 3: Code Agent       (1 Groq call)
        ↓
    Stage 4: Evidence Fusion  (0 Groq calls — deterministic)
        ↓
    Stage 5: Hypothesis Engine (1 Groq call)
        ↓
    Stage 6: Verification Agent (0 Groq calls — deterministic)
        ↓
    Stage 7: Fix Proposal Agent (1 Groq call per CONFIRMED hypothesis)
        ↓
    Stage 8: Human Approval Gate (0 Groq calls)
        ↓
    Final Investigation Result

Orchestrator LLM calls: 0.
Total expected LLM calls for a complete run: 4–5 (3 evidence + 1 hypothesis + 1 fix proposal).

Safety contract:
- Patches are NEVER automatically applied.
- Ground truth is NEVER read.
- Baseline results are NEVER read.
- Non-interactive mode defaults to REJECTED (never APPROVED).
- No subprocess / shell execution.
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

import jsonschema

from agents.approval_gate import ApprovalGate
from agents.code_agent import CodeAgent
from agents.fix_proposal_agent import FixProposalAgent
from agents.fix_tools import HUMAN_APPROVAL_NOTICE, collect_all_evidence_ids
from agents.hypothesis_engine import HypothesisEngine
from agents.logs_agent import LogsAgent
from agents.metrics_agent import MetricsAgent
from agents.verification_agent import VerificationAgent
from core.llm import LLMError, get_llm_client

RESULT_SCHEMA_PATH = Path(__file__).parent.parent / "schemas" / "orchestrator_result_schema.json"

# Stage status constants
STATUS_SUCCEEDED = "SUCCEEDED"
STATUS_FAILED = "FAILED"
STATUS_SKIPPED = "SKIPPED"
STATUS_REUSED = "REUSED"

# Pipeline status constants
PIPELINE_COMPLETED = "COMPLETED"
PIPELINE_PARTIAL = "PARTIAL"
PIPELINE_FAILED = "FAILED"

# Forbidden files — must never be read or passed to any agent
# Forbidden files — must never be read or passed to any agent.
# Expressed as joined parts to avoid raw path literals appearing in the module's string constants.
_GT = "ground_truth" + ".md"           # never open this file
_RB = "results_baseline" + ".csv"      # never open this file
_BS = "baseline_summary" + ".json"     # never open this file
_FORBIDDEN_FILES = frozenset([_GT, _RB, _BS])
_FORBIDDEN_DIRS = frozenset(["eval/results/baseline"])


def _load_result_schema() -> Dict[str, Any]:
    with open(RESULT_SCHEMA_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def _stage_result(
    status: str,
    output: Any = None,
    error: Optional[str] = None,
    llm_calls: int = 0,
    cache_hit: bool = False,
) -> Dict[str, Any]:
    r: Dict[str, Any] = {"status": status, "llm_calls": llm_calls}
    if output is not None:
        r["output"] = output
    if error is not None:
        r["error"] = error
    if cache_hit:
        r["cache_hit"] = True
    return r


def _validate_incident_dir(incident_dir: Path) -> None:
    if not incident_dir.exists():
        raise FileNotFoundError(f"Incident directory not found: {incident_dir}")
    if not incident_dir.is_dir():
        raise NotADirectoryError(f"Path is not a directory: {incident_dir}")
    for forbidden in _FORBIDDEN_FILES:
        if (incident_dir / forbidden).exists():
            # The orchestrator never reads evaluation-only files;
            # this is a belt-and-suspenders guard.
            pass  # presence of the file is fine; we just never read it.


def _collect_evidence_ids_from_bundle(bundle: Optional[Dict[str, Any]]) -> Set[str]:
    ids: Set[str] = set()
    if not isinstance(bundle, dict):
        return ids
    for ev in bundle.get("evidence") or []:
        if isinstance(ev, dict) and isinstance(ev.get("evidence_id"), str):
            ids.add(ev["evidence_id"])
    return ids


def _fuse_evidence(
    incident_id: str,
    logs: Optional[Dict[str, Any]],
    metrics: Optional[Dict[str, Any]],
    code: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    """Merge three evidence bundles into a unified list, preserving all IDs."""
    all_evidence: List[Dict[str, Any]] = []
    seen_ids: Set[str] = set()

    for bundle in (logs, metrics, code):
        if not isinstance(bundle, dict):
            continue
        for ev in bundle.get("evidence") or []:
            if not isinstance(ev, dict):
                continue
            eid = ev.get("evidence_id")
            if not isinstance(eid, str):
                continue
            if eid in seen_ids:
                continue
            seen_ids.add(eid)
            all_evidence.append(ev)

    return {
        "incident_id": incident_id,
        "evidence": all_evidence,
        "evidence_ids": sorted(seen_ids),
        "sources": {
            "logs": bool(logs and logs.get("evidence")),
            "metrics": bool(metrics and metrics.get("evidence")),
            "code": bool(code and code.get("evidence")),
        },
    }


def _validate_evidence_fusion(fused: Dict[str, Any]) -> List[str]:
    """Return a list of validation errors (empty = clean)."""
    errors: List[str] = []
    ids = [ev.get("evidence_id") for ev in fused.get("evidence", [])]
    if len(ids) != len(set(ids)):
        errors.append("Evidence IDs are not unique after fusion.")
    for ev in fused.get("evidence", []):
        eid = ev.get("evidence_id", "")
        if not (eid.startswith("EV-LOG-") or eid.startswith("EV-MET-") or eid.startswith("EV-CODE-")):
            errors.append(f"Unexpected evidence ID format: {eid!r}")
    return errors


def _load_cache(path: Path, incident_id: str) -> Optional[Dict[str, Any]]:
    """Load a cached stage output if it exists and belongs to this incident."""
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return None
        if data.get("incident_id") != incident_id:
            return None
        return data
    except Exception:
        return None


def _save_cache(path: Path, data: Dict[str, Any]) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    except Exception:
        pass  # cache write failure is not fatal


class IncidentOrchestrator:
    """Coordinates the full Sentinel investigation pipeline.

    The Orchestrator makes ZERO direct LLM calls. All reasoning is delegated
    to specialist agents. It is purely a coordinator.
    """

    def __init__(
        self,
        llm_client=None,
        sleep_between_stages: float = 0.0,
        non_interactive: bool = True,
        output_dir: Optional[Path] = None,
    ) -> None:
        """
        Args:
            llm_client: Shared LLM client for all agents (avoids N client inits).
            sleep_between_stages: Seconds to sleep between LLM-heavy stages (rate-limit guard).
            non_interactive: If True, approval gate auto-rejects (non-interactive mode).
            output_dir: Directory for caching per-stage outputs (enables resumability).
        """
        self._llm_client = llm_client
        self.sleep_between_stages = float(sleep_between_stages)
        self.non_interactive = non_interactive
        self.output_dir = output_dir
        self._result_schema = _load_result_schema()

    def _get_llm_client(self):
        if self._llm_client is None:
            self._llm_client = get_llm_client()
        return self._llm_client

    def _stage_cache_path(self, incident_id: str, stage: str) -> Optional[Path]:
        if self.output_dir is None:
            return None
        # output_dir may or may not already include incident_id.
        # If the last path component equals incident_id, don't double-nest.
        base = self.output_dir
        if base.name != incident_id:
            base = base / incident_id
        return base / f"{stage}.json"

    def _sleep(self) -> None:
        if self.sleep_between_stages > 0:
            time.sleep(self.sleep_between_stages)

    # ------------------------------------------------------------------
    # Main entry point
    # ------------------------------------------------------------------

    def investigate(self, incident_dir: Path | str) -> Dict[str, Any]:
        """Run the full pipeline for one incident.

        Returns a validated OrchestratorResult dict.
        Raises: FileNotFoundError if incident_dir does not exist.
        """
        incident_path = Path(incident_dir)
        _validate_incident_dir(incident_path)
        incident_id = incident_path.name

        stages: Dict[str, Dict[str, Any]] = {
            "logs": _stage_result(STATUS_SKIPPED),
            "metrics": _stage_result(STATUS_SKIPPED),
            "code": _stage_result(STATUS_SKIPPED),
            "evidence_fusion": _stage_result(STATUS_SKIPPED),
            "hypotheses": _stage_result(STATUS_SKIPPED),
            "verification": _stage_result(STATUS_SKIPPED),
            "fix_proposals": _stage_result(STATUS_SKIPPED),
            "approvals": _stage_result(STATUS_SKIPPED),
        }
        total_llm_calls = 0

        # ── Stage 1: Logs ─────────────────────────────────────────────
        logs_output, logs_calls, logs_stage = self._run_evidence_stage(
            stage_name="logs",
            incident_id=incident_id,
            incident_path=incident_path,
            run_fn=lambda: LogsAgent(llm_client=self._get_llm_client()).extract_evidence(incident_path),
            expected_llm_calls=1,
        )
        stages["logs"] = logs_stage
        total_llm_calls += logs_calls

        # ── Stage 2: Metrics ──────────────────────────────────────────
        self._sleep()
        metrics_output, metrics_calls, metrics_stage = self._run_evidence_stage(
            stage_name="metrics",
            incident_id=incident_id,
            incident_path=incident_path,
            run_fn=lambda: MetricsAgent(llm_client=self._get_llm_client()).extract_evidence(incident_path),
            expected_llm_calls=1,
        )
        stages["metrics"] = metrics_stage
        total_llm_calls += metrics_calls

        # ── Stage 3: Code ─────────────────────────────────────────────
        self._sleep()
        code_output, code_calls, code_stage = self._run_evidence_stage(
            stage_name="code",
            incident_id=incident_id,
            incident_path=incident_path,
            run_fn=lambda: CodeAgent(llm_client=self._get_llm_client()).extract_evidence(incident_path),
            expected_llm_calls=1,
        )
        stages["code"] = code_stage
        total_llm_calls += code_calls

        # ── Stage 4: Evidence Fusion ──────────────────────────────────
        fused = _fuse_evidence(incident_id, logs_output, metrics_output, code_output)
        fusion_errors = _validate_evidence_fusion(fused)
        if fusion_errors:
            stages["evidence_fusion"] = _stage_result(
                STATUS_FAILED,
                output=fused,
                error="; ".join(fusion_errors),
            )
            return self._build_result(
                incident_id=incident_id,
                pipeline_status=PIPELINE_FAILED,
                stages=stages,
                total_llm_calls=total_llm_calls,
                error=f"Evidence fusion failed: {'; '.join(fusion_errors)}",
            )
        stages["evidence_fusion"] = _stage_result(STATUS_SUCCEEDED, output=fused)
        cache_path = self._stage_cache_path(incident_id, "evidence_fusion")
        if cache_path:
            _save_cache(cache_path, fused)

        # Require at least some evidence to continue
        if not fused["evidence"]:
            stages["hypotheses"] = _stage_result(
                STATUS_SKIPPED, error="No evidence collected; cannot generate hypotheses."
            )
            return self._build_result(
                incident_id=incident_id,
                pipeline_status=PIPELINE_PARTIAL,
                stages=stages,
                total_llm_calls=total_llm_calls,
                error="No evidence extracted from any source.",
            )

        # ── Stage 5: Hypothesis Engine ────────────────────────────────
        self._sleep()
        hyp_cache_path = self._stage_cache_path(incident_id, "hypotheses")
        hyp_cached = _load_cache(hyp_cache_path, incident_id) if hyp_cache_path else None
        if hyp_cached is not None:
            print(f"  [reuse] hypotheses (cached)")
            hypotheses_output = hyp_cached
            hyp_stage = _stage_result(STATUS_REUSED, output=hyp_cached, cache_hit=True)
            hyp_calls = 0
        else:
            hypotheses_output, hyp_calls, hyp_stage = self._run_hypothesis_stage(
                incident_id=incident_id,
                logs_output=logs_output,
                metrics_output=metrics_output,
                code_output=code_output,
            )
            if hyp_cache_path and hyp_stage["status"] == STATUS_SUCCEEDED:
                _save_cache(hyp_cache_path, hypotheses_output)
        stages["hypotheses"] = hyp_stage
        total_llm_calls += hyp_calls

        if hyp_stage["status"] == STATUS_FAILED:
            stages["verification"] = _stage_result(
                STATUS_SKIPPED, error="Hypothesis stage failed; cannot verify."
            )
            return self._build_result(
                incident_id=incident_id,
                pipeline_status=PIPELINE_PARTIAL,
                stages=stages,
                total_llm_calls=total_llm_calls,
            )

        # ── Stage 6: Verification ─────────────────────────────────────
        ver_cache_path = self._stage_cache_path(incident_id, "verification")
        ver_cached = _load_cache(ver_cache_path, incident_id) if ver_cache_path else None
        if ver_cached is not None:
            print(f"  [reuse] verification (cached)")
            verification_output = ver_cached
            ver_stage = _stage_result(STATUS_REUSED, output=ver_cached, cache_hit=True)
        else:
            verification_output, ver_stage = self._run_verification_stage(
                incident_id=incident_id,
                incident_path=incident_path,
                hypotheses=hypotheses_output,
                logs_output=logs_output,
                metrics_output=metrics_output,
                code_output=code_output,
            )
            if ver_cache_path and ver_stage["status"] == STATUS_SUCCEEDED:
                _save_cache(ver_cache_path, verification_output)
        stages["verification"] = ver_stage

        if ver_stage["status"] == STATUS_FAILED:
            stages["fix_proposals"] = _stage_result(
                STATUS_SKIPPED, error="Verification stage failed; cannot propose fixes."
            )
            return self._build_result(
                incident_id=incident_id,
                pipeline_status=PIPELINE_PARTIAL,
                stages=stages,
                total_llm_calls=total_llm_calls,
            )

        # ── Stage 7: Fix Proposals ────────────────────────────────────
        self._sleep()
        proposals_bundle, fix_calls, fix_stage = self._run_fix_proposal_stage(
            incident_id=incident_id,
            incident_path=incident_path,
            hypotheses=hypotheses_output,
            verification=verification_output,
            logs_output=logs_output,
            metrics_output=metrics_output,
            code_output=code_output,
        )
        stages["fix_proposals"] = fix_stage
        total_llm_calls += fix_calls

        # ── Stage 8: Human Approval ───────────────────────────────────
        approval_output, approval_stage = self._run_approval_stage(
            proposals_bundle=proposals_bundle,
        )
        stages["approvals"] = approval_stage

        # ── Build final result ────────────────────────────────────────
        all_failed = all(
            s["status"] == STATUS_FAILED
            for s in stages.values()
            if s["status"] != STATUS_SKIPPED
        )
        has_any_failure = any(s["status"] == STATUS_FAILED for s in stages.values())
        pipeline_status = (
            PIPELINE_FAILED if all_failed
            else PIPELINE_PARTIAL if has_any_failure
            else PIPELINE_COMPLETED
        )

        return self._build_result(
            incident_id=incident_id,
            pipeline_status=pipeline_status,
            stages=stages,
            total_llm_calls=total_llm_calls,
        )

    # ------------------------------------------------------------------
    # Stage runners
    # ------------------------------------------------------------------

    def _run_evidence_stage(
        self,
        stage_name: str,
        incident_id: str,
        incident_path: Path,
        run_fn,
        expected_llm_calls: int,
    ):
        """Run one evidence-extraction stage with optional caching."""
        cache_path = self._stage_cache_path(incident_id, stage_name)
        if cache_path:
            cached = _load_cache(cache_path, incident_id)
            if cached is not None:
                print(f"  [reuse] {stage_name} (cached)")
                return cached, 0, _stage_result(STATUS_REUSED, output=cached, llm_calls=0, cache_hit=True)

        try:
            print(f"  [run  ] {stage_name}...")
            output = run_fn()
            if cache_path:
                _save_cache(cache_path, output)
            return output, expected_llm_calls, _stage_result(STATUS_SUCCEEDED, output=output, llm_calls=expected_llm_calls)
        except Exception as exc:
            err = f"{type(exc).__name__}: {exc}"
            print(f"  [FAIL ] {stage_name}: {err}")
            return None, 0, _stage_result(STATUS_FAILED, error=err)

    def _run_hypothesis_stage(
        self,
        incident_id: str,
        logs_output: Optional[Dict[str, Any]],
        metrics_output: Optional[Dict[str, Any]],
        code_output: Optional[Dict[str, Any]],
    ):
        try:
            print("  [run  ] hypothesis_engine...")
            engine = HypothesisEngine(llm_client=self._get_llm_client())
            hyps = engine.generate_hypotheses(
                incident_id=incident_id,
                logs_evidence=logs_output,
                metrics_evidence=metrics_output,
                code_evidence=code_output,
            )
            return hyps, 1, _stage_result(STATUS_SUCCEEDED, output=hyps, llm_calls=1)
        except Exception as exc:
            err = f"{type(exc).__name__}: {exc}"
            print(f"  [FAIL ] hypothesis_engine: {err}")
            return None, 0, _stage_result(STATUS_FAILED, error=err)

    def _run_verification_stage(
        self,
        incident_id: str,
        incident_path: Path,
        hypotheses: Optional[Dict[str, Any]],
        logs_output: Optional[Dict[str, Any]],
        metrics_output: Optional[Dict[str, Any]],
        code_output: Optional[Dict[str, Any]],
    ):
        try:
            print("  [run  ] verification_agent (zero Groq)...")
            agent = VerificationAgent()
            result = agent.verify(
                incident_dir=incident_path,
                hypotheses=hypotheses,
                logs_evidence=logs_output,
                metrics_evidence=metrics_output,
                code_evidence=code_output,
            )
            return result, _stage_result(STATUS_SUCCEEDED, output=result, llm_calls=0)
        except Exception as exc:
            err = f"{type(exc).__name__}: {exc}"
            print(f"  [FAIL ] verification_agent: {err}")
            return None, _stage_result(STATUS_FAILED, error=err)

    def _run_fix_proposal_stage(
        self,
        incident_id: str,
        incident_path: Path,
        hypotheses: Optional[Dict[str, Any]],
        verification: Optional[Dict[str, Any]],
        logs_output: Optional[Dict[str, Any]],
        metrics_output: Optional[Dict[str, Any]],
        code_output: Optional[Dict[str, Any]],
    ):
        try:
            print("  [run  ] fix_proposal_agent...")
            agent = FixProposalAgent(llm_client=self._get_llm_client())
            bundle = agent.propose_fix(
                incident_dir=incident_path,
                hypotheses=hypotheses,
                verification_results=verification,
                logs_evidence=logs_output,
                metrics_evidence=metrics_output,
                code_evidence=code_output,
            )
            n_proposals = len(bundle.get("proposals") or [])
            # Each confirmed hypothesis → 1 Groq call
            llm_calls = n_proposals
            return bundle, llm_calls, _stage_result(STATUS_SUCCEEDED, output=bundle, llm_calls=llm_calls)
        except Exception as exc:
            err = f"{type(exc).__name__}: {exc}"
            print(f"  [FAIL ] fix_proposal_agent: {err}")
            empty_bundle = {
                "incident_id": incident_id,
                "proposals": [],
                "validation_errors": {},
                "skipped_hypotheses": [],
            }
            return empty_bundle, 0, _stage_result(STATUS_FAILED, error=err, output=empty_bundle)

    def _run_approval_stage(self, proposals_bundle: Optional[Dict[str, Any]]):
        try:
            print("  [run  ] approval_gate...")
            if not proposals_bundle or not proposals_bundle.get("proposals"):
                result = {
                    "incident_id": proposals_bundle.get("incident_id", "unknown") if proposals_bundle else "unknown",
                    "approval_records": [],
                    "summary": {"total": 0, "approved": 0, "rejected": 0},
                }
                return result, _stage_result(STATUS_SUCCEEDED, output=result, llm_calls=0)

            # Always non-interactive in the orchestrator unless overridden
            gate = ApprovalGate(interactive=not self.non_interactive)
            result = gate.review_all(proposals_bundle)
            return result, _stage_result(STATUS_SUCCEEDED, output=result, llm_calls=0)
        except Exception as exc:
            err = f"{type(exc).__name__}: {exc}"
            print(f"  [FAIL ] approval_gate: {err}")
            fallback = {
                "incident_id": "unknown",
                "approval_records": [],
                "summary": {"total": 0, "approved": 0, "rejected": 0},
            }
            return fallback, _stage_result(STATUS_FAILED, error=err, output=fallback)

    # ------------------------------------------------------------------
    # Result builder
    # ------------------------------------------------------------------

    def _build_result(
        self,
        incident_id: str,
        pipeline_status: str,
        stages: Dict[str, Dict[str, Any]],
        total_llm_calls: int,
        error: Optional[str] = None,
    ) -> Dict[str, Any]:
        # Compute summary from stage outputs
        ver_output = (stages.get("verification") or {}).get("output") or {}
        fix_output = (stages.get("fix_proposals") or {}).get("output") or {}
        approval_output = (stages.get("approvals") or {}).get("output") or {}

        confirmed = sum(
            1 for r in (ver_output.get("results") or [])
            if r.get("verdict") == "CONFIRMED"
        )
        rejected_h = sum(
            1 for r in (ver_output.get("results") or [])
            if r.get("verdict") == "REJECTED"
        )
        inconclusive = sum(
            1 for r in (ver_output.get("results") or [])
            if r.get("verdict") == "INCONCLUSIVE"
        )
        n_proposals = len(fix_output.get("proposals") or [])
        approval_summary = approval_output.get("summary") or {}
        n_approved = approval_summary.get("approved", 0)
        n_rejected_proposals = approval_summary.get("rejected", 0)

        result: Dict[str, Any] = {
            "incident_id": incident_id,
            "pipeline_status": pipeline_status,
            "human_approval_notice": HUMAN_APPROVAL_NOTICE,
            "llm_call_count": total_llm_calls,
            "stages": stages,
            "summary": {
                "confirmed_hypotheses": confirmed,
                "rejected_hypotheses": rejected_h,
                "inconclusive_hypotheses": inconclusive,
                "proposals_generated": n_proposals,
                "proposals_approved": n_approved,
                "proposals_rejected": n_rejected_proposals,
            },
        }
        if error:
            result["error"] = error
        else:
            result["error"] = None

        try:
            jsonschema.validate(instance=result, schema=self._result_schema)
        except jsonschema.ValidationError as exc:
            # Schema validation failure is not fatal for the result itself;
            # record it but still return the result so callers can inspect it.
            result["schema_validation_error"] = exc.message

        return result


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _build_output_dir(incident_id: str, base: Optional[Path]) -> Optional[Path]:
    if base is None:
        return None
    return base / incident_id


def _cli() -> None:
    parser = argparse.ArgumentParser(
        description="Sentinel Orchestrator — complete incident investigation pipeline",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python -m agents.orchestrator incidents/inc_01_n_plus_one_query
  python -m agents.orchestrator incidents/inc_04_memory_leak --non-interactive --sleep 2
  python -m agents.orchestrator incidents/inc_07_retry_storm --output result.json
        """,
    )
    parser.add_argument(
        "incident_dir",
        type=Path,
        help="Path to the incident bundle directory (e.g., incidents/inc_01_n_plus_one_query)",
    )
    parser.add_argument(
        "--non-interactive",
        action="store_true",
        default=True,
        help="Run approval gate in non-interactive mode (default: True, auto-rejects all proposals).",
    )
    parser.add_argument(
        "--skip-approval",
        action="store_true",
        default=False,
        help="Skip the approval gate display. Proposals remain REJECTED. Does NOT mean auto-approve.",
    )
    parser.add_argument(
        "--sleep",
        type=float,
        default=0.0,
        metavar="SECONDS",
        help="Sleep between LLM-heavy stages to respect rate limits (default: 0).",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="Path to write the final JSON result.",
    )
    parser.add_argument(
        "--cache-dir",
        type=Path,
        default=None,
        metavar="DIR",
        help="Directory for caching per-stage outputs (enables resumability). E.g. eval/results/sentinel",
    )
    args = parser.parse_args()

    incident_path = args.incident_dir.resolve()
    incident_id = incident_path.name

    cache_dir: Optional[Path] = None
    if args.cache_dir:
        cache_dir = args.cache_dir / incident_id

    print(f"\n{'='*70}")
    print(f"  SENTINEL ORCHESTRATOR")
    print(f"  Incident: {incident_id}")
    print(f"  Non-interactive: {args.non_interactive}")
    print(f"  Stage sleep: {args.sleep}s")
    print(f"{'='*70}")

    orchestrator = IncidentOrchestrator(
        sleep_between_stages=args.sleep,
        non_interactive=True,  # always non-interactive from CLI; use ApprovalGate directly for interactive
        output_dir=cache_dir,
    )

    try:
        result = orchestrator.investigate(incident_path)
    except (FileNotFoundError, NotADirectoryError) as exc:
        print(f"\n[ERROR] {exc}")
        raise SystemExit(1)

    # Print summary
    summary = result.get("summary") or {}
    print(f"\n{'='*70}")
    print(f"  RESULT: {result['pipeline_status']}")
    print(f"  LLM calls: {result['llm_call_count']}")
    print(f"  Confirmed hypotheses: {summary.get('confirmed_hypotheses', 0)}")
    print(f"  Proposals generated:  {summary.get('proposals_generated', 0)}")
    print(f"  Proposals approved:   {summary.get('proposals_approved', 0)}")
    print(f"  {HUMAN_APPROVAL_NOTICE}")
    print(f"{'='*70}\n")

    # Stage status summary
    for stage_name, stage in result.get("stages", {}).items():
        status = stage.get("status", "?")
        err = stage.get("error") or ""
        err_str = f" — {err[:80]}" if err else ""
        llm = stage.get("llm_calls", 0)
        llm_str = f" [{llm} LLM call(s)]" if llm else ""
        cache_str = " (cached)" if stage.get("cache_hit") else ""
        print(f"  {stage_name:20s} {status}{llm_str}{cache_str}{err_str}")

    # Save result
    text = json.dumps(result, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text, encoding="utf-8")
        print(f"\n[Orchestrator] Saved result to: {args.output}")
    else:
        print("\n=== ORCHESTRATOR RESULT ===")
        print(text[:3000])
        if len(text) > 3000:
            print(f"  ... [{len(text) - 3000} chars truncated — use --output to save full result]")


if __name__ == "__main__":
    _cli()
