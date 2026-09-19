"""Route definitions for Sentinel 2.0 API Layer."""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import JSONResponse

from agents.approval_gate import _build_approval_record
from agents.orchestrator import IncidentOrchestrator
from api.errors import error_json_response, sanitize_error_message
from api.models import (
    _SAFE_ID_REGEX,
    ApprovalRecordResponse,
    CreateInvestigationRequest,
    CreateInvestigationResponse,
    GetInvestigationResponse,
    HealthResponse,
    InvestigationSummaryResponse,
    ListInvestigationsResponse,
    StageSummaryResponse,
    SubmitApprovalRequest,
    SubmitApprovalResponse,
)
from core.domain.models import ApprovalRecord, IncidentStatus
from core.persistence import ConcurrencyError, PersistenceError, PersistenceRepository

# ---------------------------------------------------------------------------
# Process-local atomic active investigation registry
# ---------------------------------------------------------------------------
_ACTIVE_LOCK = threading.Lock()
_ACTIVE_INVESTIGATIONS: set[str] = set()


def _claim_investigation(
    incident_id: str,
    repository: Optional[PersistenceRepository],
    allow_resume: bool = False,
) -> None:
    """Atomically check and register an incident ID as actively running.

    The check-persisted -> check-active -> register-active sequence is
    fully executed inside _ACTIVE_LOCK to prevent TOCTOU races in this process.
    """
    with _ACTIVE_LOCK:
        if incident_id in _ACTIVE_INVESTIGATIONS:
            raise HTTPException(
                status_code=409,
                detail="Investigation is already active in this process.",
            )
        if repository is not None:
            existing_state = repository.load(incident_id)
            if not allow_resume and existing_state is not None:
                status_str = (
                    existing_state.status.value
                    if isinstance(existing_state.status, IncidentStatus)
                    else str(existing_state.status)
                )
                raise HTTPException(
                    status_code=409,
                    detail=f"Investigation already exists with status '{status_str}'. Use POST /investigations/{incident_id}/resume to resume.",
                )
            if allow_resume:
                if existing_state is None:
                    raise HTTPException(
                        status_code=404,
                        detail=f"No persisted investigation found for '{incident_id}' to resume.",
                    )
                status_str = (
                    existing_state.status.value
                    if isinstance(existing_state.status, IncidentStatus)
                    else str(existing_state.status)
                )
                if status_str in ("COMPLETED", "completed"):
                    raise HTTPException(
                        status_code=409,
                        detail=f"Investigation '{incident_id}' is already completed and cannot be resumed.",
                    )
                if status_str in ("RUNNING", "running"):
                    raise HTTPException(
                        status_code=409,
                        detail=f"Investigation '{incident_id}' has status 'RUNNING' and cannot be safely resumed.",
                    )
                if status_str not in ("FAILED", "failed", "PARTIAL", "partial"):
                    raise HTTPException(
                        status_code=409,
                        detail=f"Investigation '{incident_id}' has status '{status_str}' which cannot be resumed.",
                    )
        _ACTIVE_INVESTIGATIONS.add(incident_id)


def _release_investigation(incident_id: str) -> None:
    """Safely unregister an incident ID from the active set under lock."""
    with _ACTIVE_LOCK:
        _ACTIVE_INVESTIGATIONS.discard(incident_id)


def _validate_id_param(investigation_id: str) -> str:
    """Validate a path parameter investigation ID."""
    if not isinstance(investigation_id, str):
        raise HTTPException(status_code=400, detail="Investigation ID must be a string.")
    stripped = investigation_id.strip()
    if not stripped:
        raise HTTPException(status_code=400, detail="Investigation ID cannot be empty.")
    if "/" in stripped or "\\" in stripped or ".." in stripped or ":" in stripped:
        raise HTTPException(status_code=400, detail="Path traversal characters detected in ID.")
    if not _SAFE_ID_REGEX.match(stripped):
        raise HTTPException(status_code=400, detail="Invalid investigation ID format.")
    return stripped


def _project_investigation_result(
    incident_id: str,
    result: Dict[str, Any],
    state: Optional[Any],
) -> JSONResponse:
    """Project raw orchestrator result and state into CreateInvestigationResponse."""
    status_val = (
        state.status.value
        if state and isinstance(state.status, IncidentStatus)
        else (str(state.status) if state else result.get("pipeline_status", "UNKNOWN"))
    )

    stages_projected: Dict[str, Any] = {}
    for st_name, st_dict in (result.get("stages") or {}).items():
        stages_projected[st_name] = StageSummaryResponse(
            status=st_dict.get("status", "UNKNOWN"),
            llm_calls=st_dict.get("llm_calls", 0),
            prompt_tokens=st_dict.get("prompt_tokens", 0),
            completion_tokens=st_dict.get("completion_tokens", 0),
            total_tokens=st_dict.get("total_tokens", 0),
            error=sanitize_error_message(st_dict["error"]) if st_dict.get("error") else None,
            cache_hit=bool(st_dict.get("cache_hit") or False),
        ).model_dump()

    summary_raw = result.get("summary") or {}
    summary_model = InvestigationSummaryResponse(
        confirmed_hypotheses=summary_raw.get("confirmed_hypotheses", 0),
        rejected_hypotheses=summary_raw.get("rejected_hypotheses", 0),
        inconclusive_hypotheses=summary_raw.get("inconclusive_hypotheses", 0),
        proposals_generated=summary_raw.get("proposals_generated", 0),
        proposals_approved=summary_raw.get("proposals_approved", 0),
        proposals_rejected=summary_raw.get("proposals_rejected", 0),
    )

    resp_model = CreateInvestigationResponse(
        investigation_id=result.get("incident_id", incident_id),
        pipeline_status=result.get("pipeline_status", "UNKNOWN"),
        status=status_val,
        started_at=state.started_at if state else result.get("started_at", ""),
        completed_at=state.completed_at if state else result.get("completed_at"),
        error=sanitize_error_message(result["error"]) if result.get("error") else None,
        llm_call_count=result.get("llm_call_count", 0),
        prompt_tokens=result.get("prompt_tokens", 0),
        completion_tokens=result.get("completion_tokens", 0),
        total_tokens=result.get("total_tokens", 0),
        summary=summary_model,
        stages=stages_projected,
        human_approval_notice=result.get("human_approval_notice", ""),
    )
    return JSONResponse(status_code=200, content=resp_model.model_dump())


# ---------------------------------------------------------------------------
# Route Handlers (Synchronous - offloaded to AnyIO thread pool by Starlette)
# ---------------------------------------------------------------------------

def health(request: Request) -> JSONResponse:
    """GET /health - Liveness probe."""
    resp = HealthResponse()
    return JSONResponse(status_code=200, content=resp.model_dump())


def create_investigation(request: Request) -> JSONResponse:
    """POST /investigations - Synchronously run an incident investigation."""
    try:
        raw_body = getattr(request, "_body", None)
        if raw_body is None:
            import anyio
            raw_body = anyio.from_thread.run(request.body)
        body = json.loads(raw_body.decode("utf-8")) if raw_body else {}
    except Exception:
        return error_json_response(400, "invalid_json", "Malformed JSON request body.")

    if not isinstance(body, dict):
        return error_json_response(400, "invalid_json", "Request body must be a JSON object.")

    try:
        req_model = CreateInvestigationRequest.model_validate(body)
        incident_id = req_model.incident_id
    except Exception as e:
        return error_json_response(400, "invalid_incident_id", str(e))

    incidents_root = Path(getattr(request.app.state, "incidents_root", Path("incidents")))
    candidate_path = incidents_root / incident_id

    # Symlink rejection: symlinked incident directories are strictly forbidden
    if candidate_path.is_symlink():
        return error_json_response(400, "invalid_incident_id", "Symlinked incident directories are not allowed.")

    if not candidate_path.is_dir():
        return error_json_response(404, "incident_not_found", f"Incident directory '{incident_id}' not found.")

    # Canonical path containment check
    resolved_candidate = candidate_path.resolve()
    resolved_root = incidents_root.resolve()
    if resolved_candidate.parent != resolved_root or candidate_path.name != incident_id:
        return error_json_response(400, "invalid_incident_id", "Incident ID must identify a direct child directory.")

    repository = getattr(request.app.state, "repository", None)
    llm_client = getattr(request.app.state, "llm_client", None)

    # Perform atomic process-local claim & persisted state check
    _claim_investigation(incident_id, repository, allow_resume=False)

    try:
        orchestrator = IncidentOrchestrator(
            llm_client=llm_client,
            non_interactive=True,
            repository=repository,
        )
        result = orchestrator.investigate(candidate_path)
    finally:
        _release_investigation(incident_id)

    return _project_investigation_result(incident_id, result, orchestrator.state)


def resume_investigation(request: Request) -> JSONResponse:
    """POST /investigations/{investigation_id}/resume - Resume an interrupted/failed investigation."""
    raw_id = request.path_params.get("investigation_id", "")
    try:
        safe_id = _validate_id_param(raw_id)
    except HTTPException as he:
        return error_json_response(he.status_code, "invalid_investigation_id", str(he.detail))

    incidents_root = Path(getattr(request.app.state, "incidents_root", Path("incidents")))
    candidate_path = incidents_root / safe_id

    # Symlink rejection
    if candidate_path.is_symlink():
        return error_json_response(400, "invalid_incident_id", "Symlinked incident directories are not allowed.")

    if not candidate_path.is_dir():
        return error_json_response(404, "incident_not_found", f"Incident directory '{safe_id}' not found.")

    # Canonical path containment check
    resolved_candidate = candidate_path.resolve()
    resolved_root = incidents_root.resolve()
    if resolved_candidate.parent != resolved_root or candidate_path.name != safe_id:
        return error_json_response(400, "invalid_incident_id", "Incident ID must identify a direct child directory.")

    repository = getattr(request.app.state, "repository", None)
    if repository is None:
        return error_json_response(503, "persistence_unavailable", "Persistence repository is required for resume.")

    llm_client = getattr(request.app.state, "llm_client", None)

    # Perform atomic process-local claim & persisted state check with allow_resume=True
    _claim_investigation(safe_id, repository, allow_resume=True)

    try:
        orchestrator = IncidentOrchestrator(
            llm_client=llm_client,
            non_interactive=True,
            repository=repository,
        )
        result = orchestrator.resume(investigation_id=safe_id, incident_dir=candidate_path)
    finally:
        _release_investigation(safe_id)

    return _project_investigation_result(safe_id, result, orchestrator.state)


def submit_approval(request: Request) -> JSONResponse:
    """POST /investigations/{investigation_id}/approval - Record human approval for a fix proposal."""
    raw_id = request.path_params.get("investigation_id", "")
    try:
        safe_id = _validate_id_param(raw_id)
    except HTTPException as he:
        return error_json_response(he.status_code, "invalid_investigation_id", str(he.detail))

    try:
        raw_body = getattr(request, "_body", None)
        if raw_body is None:
            import anyio
            raw_body = anyio.from_thread.run(request.body)
        body = json.loads(raw_body.decode("utf-8")) if raw_body else {}
    except Exception:
        return error_json_response(400, "invalid_json", "Malformed JSON request body.")

    if not isinstance(body, dict):
        return error_json_response(400, "invalid_json", "Request body must be a JSON object.")

    try:
        req_model = SubmitApprovalRequest.model_validate(body)
    except Exception as e:
        return error_json_response(400, "invalid_approval_request", str(e))

    repository = getattr(request.app.state, "repository", None)
    if repository is None:
        return error_json_response(503, "persistence_unavailable", "Persistence repository is not configured.")

    with _ACTIVE_LOCK:
        if safe_id in _ACTIVE_INVESTIGATIONS:
            raise HTTPException(
                status_code=409,
                detail="Cannot record approval while investigation is actively running.",
            )

    state = repository.load(safe_id)
    if state is None:
        return error_json_response(404, "investigation_not_found", f"Investigation '{safe_id}' not found.")

    # Confirm proposal exists in this investigation
    matching_proposal = next(
        (p for p in state.proposals if hasattr(p, "proposal_id") and p.proposal_id == req_model.proposal_id),
        None,
    )
    if matching_proposal is None:
        fix_stage = state.stages.get("fix_proposals")
        proposals_raw = (fix_stage.output.get("proposals") or []) if (fix_stage and isinstance(fix_stage.output, dict)) else []
        matching_proposal = next(
            (p for p in proposals_raw if isinstance(p, dict) and p.get("proposal_id") == req_model.proposal_id),
            None,
        )

    if matching_proposal is None:
        return error_json_response(
            404,
            "proposal_not_found",
            f"Proposal '{req_model.proposal_id}' not found in investigation '{safe_id}'.",
        )

    # Protect prior human decisions from being overwritten.
    # Automated default records have approved_by == "human" and default note text.
    existing_record = next(
        (a for a in state.approvals if hasattr(a, "proposal_id") and a.proposal_id == req_model.proposal_id),
        None,
    )
    if existing_record is not None:
        notes_text = getattr(existing_record, "notes", "") or ""
        is_automated_default = (
            getattr(existing_record, "approved_by", "") == "human"
            and "defaulting to rejected" in notes_text.lower()
        )
        if not is_automated_default:
            prior_reviewer = getattr(existing_record, "approved_by", "unknown")
            prior_status = getattr(existing_record, "status", "UNKNOWN")
            return error_json_response(
                409,
                "proposal_already_reviewed",
                f"Proposal '{req_model.proposal_id}' has already been reviewed by '{prior_reviewer}' with decision '{prior_status}'. Prior human decisions cannot be overwritten.",
            )

    # Build schema-validated approval record
    decision_str = req_model.decision
    status_str = "APPROVED" if decision_str == "approved" else "REJECTED"
    record_dict = _build_approval_record(
        proposal_id=req_model.proposal_id,
        decision=decision_str,
        approved_by=req_model.reviewer,
        notes=req_model.notes,
    )
    appr_model = ApprovalRecord.from_dict(record_dict)

    state.update_approval(appr_model)

    # Update approvals stage output if present to maintain summary consistency
    if "approvals" in state.stages and isinstance(state.stages["approvals"].output, dict):
        appr_out = state.stages["approvals"].output
        records_list = appr_out.get("approvals") or []
        updated_records = []
        found = False
        for r in records_list:
            if isinstance(r, dict) and r.get("proposal_id") == req_model.proposal_id:
                updated_records.append(record_dict)
                found = True
            else:
                updated_records.append(r)
        if not found:
            updated_records.append(record_dict)
        appr_out["approvals"] = updated_records
        approved_count = sum(1 for r in updated_records if isinstance(r, dict) and r.get("status") == "APPROVED")
        rejected_count = sum(1 for r in updated_records if isinstance(r, dict) and r.get("status") == "REJECTED")
        appr_out["summary"] = {
            "total": len(updated_records),
            "approved": approved_count,
            "rejected": rejected_count,
        }

    # Persist updated state (OCC concurrency check)
    repository.save(state)

    status_val = (
        state.status.value
        if isinstance(state.status, IncidentStatus)
        else str(state.status)
    )

    resp = SubmitApprovalResponse(
        investigation_id=safe_id,
        approval=ApprovalRecordResponse(
            proposal_id=appr_model.proposal_id,
            status=status_str,
            decision=decision_str,
            approved_by=appr_model.approved_by,
            timestamp=appr_model.timestamp,
            notes=appr_model.notes,
        ),
        status=status_val,
        human_approval_notice="HUMAN REVIEW RECORDED — fix patch has not been applied.",
    )
    return JSONResponse(status_code=200, content=resp.model_dump())


def get_investigation(request: Request) -> JSONResponse:
    """GET /investigations/{investigation_id} - Retrieve persisted investigation state."""
    raw_id = request.path_params.get("investigation_id", "")
    try:
        safe_id = _validate_id_param(raw_id)
    except HTTPException as he:
        return error_json_response(he.status_code, "invalid_investigation_id", str(he.detail))

    repository = getattr(request.app.state, "repository", None)
    if repository is None:
        return error_json_response(503, "persistence_unavailable", "Persistence repository is not configured.")

    state = repository.load(safe_id)
    if state is None:
        return error_json_response(404, "investigation_not_found", f"Investigation '{safe_id}' not found.")

    status_val = (
        state.status.value
        if isinstance(state.status, IncidentStatus)
        else str(state.status)
    )

    stages_projected: Dict[str, Any] = {}
    for st_name, st_res in state.stages.items():
        st_status = (
            st_res.status.value
            if hasattr(st_res.status, "value")
            else str(st_res.status)
        )
        stages_projected[st_name] = StageSummaryResponse(
            status=st_status,
            llm_calls=st_res.llm_calls,
            prompt_tokens=st_res.prompt_tokens,
            completion_tokens=st_res.completion_tokens,
            total_tokens=st_res.total_tokens,
            error=sanitize_error_message(st_res.error) if st_res.error else None,
            cache_hit=bool(getattr(st_res, "cache_hit", False) or False),
        ).model_dump()

    approvals_projected: List[Dict[str, Any]] = []
    for appr in state.approvals:
        appr_status = appr.status.value if hasattr(appr.status, "value") else str(appr.status)
        appr_decision = appr.decision.value if hasattr(appr.decision, "value") else str(appr.decision)
        approvals_projected.append(
            ApprovalRecordResponse(
                proposal_id=appr.proposal_id,
                status=appr_status,
                decision=appr_decision,
                approved_by=appr.approved_by,
                timestamp=appr.timestamp,
                notes=appr.notes,
            ).model_dump()
        )

    resp_model = GetInvestigationResponse(
        investigation_id=state.incident_id,
        status=status_val,
        current_stage=state.current_stage,
        started_at=state.started_at,
        completed_at=state.completed_at,
        error=sanitize_error_message(state.error) if state.error else None,
        llm_call_count=state.llm_call_count,
        version=state.version,
        stages=stages_projected,
        approvals=approvals_projected,
    )
    return JSONResponse(status_code=200, content=resp_model.model_dump())


def list_investigations(request: Request) -> JSONResponse:
    """GET /investigations - List all stored investigation IDs."""
    repository = getattr(request.app.state, "repository", None)
    if repository is None:
        return error_json_response(503, "persistence_unavailable", "Persistence repository is not configured.")

    ids = repository.list()
    resp_model = ListInvestigationsResponse(investigations=ids)
    return JSONResponse(status_code=200, content=resp_model.model_dump())
