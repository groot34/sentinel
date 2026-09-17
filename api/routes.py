"""Route definitions for Sentinel 2.0 API Layer."""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any, Dict, Optional

from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import JSONResponse

from agents.orchestrator import IncidentOrchestrator
from api.errors import error_json_response, sanitize_error_message
from api.models import (
    _SAFE_ID_REGEX,
    CreateInvestigationRequest,
    CreateInvestigationResponse,
    GetInvestigationResponse,
    HealthResponse,
    InvestigationSummaryResponse,
    ListInvestigationsResponse,
    StageSummaryResponse,
)
from core.domain.models import IncidentStatus
from core.persistence import ConcurrencyError, PersistenceError, PersistenceRepository

# ---------------------------------------------------------------------------
# Process-local atomic active investigation registry
# ---------------------------------------------------------------------------
_ACTIVE_LOCK = threading.Lock()
_ACTIVE_INVESTIGATIONS: set[str] = set()


def _claim_investigation(incident_id: str, repository: Optional[PersistenceRepository]) -> None:
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
            if existing_state is not None:
                status_str = (
                    existing_state.status.value
                    if isinstance(existing_state.status, IncidentStatus)
                    else str(existing_state.status)
                )
                raise HTTPException(
                    status_code=409,
                    detail=f"Investigation already exists with status '{status_str}'.",
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
    _claim_investigation(incident_id, repository)

    try:
        orchestrator = IncidentOrchestrator(
            llm_client=llm_client,
            non_interactive=True,
            repository=repository,
        )
        result = orchestrator.investigate(candidate_path)
    finally:
        _release_investigation(incident_id)

    # Allowlist response projection
    state = orchestrator.state
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
