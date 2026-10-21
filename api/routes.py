"""Route definitions for Sentinel 2.0 API Layer."""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse

from agents.approval_gate import _build_approval_record
from agents.bundle_validator import require_valid_incident_bundle
from agents.orchestrator import IncidentOrchestrator
from api.errors import error_json_response, sanitize_error_message
from api.models import (
    _SAFE_ID_REGEX,
    _format_sse_event,
    _project_event_payload,
    AcceptedInvestigationResponse,
    ApprovalRecordResponse,
    CreateInvestigationRequest,
    CreateInvestigationResponse,
    EvidenceResultResponse,
    EventHistoryItemResponse,
    EventHistoryResponse,
    FileChangeResponse,
    GetInvestigationResponse,
    HealthResponse,
    HypothesisResultResponse,
    InvestigationResultResponse,
    InvestigationSummaryResponse,
    ListInvestigationsResponse,
    PREFER_ASYNC_HEADER_VALUE,
    ProposalResultResponse,
    StageSummaryResponse,
    SubmitApprovalRequest,
    SubmitApprovalResponse,
    VerificationCheckResponse,
    VerificationResultResponse,
)
from core.domain.models import ApprovalRecord, IncidentStatus
from core.domain.state import InvestigationState
from starlette.background import BackgroundTask
from core.persistence import ConcurrencyError, PersistenceError, PersistenceRepository
logger = logging.getLogger(__name__)

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
                    if existing_state.is_lease_active():
                        lease_owner = getattr(existing_state, "lease_owner", None) or "another worker"
                        raise HTTPException(
                            status_code=409,
                            detail=f"Investigation '{incident_id}' has status 'RUNNING' with active lease held by '{lease_owner}' and cannot be resumed.",
                        )
                elif status_str in ("FAILED", "failed", "PARTIAL", "partial"):
                    if existing_state.is_lease_active():
                        lease_owner = getattr(existing_state, "lease_owner", None) or "another worker"
                        raise HTTPException(
                            status_code=409,
                            detail=f"Investigation '{incident_id}' has status '{status_str}' with active lease held by '{lease_owner}' and cannot be resumed.",
                        )
                else:
                    raise HTTPException(
                        status_code=409,
                        detail=f"Investigation '{incident_id}' has status '{status_str}' which cannot be resumed.",
                    )
        _ACTIVE_INVESTIGATIONS.add(incident_id)


def _release_investigation(incident_id: str) -> None:
    """Safely unregister an incident ID from the active set under lock."""
    with _ACTIVE_LOCK:
        _ACTIVE_INVESTIGATIONS.discard(incident_id)


def _build_accepted_response(
    incident_id: str,
    request: Request,
    background: Optional[Any] = None,
) -> JSONResponse:
    """Build a 202 Accepted JSON response for async investigation dispatch.

    The base URL is derived from the request so that status/event URLs
    resolve correctly regardless of the server host, port, or mount prefix.
    """
    base = str(request.base_url).rstrip("/")
    status_url = f"{base}/investigations/{incident_id}"
    events_url = f"{base}/investigations/{incident_id}/events"
    resp = AcceptedInvestigationResponse(
        investigation_id=incident_id,
        status="RUNNING",
        message=(
            f"Investigation '{incident_id}' accepted for asynchronous execution. "
            "Poll status_url or connect to events_url to track progress."
        ),
        status_url=status_url,
        events_url=events_url,
    )
    return JSONResponse(
        status_code=202,
        content=resp.model_dump(),
        background=background,
        headers={
            "Preference-Applied": "respond-async",
            "Location": status_url,
        },
    )


def _run_investigation_background(
    incident_id: str,
    candidate_path: Any,
    repository: Optional[PersistenceRepository],
    llm_client: Optional[Any],
    worker_id: Optional[str] = None,
) -> None:
    """Background task: run the investigation pipeline and persist final state.

    This function runs in a Starlette BackgroundTask (a thread-pool thread).
    Ownership is claimed via the process-local registry and durable lease
    **before** this task is scheduled, using the same worker_id to guarantee
    ownership continuity between dispatch and execution.
    Ownership is released in the finally block here.

    Process-exit limitation: if the API process exits while this task is
    running, the investigation lease will expire after its TTL (default 300 s).
    Once the lease expires, any worker can acquire it and resume the
    investigation via POST /investigations/{id}/resume. There is no durable
    job queue; recovery depends entirely on lease expiry and explicit client
    resume calls.
    """
    try:
        orchestrator = IncidentOrchestrator(
            llm_client=llm_client,
            non_interactive=True,
            repository=repository,
            worker_id=worker_id,
        )
        orchestrator.investigate(candidate_path)
    except Exception as exc:
        logger.exception(
            "Background investigation failed for '%s': %s",
            incident_id,
            exc,
        )
        # Attempt to mark the investigation FAILED so it doesn't stay RUNNING
        if repository is not None:
            try:
                state = repository.load(incident_id)
                if state is not None:
                    status_val = (
                        state.status.value
                        if hasattr(state.status, "value")
                        else str(state.status)
                    )
                    if status_val not in ("COMPLETED", "FAILED", "PARTIAL"):
                        state.fail(error=f"Background execution failed: {type(exc).__name__}")
                        repository.save(state)
            except Exception as persist_exc:
                logger.error(
                    "Failed to persist FAILED state for '%s' after background error: %s",
                    incident_id,
                    persist_exc,
                )
    finally:
        _release_investigation(incident_id)


def _run_resume_background(
    incident_id: str,
    candidate_path: Any,
    repository: Optional[PersistenceRepository],
    llm_client: Optional[Any],
    worker_id: Optional[str] = None,
) -> None:
    """Background task: resume an investigation pipeline and persist final state.

    Same process-exit and recovery semantics as _run_investigation_background.
    Executes under the same worker_id acquired durably before HTTP 202 acceptance.
    """
    try:
        orchestrator = IncidentOrchestrator(
            llm_client=llm_client,
            non_interactive=True,
            repository=repository,
            worker_id=worker_id,
        )
        orchestrator.resume(investigation_id=incident_id, incident_dir=candidate_path)
    except Exception as exc:
        logger.exception(
            "Background resume failed for '%s': %s",
            incident_id,
            exc,
        )
        # Attempt to mark the investigation FAILED so it doesn't stay RUNNING
        if repository is not None:
            try:
                state = repository.load(incident_id)
                if state is not None:
                    status_val = (
                        state.status.value
                        if hasattr(state.status, "value")
                        else str(state.status)
                    )
                    if status_val not in ("COMPLETED", "FAILED", "PARTIAL"):
                        state.fail(error=f"Background resume failed: {type(exc).__name__}")
                        repository.save(state)
            except Exception as persist_exc:
                logger.error(
                    "Failed to persist FAILED state for '%s' after background resume error: %s",
                    incident_id,
                    persist_exc,
                )
    finally:
        _release_investigation(incident_id)


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
# Investigation result projection helpers (GET /investigations/{id}/result)
#
# Each helper reads only named attributes/dict keys from the persisted state
# and returns plain dicts that are then validated by the allowlisted response
# models.  Nothing is inferred: missing or malformed input yields empty/None
# rather than a fabricated value.
# ---------------------------------------------------------------------------

def _opt_str(value: Any) -> Optional[str]:
    """Return *value* as a plain string, or None for anything else.

    Accepts str and str-valued enums; never stringifies arbitrary objects, so
    no opaque repr/JSON blobs can leak into a response field.
    """
    if isinstance(value, str):
        return value
    inner = getattr(value, "value", None)
    if isinstance(inner, str):
        return inner
    return None


def _opt_float(value: Any) -> Optional[float]:
    """Return *value* as a float, or None when it is not a real number."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _opt_int(value: Any) -> Optional[int]:
    """Return *value* as an int, or None when it is not a real integer."""
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _str_list(value: Any) -> List[str]:
    """Return only the str members of *value* (empty list for non-sequences)."""
    if not isinstance(value, (list, tuple)):
        return []
    return [item for item in value if isinstance(item, str)]


def _project_evidence(state: InvestigationState) -> List[Dict[str, Any]]:
    """Project persisted evidence items onto the allowlisted evidence shape."""
    projected: List[Dict[str, Any]] = []
    for item in getattr(state, "evidence", None) or []:
        evidence_id = _opt_str(getattr(item, "evidence_id", None))
        if not evidence_id:
            continue
        source = _opt_str(getattr(item, "source", None)) or _opt_str(
            getattr(item, "source_type", None)
        )
        excerpt = _opt_str(getattr(item, "excerpt", None)) or _opt_str(
            getattr(item, "raw_snippet", None)
        )
        projected.append(
            EvidenceResultResponse(
                evidence_id=evidence_id,
                source=source,
                type=_opt_str(getattr(item, "type", None)),
                reference=_opt_str(getattr(item, "reference", None)),
                description=_opt_str(getattr(item, "description", None)),
                excerpt=excerpt,
                timestamp=_opt_str(getattr(item, "timestamp", None)),
                interpretation=_opt_str(getattr(item, "interpretation", None)),
                metric=_opt_str(getattr(item, "metric", None)),
                value=_opt_float(getattr(item, "value", None)),
            ).model_dump()
        )
    return projected


def _project_verification(state: InvestigationState) -> List[Dict[str, Any]]:
    """Project the persisted verification stage output.

    The verification stage output is the only persisted representation that
    keeps a check's discrete PASS/FAIL ``result`` and its ``reference``
    (file:line) — the normalised ``state.verification_results`` records fold
    that outcome into free text.  It is also the representation the
    orchestrator itself treats as authoritative for the result summary.
    """
    stage = (getattr(state, "stages", None) or {}).get("verification")
    output = getattr(stage, "output", None) if stage is not None else None
    if not isinstance(output, dict):
        return []

    raw_results = output.get("results")
    if not isinstance(raw_results, list):
        return []

    projected: List[Dict[str, Any]] = []
    for raw in raw_results:
        if not isinstance(raw, dict):
            continue
        hypothesis_id = _opt_str(raw.get("hypothesis_id"))
        verdict = _opt_str(raw.get("verdict"))
        if not hypothesis_id or not verdict:
            continue

        checks: List[Dict[str, Any]] = []
        raw_checks = raw.get("checks")
        for chk in raw_checks if isinstance(raw_checks, list) else []:
            if not isinstance(chk, dict):
                continue
            detail = _opt_str(chk.get("detail"))
            checks.append(
                VerificationCheckResponse(
                    check_id=_opt_str(chk.get("check_id")),
                    description=_opt_str(chk.get("description")),
                    result=_opt_str(chk.get("result")),
                    # ``reference`` is the traceability contract and is emitted
                    # verbatim (incident-relative, e.g. "service/app.py:42").
                    reference=_opt_str(chk.get("reference")),
                    # ``detail`` is free-form tool diagnostics, so it is
                    # redacted for absolute filesystem paths like stage errors.
                    detail=sanitize_error_message(detail) if detail else None,
                    evidence_ids=_str_list(chk.get("evidence")),
                ).model_dump()
            )

        projected.append(
            VerificationResultResponse(
                hypothesis_id=hypothesis_id,
                verdict=verdict,
                confidence=_opt_float(raw.get("confidence")),
                reasoning=_opt_str(raw.get("reasoning")),
                checks=checks,
            ).model_dump()
        )
    return projected


def _project_hypotheses(
    state: InvestigationState,
    verdict_by_hypothesis: Dict[str, str],
) -> List[Dict[str, Any]]:
    """Project persisted hypotheses, joining in any known verdict."""
    projected: List[Dict[str, Any]] = []
    for hyp in getattr(state, "hypotheses", None) or []:
        hypothesis_id = _opt_str(getattr(hyp, "hypothesis_id", None))
        if not hypothesis_id:
            continue
        projected.append(
            HypothesisResultResponse(
                hypothesis_id=hypothesis_id,
                claim=_opt_str(getattr(hyp, "claim", None)) or "",
                evidence_ids=_str_list(getattr(hyp, "evidence_ids", None)),
                verdict=verdict_by_hypothesis.get(hypothesis_id),
                supporting_reasoning=_opt_str(getattr(hyp, "supporting_reasoning", None)),
                falsification_criteria=_str_list(getattr(hyp, "falsification_criteria", None)),
                verification_plan=_str_list(getattr(hyp, "verification_plan", None)),
            ).model_dump()
        )
    return projected


def _project_proposals(state: InvestigationState) -> List[Dict[str, Any]]:
    """Project persisted fix proposals onto the allowlisted proposal shape."""
    projected: List[Dict[str, Any]] = []
    for prop in getattr(state, "proposals", None) or []:
        proposal_id = _opt_str(getattr(prop, "proposal_id", None))
        if not proposal_id:
            continue

        changes: List[Dict[str, Any]] = []
        raw_changes = getattr(prop, "changes", None)
        for change in raw_changes if isinstance(raw_changes, list) else []:
            file_path = _opt_str(getattr(change, "file", None))
            if not file_path:
                continue
            changes.append(
                FileChangeResponse(
                    file=file_path,
                    description=_opt_str(getattr(change, "description", None)),
                    start_line=_opt_int(getattr(change, "start_line", None)),
                    end_line=_opt_int(getattr(change, "end_line", None)),
                ).model_dump()
            )

        projected.append(
            ProposalResultResponse(
                proposal_id=proposal_id,
                hypothesis_id=_opt_str(getattr(prop, "hypothesis_id", None)),
                status=_opt_str(getattr(prop, "status", None)),
                summary=_opt_str(getattr(prop, "summary", None)),
                rationale=_opt_str(getattr(prop, "rationale", None)),
                patch=_opt_str(getattr(prop, "patch", None)),
                expected_effect=_opt_str(getattr(prop, "expected_effect", None)),
                risks=_str_list(getattr(prop, "risks", None)),
                validation_plan=_str_list(getattr(prop, "validation_plan", None)),
                rollback_plan=_opt_str(getattr(prop, "rollback_plan", None)),
                evidence_ids=_str_list(getattr(prop, "evidence_ids", None)),
                changes=changes,
                human_approval_notice=_opt_str(getattr(prop, "human_approval_notice", None)),
            ).model_dump()
        )
    return projected


def _project_investigation_result_response(
    state: InvestigationState,
    status_val: str,
) -> InvestigationResultResponse:
    """Build the allowlisted investigation-result response for a persisted state."""
    verification_projected = _project_verification(state)
    verdict_by_hypothesis = {
        v["hypothesis_id"]: v["verdict"]
        for v in verification_projected
        if v.get("hypothesis_id") and v.get("verdict")
    }
    hypotheses_projected = _project_hypotheses(state, verdict_by_hypothesis)
    evidence_projected = _project_evidence(state)
    proposals_projected = _project_proposals(state)

    return InvestigationResultResponse(
        investigation_id=state.incident_id,
        status=status_val,
        current_stage=getattr(state, "current_stage", None),
        completed_at=getattr(state, "completed_at", None),
        result_available=bool(
            hypotheses_projected
            or evidence_projected
            or verification_projected
            or proposals_projected
        ),
        hypotheses=hypotheses_projected,
        evidence=evidence_projected,
        verification=verification_projected,
        proposals=proposals_projected,
    )


# ---------------------------------------------------------------------------
# Route Handlers (Synchronous - offloaded to AnyIO thread pool by Starlette)
# ---------------------------------------------------------------------------

def health(request: Request) -> JSONResponse:
    """GET /health - Liveness probe."""
    resp = HealthResponse()
    return JSONResponse(status_code=200, content=resp.model_dump())


def create_investigation(request: Request) -> JSONResponse:
    """POST /investigations - Run an incident investigation.

    Default: synchronous execution (200 OK with full result).
    Opt-in async: send ``Prefer: respond-async`` header for 202 Accepted.
    """
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

    # Mission 18 preflight: an invalid bundle is rejected before any state is
    # created, any lease is acquired, or any LLM-backed stage runs — for both
    # the synchronous and the asynchronous dispatch paths.
    # InvalidIncidentBundleError is mapped to 400 invalid_incident_bundle by the
    # exception handler registered in api/app.py.
    require_valid_incident_bundle(candidate_path, expected_id=incident_id)

    repository = getattr(request.app.state, "repository", None)
    llm_client = getattr(request.app.state, "llm_client", None)

    # --- Async opt-in: Prefer: respond-async or ?async=true ---
    prefer_header = request.headers.get("prefer", "").strip().lower()
    async_param = request.query_params.get("async", "").strip().lower()
    is_async = prefer_header == PREFER_ASYNC_HEADER_VALUE or async_param in ("true", "1")

    if is_async:
        # Claim process-local slot and validate no existing persisted state
        _claim_investigation(incident_id, repository, allow_resume=False)
        # Persist initial RUNNING state with durable lease BEFORE returning 202
        # so the investigation is immediately queryable and leased under the same worker_id.
        import uuid as _uuid
        worker_id = f"worker-{_uuid.uuid4().hex[:8]}"
        try:
            initial_state = InvestigationState(incident_id=incident_id)
            initial_state.acquire_lease(owner_id=worker_id, ttl_seconds=300)
            if repository is not None:
                repository.save(initial_state)
        except ConcurrencyError as ce:
            _release_investigation(incident_id)
            logger.warning("OCC conflict during async create for '%s': %s", incident_id, ce)
            return error_json_response(409, "conflict", str(ce))
        except PersistenceError as pe:
            _release_investigation(incident_id)
            logger.error("Persistence error during async create for '%s': %s", incident_id, pe)
            return error_json_response(503, "persistence_unavailable", str(pe))
        except Exception as setup_exc:
            _release_investigation(incident_id)
            logger.error("Async setup failed for '%s': %s", incident_id, setup_exc)
            return error_json_response(
                500,
                "internal_error",
                "Failed to initialize async investigation state.",
            )
        # Schedule the real pipeline as a background task with the exact same worker_id.
        # _run_investigation_background releases the process-local claim in its finally.
        bg = BackgroundTask(
            _run_investigation_background,
            incident_id,
            candidate_path,
            repository,
            llm_client,
            worker_id,
        )
        return _build_accepted_response(incident_id, request, background=bg)

    # --- Default: synchronous execution ---
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
    """POST /investigations/{investigation_id}/resume - Resume an interrupted/failed investigation.

    Default: synchronous execution (200 OK with full result).
    Opt-in async: send ``Prefer: respond-async`` header for 202 Accepted.
    """
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

    # Mission 18 preflight: validate the bundle before any lease is acquired or
    # any LLM-backed stage runs, on both the synchronous and asynchronous paths.
    require_valid_incident_bundle(candidate_path, expected_id=safe_id)

    repository = getattr(request.app.state, "repository", None)
    if repository is None:
        return error_json_response(503, "persistence_unavailable", "Persistence repository is required for resume.")

    llm_client = getattr(request.app.state, "llm_client", None)

    # --- Async opt-in: Prefer: respond-async or ?async=true ---
    prefer_header = request.headers.get("prefer", "").strip().lower()
    async_param = request.query_params.get("async", "").strip().lower()
    is_async = prefer_header == PREFER_ASYNC_HEADER_VALUE or async_param in ("true", "1")

    if is_async:
        # Validate resumability (404, 409 for completed/active lease) before
        # returning 202 — this ensures error semantics are identical to sync.
        _claim_investigation(safe_id, repository, allow_resume=True)
        # Acquire durable execution ownership in repository BEFORE returning 202
        import uuid as _uuid
        worker_id = f"worker-{_uuid.uuid4().hex[:8]}"
        try:
            existing_state = repository.load(safe_id)
            if existing_state is None:
                _release_investigation(safe_id)
                return error_json_response(404, "investigation_not_found", f"Investigation '{safe_id}' not found.")

            if not existing_state.acquire_lease(owner_id=worker_id, ttl_seconds=300):
                lease_owner = getattr(existing_state, "lease_owner", None) or "another worker"
                _release_investigation(safe_id)
                return error_json_response(
                    409,
                    "conflict",
                    f"Investigation '{safe_id}' has an active lease held by '{lease_owner}'.",
                )

            # Record original status for telemetry before transitioning to RUNNING
            orig_status = (
                existing_state.status.value
                if hasattr(existing_state.status, "value")
                else str(existing_state.status)
            )
            existing_state.metadata["resumed_from_status"] = orig_status
            existing_state.current_stage = None
            existing_state.status = IncidentStatus.RUNNING
            existing_state.error = None

            # Persist durable lease under OCC protection
            repository.save(existing_state)
        except ConcurrencyError as ce:
            _release_investigation(safe_id)
            logger.warning("OCC conflict during async resume for '%s': %s", safe_id, ce)
            return error_json_response(409, "conflict", str(ce))
        except PersistenceError as pe:
            _release_investigation(safe_id)
            logger.error("Persistence error during async resume for '%s': %s", safe_id, pe)
            return error_json_response(503, "persistence_unavailable", str(pe))
        except Exception as exc:
            _release_investigation(safe_id)
            logger.exception("Unexpected error acquiring lease for async resume of '%s': %s", safe_id, exc)
            return error_json_response(500, "internal_error", "Failed to acquire execution ownership for resume.")

        # _claim_investigation registered the slot; background task will release it.
        bg = BackgroundTask(
            _run_resume_background,
            safe_id,
            candidate_path,
            repository,
            llm_client,
            worker_id,
        )
        return _build_accepted_response(safe_id, request, background=bg)

    # --- Default: synchronous execution ---
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

    if state.is_lease_active():
        raise HTTPException(
            status_code=409,
            detail="Cannot record approval while investigation is actively running under an active lease.",
        )

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
        lease_owner=getattr(state, "lease_owner", None),
        lease_expires_at=getattr(state, "lease_expires_at", None),
        stages=stages_projected,
        approvals=approvals_projected,
    )
    return JSONResponse(status_code=200, content=resp_model.model_dump())


def get_investigation_result(request: Request) -> JSONResponse:
    """GET /investigations/{investigation_id}/result - Retrieve the investigation diagnosis.

    Exposes the evidence, hypotheses, verification checks and fix proposals
    that are already persisted in the investigation state.  Sections that have
    not been produced yet (e.g. an investigation still in progress) are
    returned as empty lists rather than triggering an error.
    """
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

    resp_model = _project_investigation_result_response(state, status_val)
    return JSONResponse(status_code=200, content=resp_model.model_dump())


def list_investigations(request: Request) -> JSONResponse:
    """GET /investigations - List all stored investigation IDs."""
    repository = getattr(request.app.state, "repository", None)
    if repository is None:
        return error_json_response(503, "persistence_unavailable", "Persistence repository is not configured.")

    ids = repository.list()
    resp_model = ListInvestigationsResponse(investigations=ids)
    return JSONResponse(status_code=200, content=resp_model.model_dump())
async def _stream_investigation_events(
    request: Request,
    safe_id: str,
    repository: Any,
    poll_interval: float = 0.5,
    keepalive_interval: float = 15.0,
    page_size: int = 200,
):
    """Async generator streaming durable outbox events as Server-Sent Events (SSE).

    Conforms to W3C Server-Sent Events standard and handles:
    - Keyset pagination over canonical order (created_at ASC, event_id ASC).
    - Cursor resolution from Last-Event-ID header (or last_event_id query param)
      by paging bounded chunks against stored history.
    - Delivery of historical events and live-polling in bounded pages without
      loading unbounded history into memory.
    - Periodic keep-alive comments (: keep-alive\\n\\n).
    - Draining all persisted events prior to clean closure on terminal states,
      closing only when a subsequent query confirms there are no remaining events.
    - Honest behaviour when event history is unsupported by the persistence backend.
    - Graceful cleanup on client disconnection / cancellation.
    """
    backend_supports = getattr(repository, "supports_event_history", True)
    if not backend_supports:
        notice_data = {
            "error": "event_history_unsupported",
            "detail": "Active persistence backend does not support durable event history.",
            "backend_supports_event_history": False,
        }
        yield _format_sse_event(event_type="notice", data=notice_data)
        return

    # Extract cursor from header or query parameter
    last_event_id = request.headers.get("last-event-id") or request.query_params.get("last_event_id")

    # Allow application state to configure intervals and page size dynamically
    poll_interval = float(getattr(request.app.state, "sse_poll_interval", poll_interval))
    keepalive_interval = float(getattr(request.app.state, "sse_keepalive_interval", keepalive_interval))
    page_size = int(getattr(request.app.state, "sse_page_size", page_size))

    def event_key(ev: Any) -> Tuple[str, str]:
        c_at = getattr(ev, "created_at", None)
        c_at_str = c_at.isoformat() if hasattr(c_at, "isoformat") else str(c_at or "")
        return (c_at_str, str(getattr(ev, "event_id", "") or ""))

    def project_sse(ev: Any) -> str:
        raw_payload = ev.payload if isinstance(ev.payload, dict) else {}
        safe_payload = _project_event_payload(raw_payload)
        timestamp = safe_payload.get("timestamp") or ev.created_at or None
        data = {
            "event_id": ev.event_id,
            "investigation_id": ev.investigation_id,
            "event_type": ev.event_type,
            "timestamp": timestamp,
            "created_at": ev.created_at,
            "processed_at": ev.processed_at,
            "status": ev.status,
            "retry_count": ev.retry_count,
            "error_message": ev.error_message,
            "payload": safe_payload,
        }
        return _format_sse_event(event_type=ev.event_type, data=data, event_id=ev.event_id)

    last_cursor: Optional[Tuple[str, str]] = None

    if last_event_id:
        # Search stored history page-by-page to resolve the cursor event
        search_cursor: Optional[Tuple[str, str]] = None
        cursor_found = False
        while True:
            try:
                batch = repository.list_events(safe_id, limit=page_size, after_cursor=search_cursor)
            except Exception as exc:
                logger.warning("Error resolving cursor for investigation %s: %s", safe_id, exc)
                yield _format_sse_event(
                    event_type="error",
                    data={"error": "event_history_error", "detail": "Failed to resolve event cursor."},
                )
                return

            if not batch:
                break

            match = next((ev for ev in batch if ev.event_id == last_event_id), None)
            if match is not None:
                cursor_found = True
                last_cursor = event_key(match)
                break

            search_cursor = event_key(batch[-1])
            if len(batch) < page_size:
                break

        if not cursor_found:
            # Cursor could not be resolved in stored history.
            # Emit explicit notice and deliver from beginning so no events are lost.
            yield f": notice: cursor '{last_event_id}' could not be resolved; delivering from beginning\n\n"
            last_cursor = None

    last_keepalive = time.monotonic()
    max_polls = getattr(request.app.state, "sse_max_polls", None)
    poll_count = 0
    first_fetch = True

    try:
        while True:
            # 1. Detect client disconnect
            if await request.is_disconnected():
                break

            # 2. Query next bounded page strictly after last_cursor
            query_succeeded = False
            try:
                events_page = repository.list_events(safe_id, limit=page_size, after_cursor=last_cursor)
                query_succeeded = True
            except Exception as exc:
                logger.warning("Error fetching events for investigation %s: %s", safe_id, exc)
                if first_fetch:
                    yield _format_sse_event(
                        event_type="error",
                        data={"error": "event_history_error", "detail": "Failed to retrieve initial event history."},
                    )
                    return
                events_page = []
            first_fetch = False

            # 3. Emit each event in canonical order and advance cursor
            for ev in events_page:
                yield project_sse(ev)
                last_cursor = event_key(ev)

            # 4. Check current investigation status for terminal states
            state = repository.load(safe_id)
            is_terminal = False
            if state is not None:
                st = state.status.value if hasattr(state.status, "value") else str(state.status)
                if st in ("COMPLETED", "FAILED", "PARTIAL"):
                    is_terminal = True

            # 5. If this page was full, immediately continue to fetch subsequent pages
            if len(events_page) == page_size:
                continue

            # 6. Terminal stream handling:
            # If investigation is terminal and this query confirmed 0 remaining events, close cleanly.
            if is_terminal and query_succeeded and len(events_page) == 0:
                break

            # If investigation is terminal but events were delivered, immediately loop to confirm empty.
            if is_terminal and len(events_page) > 0:
                continue

            # 7. Check max_polls limit (used in tests for non-terminal streams)
            poll_count += 1
            if max_polls is not None and poll_count > max_polls:
                break

            # 8. Periodic keep-alive comments
            now = time.monotonic()
            if now - last_keepalive >= keepalive_interval:
                yield ": keep-alive\n\n"
                last_keepalive = now

            # 9. Sleep between polling cycles (releasing DB connection)
            await asyncio.sleep(poll_interval)

    except (asyncio.CancelledError, GeneratorExit):
        return


def list_investigation_events(request: Request) -> Union[JSONResponse, StreamingResponse]:
    """GET /investigations/{investigation_id}/events - Return event history or live stream.

    Supports HTTP content negotiation:
    - Accept: text/event-stream -> Returns Server-Sent Events (SSE) live stream
    - Accept: application/json (or default) -> Returns JSON snapshot (EventHistoryResponse)

    When the active backend does not durably store outbox events (e.g.
    FilesystemRepository):
    - JSON: returns an empty ``events`` list with ``backend_supports_event_history: false``
    - SSE: streams an informative notice event and closes cleanly
    """
    raw_id = request.path_params.get("investigation_id", "")
    try:
        safe_id = _validate_id_param(raw_id)
    except HTTPException as he:
        return error_json_response(he.status_code, "invalid_investigation_id", str(he.detail))

    repository = getattr(request.app.state, "repository", None)
    if repository is None:
        return error_json_response(
            503,
            "persistence_unavailable",
            "Persistence repository is not configured.",
        )

    # Confirm the investigation exists before returning (potentially empty)
    # event history or starting SSE stream so callers receive 404 on missing investigations.
    if not repository.exists(safe_id):
        return error_json_response(
            404,
            "investigation_not_found",
            f"Investigation '{safe_id}' not found.",
        )

    accept_header = request.headers.get("accept", "").lower()
    if "text/event-stream" in accept_header:
        poll_interval = float(getattr(request.app.state, "sse_poll_interval", 0.5))
        keepalive_interval = float(getattr(request.app.state, "sse_keepalive_interval", 15.0))
        page_size = int(getattr(request.app.state, "sse_page_size", 200))
        return StreamingResponse(
            _stream_investigation_events(
                request=request,
                safe_id=safe_id,
                repository=repository,
                poll_interval=poll_interval,
                keepalive_interval=keepalive_interval,
                page_size=page_size,
            ),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    # JSON Snapshot Response (Mission 12B)
    backend_supports = getattr(repository, "supports_event_history", True)

    try:
        outbox_events = repository.list_events(safe_id)
    except Exception as exc:
        from core.persistence import PersistenceError as PE
        if isinstance(exc, PE):
            raise
        return error_json_response(
            500,
            "event_history_error",
            "An error occurred retrieving event history.",
        )

    projected: List[Dict[str, Any]] = []
    for ev in outbox_events:
        raw_payload = ev.payload if isinstance(ev.payload, dict) else {}
        safe_payload = _project_event_payload(raw_payload)
        timestamp = safe_payload.get("timestamp") or ev.created_at or None
        projected.append(
            EventHistoryItemResponse(
                event_id=ev.event_id,
                investigation_id=ev.investigation_id,
                event_type=ev.event_type,
                timestamp=timestamp,
                created_at=ev.created_at,
                processed_at=ev.processed_at,
                status=ev.status,
                retry_count=ev.retry_count,
                error_message=ev.error_message,
                payload=safe_payload,
            ).model_dump()
        )

    resp = EventHistoryResponse(
        investigation_id=safe_id,
        total=len(projected),
        events=projected,
        backend_supports_event_history=bool(backend_supports),
    )
    return JSONResponse(status_code=200, content=resp.model_dump())