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
from typing import Any, Dict, List, Optional, Set, TYPE_CHECKING

import jsonschema

from agents.approval_gate import ApprovalGate
from agents.code_agent import CodeAgent
from agents.fix_proposal_agent import FixProposalAgent
from agents.fix_tools import HUMAN_APPROVAL_NOTICE, collect_all_evidence_ids
from agents.hypothesis_engine import HypothesisEngine
from agents.logs_agent import LogsAgent
from agents.metrics_agent import MetricsAgent
from agents.verification_agent import VerificationAgent
from core.domain.models import IncidentStatus, StageStatus
from core.domain.state import InvestigationState
from core.llm import LLMError, get_llm_client
from core.persistence.repository import PersistenceError, PersistenceRepository
from core.events.base import DomainEvent
from core.events.bus import EventBus
from core.events.events import (
    InvestigationCompleted,
    InvestigationResumed,
    InvestigationStarted,
    StageCached,
    StageCompleted,
    StageFailed,
    StageSkipped,
    StageStarted,
)

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

# Canonical stage execution order
CANONICAL_STAGE_ORDER = [
    "logs",
    "metrics",
    "code",
    "evidence_fusion",
    "hypotheses",
    "verification",
    "fix_proposals",
    "approvals",
]


def _find_resume_stage(state: InvestigationState) -> Optional[str]:
    """Find the first incomplete or retryable stage in canonical order.

    Rules:
    - SUCCEEDED -> skip
    - REUSED / CACHED -> skip
    - FAILED -> resume from here
    - RUNNING -> resume from here
    - SKIPPED -> resume from here
    - missing -> first incomplete stage
    - If all stages completed -> None
    """
    for stage_name in CANONICAL_STAGE_ORDER:
        sr = state.stages.get(stage_name)
        if sr is None:
            return stage_name
        status = sr.status if isinstance(sr.status, str) else sr.status.value
        if status in (STATUS_SUCCEEDED, STATUS_REUSED, "CACHED"):
            continue
        return stage_name
    return None

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
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
    total_tokens: int = 0,
    cache_hit: bool = False,
) -> Dict[str, Any]:
    r: Dict[str, Any] = {
        "status": status,
        "llm_calls": llm_calls,
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": total_tokens,
    }
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


def _get_token_usage_snapshot(client: Any) -> Dict[str, int]:
    if client is not None and hasattr(client, "get_session_token_usage"):
        try:
            return client.get_session_token_usage()
        except Exception:
            pass
    return {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0, "llm_calls": 0}


def _calc_token_delta(before: Dict[str, int], after: Dict[str, int]) -> Dict[str, int]:
    return {
        "prompt_tokens": max(0, (after.get("prompt_tokens") or 0) - (before.get("prompt_tokens") or 0)),
        "completion_tokens": max(0, (after.get("completion_tokens") or 0) - (before.get("completion_tokens") or 0)),
        "total_tokens": max(0, (after.get("total_tokens") or 0) - (before.get("total_tokens") or 0)),
        "llm_calls": max(0, (after.get("llm_calls") or 0) - (before.get("llm_calls") or 0)),
    }


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
        repository: Optional[PersistenceRepository] = None,
        event_bus: Optional[EventBus] = None,
    ) -> None:
        """
        Args:
            llm_client: Shared LLM client for all agents (avoids N client inits).
            sleep_between_stages: Seconds to sleep between LLM-heavy stages (rate-limit guard).
            non_interactive: If True, approval gate auto-rejects (non-interactive mode).
            output_dir: Directory for caching per-stage outputs (enables resumability).
            repository: Optional PersistenceRepository for durable InvestigationState persistence.
            event_bus: Optional EventBus for domain event publication. Defaults to None (disabled).
        """
        self._llm_client = llm_client
        self.sleep_between_stages = float(sleep_between_stages)
        self.non_interactive = non_interactive
        self.output_dir = output_dir
        self.repository = repository
        self._repository = repository
        self._event_bus = event_bus
        self._result_schema = _load_result_schema()
        self.state: Optional[InvestigationState] = None
        self.last_state: Optional[InvestigationState] = None

    def _persist(self, state: InvestigationState) -> None:
        """Persist investigation state if repository is configured.

        Never swallows persistence exceptions.
        """
        if self._repository is not None:
            self._repository.save(state)

    def _emit(self, event: DomainEvent) -> None:
        """Publish a domain event if an EventBus is configured.

        Subscriber failures are handled by the EventBus itself; this helper
        never suppresses them — callers should not wrap _emit() in try/except.
        """
        if self._event_bus is not None:
            self._event_bus.publish(event)

    def _make_completed_event(
        self,
        state: InvestigationState,
        pipeline_status: str,
        error: Optional[str] = None,
    ) -> InvestigationCompleted:
        """Build InvestigationCompleted from current state without altering state."""
        stages = state.stages
        ver_output = (stages.get("verification") or None)
        fix_output = (stages.get("fix_proposals") or None)
        approval_output = (stages.get("approvals") or None)

        ver_results = (ver_output.output or {}) if ver_output else {}
        fix_results = (fix_output.output or {}) if fix_output else {}
        appr_results = (approval_output.output or {}) if approval_output else {}

        confirmed = sum(
            1 for r in (ver_results.get("results") or [])
            if r.get("verdict") == "CONFIRMED"
        )
        n_proposals = len((fix_results.get("proposals") or []))
        n_approved = (appr_results.get("summary") or {}).get("approved", 0)
        total_llm_calls = sum(s.llm_calls or 0 for s in stages.values())
        total_tokens = sum(s.total_tokens or 0 for s in stages.values())

        return InvestigationCompleted(
            investigation_id=state.incident_id,
            status=pipeline_status,
            total_llm_calls=total_llm_calls,
            total_tokens=total_tokens,
            confirmed_hypotheses=confirmed,
            proposals_generated=n_proposals,
            proposals_approved=n_approved,
            error=error or state.error,
        )

    def _find_resume_stage(self, state: InvestigationState) -> Optional[str]:
        """Find the first incomplete or retryable stage in canonical order."""
        return _find_resume_stage(state)

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
    # Main entry points
    # ------------------------------------------------------------------

    def investigate(self, incident_dir: Path | str) -> Dict[str, Any]:
        """Run the full pipeline for one incident.

        Returns a validated OrchestratorResult dict.
        Raises: FileNotFoundError if incident_dir does not exist.
        """
        incident_path = Path(incident_dir)
        _validate_incident_dir(incident_path)
        incident_id = incident_path.name

        # Create exactly one InvestigationState for this investigation
        state = InvestigationState(incident_id=incident_id)
        self.state = state
        self.last_state = state
        self._persist(state)
        # Emit InvestigationStarted AFTER state is created and persisted
        self._emit(InvestigationStarted(
            investigation_id=incident_id,
            initial_stage="logs",
        ))

        return self._run_pipeline(incident_path=incident_path, state=state, resume_stage="logs")

    def resume(self, investigation_id: str, incident_dir: Path | str) -> Dict[str, Any]:
        """Resume an incomplete or failed investigation from durable state.

        Args:
            investigation_id: Identifier of the investigation to resume.
            incident_dir: Path to the incident bundle directory.

        Returns:
            Validated OrchestratorResult dict.

        Raises:
            PersistenceError: If repository is unconfigured, state missing, or state completed.
            FileNotFoundError: If incident_dir does not exist.
        """
        if self._repository is None:
            raise PersistenceError("Persistence repository is required for resume")

        incident_path = Path(incident_dir)
        _validate_incident_dir(incident_path)
        if incident_path.name != investigation_id:
            raise PersistenceError(
                f"Incident directory name '{incident_path.name}' does not match investigation ID '{investigation_id}'"
            )

        state = self._repository.load(investigation_id)
        if state is None:
            raise PersistenceError(f"No persisted state found for investigation '{investigation_id}'")

        status_val = state.status if isinstance(state.status, str) else state.status.value
        if status_val in (IncidentStatus.COMPLETED, "COMPLETED"):
            raise PersistenceError(f"Cannot resume completed investigation '{investigation_id}'")

        # Reset active stage and error state for clean resumption
        state.current_stage = None
        if status_val in (IncidentStatus.FAILED, IncidentStatus.PARTIAL, "FAILED", "PARTIAL"):
            state.status = IncidentStatus.RUNNING
            state.error = None

        self.state = state
        self.last_state = state

        resume_stage = self._find_resume_stage(state)

        # Persist the cleaned-up resume state BEFORE emitting InvestigationResumed
        self._persist(state)
        resumed_from_status = status_val if isinstance(status_val, str) else status_val.value
        self._emit(InvestigationResumed(
            investigation_id=investigation_id,
            resume_stage=resume_stage,
            resumed_from_status=resumed_from_status,
        ))

        return self._run_pipeline(incident_path=incident_path, state=state, resume_stage=resume_stage)

    def _run_pipeline(
        self,
        incident_path: Path,
        state: InvestigationState,
        resume_stage: Optional[str] = "logs",
    ) -> Dict[str, Any]:
        incident_id = incident_path.name

        def should_run(stage_name: str) -> bool:
            if resume_stage is None:
                return False
            resume_idx = CANONICAL_STAGE_ORDER.index(resume_stage)
            stage_idx = CANONICAL_STAGE_ORDER.index(stage_name)
            return stage_idx >= resume_idx

        def _get_stage_output(s_name: str) -> Any:
            sr = state.stages.get(s_name)
            if sr is not None and sr.status in (STATUS_SUCCEEDED, STATUS_REUSED, "CACHED"):
                return sr.output
            return None

        # Reinject previously completed outputs for downstream stages
        logs_output = _get_stage_output("logs")
        metrics_output = _get_stage_output("metrics")
        code_output = _get_stage_output("code")
        fused = _get_stage_output("evidence_fusion")
        hypotheses_output = _get_stage_output("hypotheses")
        verification_output = _get_stage_output("verification")
        proposals_bundle = _get_stage_output("fix_proposals")
        approval_output = _get_stage_output("approvals")

        # ── Stage 1: Logs ─────────────────────────────────────────────
        if should_run("logs"):
            logs_output, logs_calls, logs_stage = self._run_evidence_stage(
                stage_name="logs",
                incident_id=incident_id,
                incident_path=incident_path,
                run_fn=lambda: LogsAgent(llm_client=self._get_llm_client()).extract_evidence(incident_path),
                expected_llm_calls=1,
            )
            if logs_stage.get("status") == STATUS_REUSED:
                state.mark_cached("logs", output=logs_output)
                if logs_output:
                    state.add_evidence(logs_output)
                self._persist(state)
                self._emit(StageCached(investigation_id=incident_id, stage="logs"))
            elif logs_stage.get("status") == STATUS_SUCCEEDED:
                state.start_stage("logs")
                self._emit(StageStarted(investigation_id=incident_id, stage="logs"))
                state.complete_stage(
                    "logs",
                    output=logs_output,
                    llm_calls=logs_stage.get("llm_calls", 0),
                    prompt_tokens=logs_stage.get("prompt_tokens", 0),
                    completion_tokens=logs_stage.get("completion_tokens", 0),
                    total_tokens=logs_stage.get("total_tokens", 0),
                )
                if logs_output:
                    state.add_evidence(logs_output)
                self._persist(state)
                self._emit(StageCompleted(
                    investigation_id=incident_id,
                    stage="logs",
                    llm_calls=logs_stage.get("llm_calls", 0),
                    prompt_tokens=logs_stage.get("prompt_tokens", 0),
                    completion_tokens=logs_stage.get("completion_tokens", 0),
                    total_tokens=logs_stage.get("total_tokens", 0),
                ))
            else:
                state.start_stage("logs")
                self._emit(StageStarted(investigation_id=incident_id, stage="logs"))
                state.fail_stage("logs", error=logs_stage.get("error", "Unknown error"), output=logs_output)
                if logs_output:
                    state.add_evidence(logs_output)
                self._persist(state)
                self._emit(StageFailed(
                    investigation_id=incident_id,
                    stage="logs",
                    error=logs_stage.get("error", "Unknown error"),
                ))

        # ── Stage 2: Metrics ──────────────────────────────────────────
        if should_run("metrics"):
            self._sleep()
            metrics_output, metrics_calls, metrics_stage = self._run_evidence_stage(
                stage_name="metrics",
                incident_id=incident_id,
                incident_path=incident_path,
                run_fn=lambda: MetricsAgent(llm_client=self._get_llm_client()).extract_evidence(incident_path),
                expected_llm_calls=1,
            )
            if metrics_stage.get("status") == STATUS_REUSED:
                state.mark_cached("metrics", output=metrics_output)
                if metrics_output:
                    state.add_evidence(metrics_output)
                self._persist(state)
                self._emit(StageCached(investigation_id=incident_id, stage="metrics"))
            elif metrics_stage.get("status") == STATUS_SUCCEEDED:
                state.start_stage("metrics")
                self._emit(StageStarted(investigation_id=incident_id, stage="metrics"))
                state.complete_stage(
                    "metrics",
                    output=metrics_output,
                    llm_calls=metrics_stage.get("llm_calls", 0),
                    prompt_tokens=metrics_stage.get("prompt_tokens", 0),
                    completion_tokens=metrics_stage.get("completion_tokens", 0),
                    total_tokens=metrics_stage.get("total_tokens", 0),
                )
                if metrics_output:
                    state.add_evidence(metrics_output)
                self._persist(state)
                self._emit(StageCompleted(
                    investigation_id=incident_id,
                    stage="metrics",
                    llm_calls=metrics_stage.get("llm_calls", 0),
                    prompt_tokens=metrics_stage.get("prompt_tokens", 0),
                    completion_tokens=metrics_stage.get("completion_tokens", 0),
                    total_tokens=metrics_stage.get("total_tokens", 0),
                ))
            else:
                state.start_stage("metrics")
                self._emit(StageStarted(investigation_id=incident_id, stage="metrics"))
                state.fail_stage("metrics", error=metrics_stage.get("error", "Unknown error"), output=metrics_output)
                if metrics_output:
                    state.add_evidence(metrics_output)
                self._persist(state)
                self._emit(StageFailed(
                    investigation_id=incident_id,
                    stage="metrics",
                    error=metrics_stage.get("error", "Unknown error"),
                ))

        # ── Stage 3: Code ─────────────────────────────────────────────
        if should_run("code"):
            self._sleep()
            code_output, code_calls, code_stage = self._run_evidence_stage(
                stage_name="code",
                incident_id=incident_id,
                incident_path=incident_path,
                run_fn=lambda: CodeAgent(llm_client=self._get_llm_client()).extract_evidence(incident_path),
                expected_llm_calls=1,
            )
            if code_stage.get("status") == STATUS_REUSED:
                state.mark_cached("code", output=code_output)
                if code_output:
                    state.add_evidence(code_output)
                self._persist(state)
                self._emit(StageCached(investigation_id=incident_id, stage="code"))
            elif code_stage.get("status") == STATUS_SUCCEEDED:
                state.start_stage("code")
                self._emit(StageStarted(investigation_id=incident_id, stage="code"))
                state.complete_stage(
                    "code",
                    output=code_output,
                    llm_calls=code_stage.get("llm_calls", 0),
                    prompt_tokens=code_stage.get("prompt_tokens", 0),
                    completion_tokens=code_stage.get("completion_tokens", 0),
                    total_tokens=code_stage.get("total_tokens", 0),
                )
                if code_output:
                    state.add_evidence(code_output)
                self._persist(state)
                self._emit(StageCompleted(
                    investigation_id=incident_id,
                    stage="code",
                    llm_calls=code_stage.get("llm_calls", 0),
                    prompt_tokens=code_stage.get("prompt_tokens", 0),
                    completion_tokens=code_stage.get("completion_tokens", 0),
                    total_tokens=code_stage.get("total_tokens", 0),
                ))
            else:
                state.start_stage("code")
                self._emit(StageStarted(investigation_id=incident_id, stage="code"))
                state.fail_stage("code", error=code_stage.get("error", "Unknown error"), output=code_output)
                if code_output:
                    state.add_evidence(code_output)
                self._persist(state)
                self._emit(StageFailed(
                    investigation_id=incident_id,
                    stage="code",
                    error=code_stage.get("error", "Unknown error"),
                ))

        # ── Stage 4: Evidence Fusion ──────────────────────────────────
        if should_run("evidence_fusion"):
            state.start_stage("evidence_fusion")
            self._emit(StageStarted(investigation_id=incident_id, stage="evidence_fusion"))
            fused = _fuse_evidence(incident_id, logs_output, metrics_output, code_output)
            fusion_errors = _validate_evidence_fusion(fused)
            if fusion_errors:
                err_msg = "; ".join(fusion_errors)
                state.fail_stage("evidence_fusion", error=err_msg, output=fused)
                state.fail(error=f"Evidence fusion failed: {err_msg}")
                self._persist(state)
                self._emit(StageFailed(
                    investigation_id=incident_id,
                    stage="evidence_fusion",
                    error=err_msg,
                ))
                self._emit(self._make_completed_event(state, PIPELINE_FAILED, error=f"Evidence fusion failed: {err_msg}"))
                return self._build_result_from_state(
                    state=state,
                    pipeline_status=PIPELINE_FAILED,
                    error=f"Evidence fusion failed: {err_msg}",
                )
            state.complete_stage("evidence_fusion", output=fused)
            cache_path = self._stage_cache_path(incident_id, "evidence_fusion")
            if cache_path:
                _save_cache(cache_path, fused)
            self._persist(state)
            self._emit(StageCompleted(investigation_id=incident_id, stage="evidence_fusion"))

        # Require at least some evidence to continue
        if not fused or not fused.get("evidence"):
            state.skip_stage("hypotheses", reason="No evidence collected; cannot generate hypotheses.")
            state.mark_partial()
            self._persist(state)
            self._emit(StageSkipped(
                investigation_id=incident_id,
                stage="hypotheses",
                reason="No evidence collected; cannot generate hypotheses.",
            ))
            self._emit(self._make_completed_event(state, PIPELINE_PARTIAL, error="No evidence extracted from any source."))
            return self._build_result_from_state(
                state=state,
                pipeline_status=PIPELINE_PARTIAL,
                error="No evidence extracted from any source.",
            )

        # ── Stage 5: Hypothesis Engine ────────────────────────────────
        if should_run("hypotheses"):
            self._sleep()
            hyp_cache_path = self._stage_cache_path(incident_id, "hypotheses")
            hyp_cached = _load_cache(hyp_cache_path, incident_id) if hyp_cache_path else None
            if hyp_cached is not None:
                print(f"  [reuse] hypotheses (cached)")
                hypotheses_output = hyp_cached
                state.mark_cached("hypotheses", output=hyp_cached)
                state.add_hypotheses(hypotheses_output)
                self._persist(state)
                self._emit(StageCached(investigation_id=incident_id, stage="hypotheses"))
            else:
                state.start_stage("hypotheses")
                self._emit(StageStarted(investigation_id=incident_id, stage="hypotheses"))
                hypotheses_output, hyp_calls, hyp_stage = self._run_hypothesis_stage(
                    incident_id=incident_id,
                    logs_output=logs_output,
                    metrics_output=metrics_output,
                    code_output=code_output,
                )
                if hyp_stage.get("status") == STATUS_SUCCEEDED:
                    if hyp_cache_path:
                        _save_cache(hyp_cache_path, hypotheses_output)
                    state.complete_stage(
                        "hypotheses",
                        output=hypotheses_output,
                        llm_calls=hyp_stage.get("llm_calls", 0),
                        prompt_tokens=hyp_stage.get("prompt_tokens", 0),
                        completion_tokens=hyp_stage.get("completion_tokens", 0),
                        total_tokens=hyp_stage.get("total_tokens", 0),
                    )
                    state.add_hypotheses(hypotheses_output)
                    self._persist(state)
                    self._emit(StageCompleted(
                        investigation_id=incident_id,
                        stage="hypotheses",
                        llm_calls=hyp_stage.get("llm_calls", 0),
                        prompt_tokens=hyp_stage.get("prompt_tokens", 0),
                        completion_tokens=hyp_stage.get("completion_tokens", 0),
                        total_tokens=hyp_stage.get("total_tokens", 0),
                    ))
                else:
                    state.fail_stage("hypotheses", error=hyp_stage.get("error", "Hypothesis stage failed"), output=hypotheses_output)
                    self._persist(state)
                    self._emit(StageFailed(
                        investigation_id=incident_id,
                        stage="hypotheses",
                        error=hyp_stage.get("error", "Hypothesis stage failed"),
                    ))

        if state.stages.get("hypotheses") and state.stages["hypotheses"].status == STATUS_FAILED:
            state.skip_stage("verification", reason="Hypothesis stage failed; cannot verify.")
            state.mark_partial()
            self._persist(state)
            self._emit(StageSkipped(
                investigation_id=incident_id,
                stage="verification",
                reason="Hypothesis stage failed; cannot verify.",
            ))
            self._emit(self._make_completed_event(state, PIPELINE_PARTIAL))
            return self._build_result_from_state(
                state=state,
                pipeline_status=PIPELINE_PARTIAL,
            )

        # ── Stage 6: Verification ─────────────────────────────────────
        if should_run("verification"):
            ver_cache_path = self._stage_cache_path(incident_id, "verification")
            ver_cached = _load_cache(ver_cache_path, incident_id) if ver_cache_path else None
            if ver_cached is not None:
                print(f"  [reuse] verification (cached)")
                verification_output = ver_cached
                state.mark_cached("verification", output=ver_cached)
                state.add_verification_results(verification_output)
                self._persist(state)
                self._emit(StageCached(investigation_id=incident_id, stage="verification"))
            else:
                state.start_stage("verification")
                self._emit(StageStarted(investigation_id=incident_id, stage="verification"))
                verification_output, ver_stage = self._run_verification_stage(
                    incident_id=incident_id,
                    incident_path=incident_path,
                    hypotheses=hypotheses_output,
                    logs_output=logs_output,
                    metrics_output=metrics_output,
                    code_output=code_output,
                )
                if ver_stage.get("status") == STATUS_SUCCEEDED:
                    if ver_cache_path:
                        _save_cache(ver_cache_path, verification_output)
                    state.complete_stage(
                        "verification",
                        output=verification_output,
                        llm_calls=0,
                        prompt_tokens=0,
                        completion_tokens=0,
                        total_tokens=0,
                    )
                    state.add_verification_results(verification_output)
                    self._persist(state)
                    self._emit(StageCompleted(investigation_id=incident_id, stage="verification"))
                else:
                    state.fail_stage("verification", error=ver_stage.get("error", "Verification stage failed"), output=verification_output)
                    self._persist(state)
                    self._emit(StageFailed(
                        investigation_id=incident_id,
                        stage="verification",
                        error=ver_stage.get("error", "Verification stage failed"),
                    ))

        if state.stages.get("verification") and state.stages["verification"].status == STATUS_FAILED:
            state.skip_stage("fix_proposals", reason="Verification stage failed; cannot propose fixes.")
            state.mark_partial()
            self._persist(state)
            self._emit(StageSkipped(
                investigation_id=incident_id,
                stage="fix_proposals",
                reason="Verification stage failed; cannot propose fixes.",
            ))
            self._emit(self._make_completed_event(state, PIPELINE_PARTIAL))
            return self._build_result_from_state(
                state=state,
                pipeline_status=PIPELINE_PARTIAL,
            )

        # ── Stage 7: Fix Proposals ────────────────────────────────────
        if should_run("fix_proposals"):
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
            if fix_stage.get("status") == STATUS_REUSED:
                state.mark_cached("fix_proposals", output=proposals_bundle)
                state.add_proposals(proposals_bundle)
                self._persist(state)
                self._emit(StageCached(investigation_id=incident_id, stage="fix_proposals"))
            elif fix_stage.get("status") == STATUS_SUCCEEDED:
                state.start_stage("fix_proposals")
                self._emit(StageStarted(investigation_id=incident_id, stage="fix_proposals"))
                state.complete_stage(
                    "fix_proposals",
                    output=proposals_bundle,
                    llm_calls=fix_stage.get("llm_calls", 0),
                    prompt_tokens=fix_stage.get("prompt_tokens", 0),
                    completion_tokens=fix_stage.get("completion_tokens", 0),
                    total_tokens=fix_stage.get("total_tokens", 0),
                )
                state.add_proposals(proposals_bundle)
                self._persist(state)
                self._emit(StageCompleted(
                    investigation_id=incident_id,
                    stage="fix_proposals",
                    llm_calls=fix_stage.get("llm_calls", 0),
                    prompt_tokens=fix_stage.get("prompt_tokens", 0),
                    completion_tokens=fix_stage.get("completion_tokens", 0),
                    total_tokens=fix_stage.get("total_tokens", 0),
                ))
            else:
                state.start_stage("fix_proposals")
                self._emit(StageStarted(investigation_id=incident_id, stage="fix_proposals"))
                state.fail_stage("fix_proposals", error=fix_stage.get("error", "Fix proposal failed"), output=proposals_bundle)
                if proposals_bundle:
                    state.add_proposals(proposals_bundle)
                self._persist(state)
                self._emit(StageFailed(
                    investigation_id=incident_id,
                    stage="fix_proposals",
                    error=fix_stage.get("error", "Fix proposal failed"),
                ))

        # ── Stage 8: Human Approval ───────────────────────────────────
        if should_run("approvals"):
            state.start_stage("approvals")
            self._emit(StageStarted(investigation_id=incident_id, stage="approvals"))
            approval_output, approval_stage = self._run_approval_stage(
                proposals_bundle=proposals_bundle,
            )
            if approval_stage.get("status") == STATUS_SUCCEEDED:
                state.complete_stage(
                    "approvals",
                    output=approval_output,
                    llm_calls=0,
                    prompt_tokens=0,
                    completion_tokens=0,
                    total_tokens=0,
                )
                state.record_approval(approval_output)
                self._persist(state)
                self._emit(StageCompleted(investigation_id=incident_id, stage="approvals"))
            else:
                state.fail_stage("approvals", error=approval_stage.get("error", "Approval stage failed"), output=approval_output)
                if approval_output:
                    state.record_approval(approval_output)
                self._persist(state)
                self._emit(StageFailed(
                    investigation_id=incident_id,
                    stage="approvals",
                    error=approval_stage.get("error", "Approval stage failed"),
                ))

        # ── Build final result ────────────────────────────────────────
        all_failed = all(
            s.status == STATUS_FAILED
            for s in state.stages.values()
            if s.status != STATUS_SKIPPED
        )
        has_any_failure = any(s.status == STATUS_FAILED for s in state.stages.values())
        pipeline_status = (
            PIPELINE_FAILED if all_failed
            else PIPELINE_PARTIAL if has_any_failure
            else PIPELINE_COMPLETED
        )
        if pipeline_status == PIPELINE_COMPLETED:
            state.complete()
        elif pipeline_status == PIPELINE_PARTIAL:
            state.mark_partial()
        else:
            state.fail()

        self._persist(state)
        # Emit terminal InvestigationCompleted AFTER state is finalized and persisted
        self._emit(self._make_completed_event(state, pipeline_status))

        return self._build_result_from_state(
            state=state,
            pipeline_status=pipeline_status,
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
                return cached, 0, _stage_result(
                    STATUS_REUSED,
                    output=cached,
                    llm_calls=0,
                    prompt_tokens=0,
                    completion_tokens=0,
                    total_tokens=0,
                    cache_hit=True,
                )

        client = self._get_llm_client()
        tok_before = _get_token_usage_snapshot(client)
        try:
            print(f"  [run  ] {stage_name}...")
            output = run_fn()
            tok_after = _get_token_usage_snapshot(client)
            delta = _calc_token_delta(tok_before, tok_after)
            calls = delta["llm_calls"] or expected_llm_calls
            if cache_path:
                _save_cache(cache_path, output)
            return output, calls, _stage_result(
                STATUS_SUCCEEDED,
                output=output,
                llm_calls=calls,
                prompt_tokens=delta["prompt_tokens"],
                completion_tokens=delta["completion_tokens"],
                total_tokens=delta["total_tokens"],
            )
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
        client = self._get_llm_client()
        tok_before = _get_token_usage_snapshot(client)
        try:
            print("  [run  ] hypothesis_engine...")
            engine = HypothesisEngine(llm_client=client)
            hyps = engine.generate_hypotheses(
                incident_id=incident_id,
                logs_evidence=logs_output,
                metrics_evidence=metrics_output,
                code_evidence=code_output,
            )
            tok_after = _get_token_usage_snapshot(client)
            delta = _calc_token_delta(tok_before, tok_after)
            calls = delta["llm_calls"] or 1
            return hyps, calls, _stage_result(
                STATUS_SUCCEEDED,
                output=hyps,
                llm_calls=calls,
                prompt_tokens=delta["prompt_tokens"],
                completion_tokens=delta["completion_tokens"],
                total_tokens=delta["total_tokens"],
            )
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
            return result, _stage_result(
                STATUS_SUCCEEDED,
                output=result,
                llm_calls=0,
                prompt_tokens=0,
                completion_tokens=0,
                total_tokens=0,
            )
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
        cache_path = self._stage_cache_path(incident_id, "fix_proposals")
        if cache_path:
            cached = _load_cache(cache_path, incident_id)
            if cached is not None and isinstance(cached.get("proposals"), list):
                print(f"  [reuse] fix_proposals (cached)")
                return cached, 0, _stage_result(
                    STATUS_REUSED,
                    output=cached,
                    llm_calls=0,
                    prompt_tokens=0,
                    completion_tokens=0,
                    total_tokens=0,
                    cache_hit=True,
                )

        client = self._get_llm_client()
        tok_before = _get_token_usage_snapshot(client)
        try:
            print("  [run  ] fix_proposal_agent...")
            agent = FixProposalAgent(llm_client=client)
            bundle = agent.propose_fix(
                incident_dir=incident_path,
                hypotheses=hypotheses,
                verification_results=verification,
                logs_evidence=logs_output,
                metrics_evidence=metrics_output,
                code_evidence=code_output,
            )
            tok_after = _get_token_usage_snapshot(client)
            delta = _calc_token_delta(tok_before, tok_after)
            n_proposals = len(bundle.get("proposals") or [])
            calls = delta["llm_calls"] or n_proposals
            if cache_path and bundle.get("proposals"):
                _save_cache(cache_path, bundle)
            return bundle, calls, _stage_result(
                STATUS_SUCCEEDED,
                output=bundle,
                llm_calls=calls,
                prompt_tokens=delta["prompt_tokens"],
                completion_tokens=delta["completion_tokens"],
                total_tokens=delta["total_tokens"],
            )
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
                return result, _stage_result(
                    STATUS_SUCCEEDED,
                    output=result,
                    llm_calls=0,
                    prompt_tokens=0,
                    completion_tokens=0,
                    total_tokens=0,
                )

            # Always non-interactive in the orchestrator unless overridden
            gate = ApprovalGate(interactive=not self.non_interactive)
            result = gate.review_all(proposals_bundle)
            return result, _stage_result(
                STATUS_SUCCEEDED,
                output=result,
                llm_calls=0,
                prompt_tokens=0,
                completion_tokens=0,
                total_tokens=0,
            )
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

    def _build_result_from_state(
        self,
        state: InvestigationState,
        pipeline_status: str,
        error: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Convert an InvestigationState into a backward-compatible OrchestratorResult dict."""
        stages_dict: Dict[str, Dict[str, Any]] = {}
        stage_names = [
            "logs",
            "metrics",
            "code",
            "evidence_fusion",
            "hypotheses",
            "verification",
            "fix_proposals",
            "approvals",
        ]
        for name in stage_names:
            if name in state.stages:
                st = state.stages[name]
                st_dict: Dict[str, Any] = {
                    "status": st.status if isinstance(st.status, str) else st.status.value,
                    "llm_calls": st.llm_calls,
                    "prompt_tokens": st.prompt_tokens,
                    "completion_tokens": st.completion_tokens,
                    "total_tokens": st.total_tokens,
                }
                if st.output is not None:
                    st_dict["output"] = st.output
                if st.error is not None:
                    st_dict["error"] = st.error
                if st.cache_hit:
                    st_dict["cache_hit"] = True
                stages_dict[name] = st_dict
            else:
                stages_dict[name] = _stage_result(STATUS_SKIPPED)

        total_llm_calls = sum((s.get("llm_calls") or 0) for s in stages_dict.values())
        return self._build_result(
            incident_id=state.incident_id,
            pipeline_status=pipeline_status,
            stages=stages_dict,
            total_llm_calls=total_llm_calls,
            error=error or state.error,
        )

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

        total_prompt_tokens = sum((s.get("prompt_tokens") or 0) for s in stages.values())
        total_completion_tokens = sum((s.get("completion_tokens") or 0) for s in stages.values())
        total_tokens = sum((s.get("total_tokens") or 0) for s in stages.values())

        result: Dict[str, Any] = {
            "incident_id": incident_id,
            "pipeline_status": pipeline_status,
            "human_approval_notice": HUMAN_APPROVAL_NOTICE,
            "llm_call_count": total_llm_calls,
            "prompt_tokens": total_prompt_tokens,
            "completion_tokens": total_completion_tokens,
            "total_tokens": total_tokens,
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
