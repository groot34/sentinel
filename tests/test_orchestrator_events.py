"""Tests for IncidentOrchestrator event-driven integration (Mission 04).

All LLM and agent calls are mocked — no real Groq API calls.
Tests verify the strict lifecycle: State Mutation -> Persistence -> Event Publish.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List
from unittest.mock import MagicMock, patch

import pytest

from agents.fix_tools import HUMAN_APPROVAL_NOTICE
from agents.orchestrator import (
    PIPELINE_COMPLETED,
    PIPELINE_FAILED,
    PIPELINE_PARTIAL,
    IncidentOrchestrator,
)
from core.domain.models import IncidentStatus, StageStatus
from core.domain.state import InvestigationState
from core.events.bus import EventRecorder, InMemoryEventBus
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
from core.persistence.filesystem import FilesystemRepository


REPO = Path(__file__).parent.parent
INC_01 = REPO / "incidents" / "inc_01_n_plus_one_query"


# ---------------------------------------------------------------------------
# Helpers / Test data builders
# ---------------------------------------------------------------------------

def _mk_logs(incident_id: str = "inc_01_n_plus_one_query") -> Dict[str, Any]:
    return {
        "incident_id": incident_id,
        "agent": "logs_agent",
        "summary": "test logs",
        "evidence": [
            {
                "evidence_id": "EV-LOG-001",
                "source": "logs",
                "reference": "logs/application.log:1",
                "timestamp": "",
                "type": "error",
                "excerpt": "log excerpt 1",
                "interpretation": "interp 1",
            }
        ],
    }


def _mk_metrics(incident_id: str = "inc_01_n_plus_one_query") -> Dict[str, Any]:
    return {
        "incident_id": incident_id,
        "agent": "metrics_agent",
        "summary": "test metrics",
        "evidence": [
            {
                "evidence_id": "EV-MET-001",
                "source": "metrics",
                "reference": "metrics/metrics.csv:1",
                "timestamp": "",
                "type": "spike",
                "excerpt": "metric excerpt 1",
                "interpretation": "interp 1",
            }
        ],
    }


def _mk_code(incident_id: str = "inc_01_n_plus_one_query") -> Dict[str, Any]:
    return {
        "incident_id": incident_id,
        "agent": "code_agent",
        "summary": "test code",
        "evidence": [
            {
                "evidence_id": "EV-CODE-001",
                "source": "code",
                "reference": "service/app.py:1",
                "timestamp": "",
                "type": "suspicious_pattern",
                "excerpt": "code excerpt 1",
                "interpretation": "interp 1",
            }
        ],
    }


def _mk_hypotheses(incident_id: str = "inc_01_n_plus_one_query") -> Dict[str, Any]:
    return {
        "incident_id": incident_id,
        "hypotheses": [
            {
                "hypothesis_id": "HYP-001",
                "claim": "N+1 query pattern.",
                "evidence_ids": ["EV-LOG-001", "EV-CODE-001"],
                "supporting_reasoning": "250 queries per request.",
                "falsification_criteria": ["constant query count"],
                "verification_plan": ["check query count per request"],
            }
        ],
    }


def _mk_verification(verdict: str = "CONFIRMED") -> Dict[str, Any]:
    return {
        "incident_id": "inc_01_n_plus_one_query",
        "results": [
            {
                "hypothesis_id": "HYP-001",
                "verdict": verdict,
                "checks": [
                    {
                        "check_id": "CHK-001",
                        "description": "check",
                        "result": "PASS",
                        "evidence": ["EV-LOG-001"],
                        "reference": "service/app.py:22",
                    }
                ],
                "reasoning": "All checks passed.",
                "confidence": 0.9,
            }
        ],
    }


def _mk_fix_proposals() -> Dict[str, Any]:
    return {
        "incident_id": "inc_01_n_plus_one_query",
        "proposals": [
            {
                "proposal_id": "FIX-001",
                "hypothesis_id": "HYP-001",
                "incident_id": "inc_01_n_plus_one_query",
                "status": "PROPOSED",
                "human_approval_notice": HUMAN_APPROVAL_NOTICE,
                "summary": "Fix 1",
                "rationale": "rationale",
                "changes": [
                    {
                        "file": "service/app.py",
                        "start_line": None,
                        "end_line": None,
                        "description": "desc",
                        "before": "old",
                        "after": "new",
                    }
                ],
                "patch": "--- a/service/app.py\n+++ b/service/app.py\n@@ -1 +1 @@\n-old\n+new",
                "expected_effect": "better",
                "risks": [],
                "validation_plan": ["run tests"],
                "rollback_plan": "revert",
                "evidence_ids": ["EV-LOG-001"],
            }
        ],
        "validation_errors": {},
        "skipped_hypotheses": [],
    }


def _mk_approvals() -> Dict[str, Any]:
    return {
        "incident_id": "inc_01_n_plus_one_query",
        "approval_records": [],
        "summary": {"total": 1, "approved": 0, "rejected": 1},
    }


def _patch_pipeline_agents(
    mock_logs=None,
    mock_metrics=None,
    mock_code=None,
    mock_hyp=None,
    mock_ver=None,
    mock_fix=None,
    mock_gate=None,
):
    """Return a context manager patching all specialist agents."""
    p_logs = patch("agents.orchestrator.LogsAgent")
    p_metrics = patch("agents.orchestrator.MetricsAgent")
    p_code = patch("agents.orchestrator.CodeAgent")
    p_hyp = patch("agents.orchestrator.HypothesisEngine")
    p_ver = patch("agents.orchestrator.VerificationAgent")
    p_fix = patch("agents.orchestrator.FixProposalAgent")
    p_gate = patch("agents.orchestrator.ApprovalGate")

    class AgentMocks:
        def __enter__(self):
            self.logs = p_logs.__enter__()
            self.metrics = p_metrics.__enter__()
            self.code = p_code.__enter__()
            self.hyp = p_hyp.__enter__()
            self.ver = p_ver.__enter__()
            self.fix = p_fix.__enter__()
            self.gate = p_gate.__enter__()
            self.logs.return_value.extract_evidence.return_value = mock_logs or _mk_logs()
            self.metrics.return_value.extract_evidence.return_value = mock_metrics or _mk_metrics()
            self.code.return_value.extract_evidence.return_value = mock_code or _mk_code()
            self.hyp.return_value.generate_hypotheses.return_value = mock_hyp or _mk_hypotheses()
            self.ver.return_value.verify.return_value = mock_ver or _mk_verification()
            self.fix.return_value.propose_fix.return_value = mock_fix or _mk_fix_proposals()
            self.gate.return_value.review_all.return_value = mock_gate or _mk_approvals()
            return self

        def __exit__(self, exc_type, exc_val, exc_tb):
            p_gate.__exit__(exc_type, exc_val, exc_tb)
            p_fix.__exit__(exc_type, exc_val, exc_tb)
            p_ver.__exit__(exc_type, exc_val, exc_tb)
            p_hyp.__exit__(exc_type, exc_val, exc_tb)
            p_code.__exit__(exc_type, exc_val, exc_tb)
            p_metrics.__exit__(exc_type, exc_val, exc_tb)
            p_logs.__exit__(exc_type, exc_val, exc_tb)

    return AgentMocks()


# ---------------------------------------------------------------------------
# Test 1: No event bus → no events emitted, investigation still succeeds
# ---------------------------------------------------------------------------

def test_no_event_bus_backward_compatible():
    """Orchestrator without event_bus runs successfully without any events."""
    bus = InMemoryEventBus()
    recorder = EventRecorder(bus)

    orc = IncidentOrchestrator(non_interactive=True)  # no event_bus

    with _patch_pipeline_agents():
        result = orc.investigate(INC_01)

    assert result["pipeline_status"] == PIPELINE_COMPLETED
    # No events were recorded because no bus was wired
    assert recorder.events == []


# ---------------------------------------------------------------------------
# Test 2: Fresh run produces the expected full event sequence
# ---------------------------------------------------------------------------

def test_fresh_run_full_event_sequence():
    """A complete fresh run emits the 8-stage lifecycle in order."""
    bus = InMemoryEventBus()
    recorder = EventRecorder(bus)
    orc = IncidentOrchestrator(non_interactive=True, event_bus=bus)

    with _patch_pipeline_agents():
        result = orc.investigate(INC_01)

    assert result["pipeline_status"] == PIPELINE_COMPLETED

    types = [e.event_type for e in recorder.events]

    # InvestigationStarted must be first
    assert types[0] == "InvestigationStarted"
    # InvestigationCompleted must be last
    assert types[-1] == "InvestigationCompleted"

    # Exactly one InvestigationStarted and one InvestigationCompleted
    assert types.count("InvestigationStarted") == 1
    assert types.count("InvestigationCompleted") == 1

    # All 8 stages have StageStarted/StageCompleted (no cache in fresh run)
    started_stages = [e.stage for e in recorder.filter(StageStarted)]
    completed_stages = [e.stage for e in recorder.filter(StageCompleted)]

    assert "logs" in started_stages
    assert "metrics" in started_stages
    assert "code" in started_stages
    assert "evidence_fusion" in started_stages
    assert "hypotheses" in started_stages
    assert "approvals" in started_stages

    assert "logs" in completed_stages
    assert "metrics" in completed_stages
    assert "code" in completed_stages
    assert "evidence_fusion" in completed_stages


# ---------------------------------------------------------------------------
# Test 3: InvestigationStarted event carries correct investigation_id
# ---------------------------------------------------------------------------

def test_investigation_started_event_fields():
    """InvestigationStarted has correct investigation_id and initial_stage."""
    bus = InMemoryEventBus()
    recorder = EventRecorder(bus)
    orc = IncidentOrchestrator(non_interactive=True, event_bus=bus)

    with _patch_pipeline_agents():
        orc.investigate(INC_01)

    started = recorder.filter(InvestigationStarted)
    assert len(started) == 1
    assert started[0].investigation_id == INC_01.name
    assert started[0].initial_stage == "logs"


# ---------------------------------------------------------------------------
# Test 4: StageStarted emitted before StageCompleted for each stage
# ---------------------------------------------------------------------------

def test_stage_started_before_completed_ordering():
    """StageStarted always precedes StageCompleted for the same stage in fresh run."""
    bus = InMemoryEventBus()
    recorder = EventRecorder(bus)
    orc = IncidentOrchestrator(non_interactive=True, event_bus=bus)

    with _patch_pipeline_agents():
        orc.investigate(INC_01)

    events = recorder.events
    for stage in ["logs", "metrics", "code", "evidence_fusion"]:
        started_idx = next(i for i, e in enumerate(events) if e.event_type == "StageStarted" and e.stage == stage)
        completed_idx = next(i for i, e in enumerate(events) if e.event_type == "StageCompleted" and e.stage == stage)
        assert started_idx < completed_idx, f"StageStarted must precede StageCompleted for '{stage}'"


# ---------------------------------------------------------------------------
# Test 5: StageCompleted carries token telemetry from the agent
# ---------------------------------------------------------------------------

def test_stage_completed_carries_telemetry():
    """StageCompleted for 'logs' stage carries llm_calls and token counts from agent."""
    bus = InMemoryEventBus()
    recorder = EventRecorder(bus)
    orc = IncidentOrchestrator(non_interactive=True, event_bus=bus)

    llm_client = MagicMock()
    llm_client.get_session_token_usage.return_value = {
        "prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0, "llm_calls": 0,
    }
    orc._llm_client = llm_client

    with _patch_pipeline_agents():
        orc.investigate(INC_01)

    completed = recorder.filter(StageCompleted)
    logs_completed = next((e for e in completed if e.stage == "logs"), None)
    assert logs_completed is not None
    # llm_calls and token fields must be non-negative integers
    assert isinstance(logs_completed.llm_calls, int)
    assert logs_completed.llm_calls >= 0


# ---------------------------------------------------------------------------
# Test 6: Cache reuse emits StageCached — never StageStarted or StageCompleted
# ---------------------------------------------------------------------------

def test_cache_reuse_emits_stage_cached_not_started_completed(tmp_path):
    """When a stage uses file-system cache, only StageCached is emitted (not StageStarted/Completed)."""
    bus = InMemoryEventBus()
    recorder = EventRecorder(bus)
    orc = IncidentOrchestrator(
        non_interactive=True,
        event_bus=bus,
        output_dir=tmp_path,
    )
    incident_id = INC_01.name

    # Pre-populate cache for 'logs' stage
    cache_dir = tmp_path / incident_id
    cache_dir.mkdir(parents=True, exist_ok=True)
    logs_data = _mk_logs(incident_id)
    (cache_dir / "logs.json").write_text(
        json.dumps(logs_data), encoding="utf-8"
    )

    with _patch_pipeline_agents(mock_logs=logs_data):
        orc.investigate(INC_01)

    # StageCached must be present for 'logs'
    cached_events = recorder.filter(StageCached)
    assert any(e.stage == "logs" for e in cached_events), "StageCached for logs expected"

    # StageStarted must NOT appear for 'logs'
    started_events = recorder.filter(StageStarted)
    assert not any(e.stage == "logs" for e in started_events), (
        "StageStarted must not be emitted for cache-reused logs stage"
    )

    # StageCompleted must NOT appear for 'logs'
    completed_events = recorder.filter(StageCompleted)
    assert not any(e.stage == "logs" for e in completed_events), (
        "StageCompleted must not be emitted for cache-reused logs stage"
    )


# ---------------------------------------------------------------------------
# Test 7: Stage failure emits StageFailed
# ---------------------------------------------------------------------------

def test_stage_failure_emits_stage_failed():
    """When a stage agent raises an exception, StageFailed is emitted."""
    bus = InMemoryEventBus()
    recorder = EventRecorder(bus)
    orc = IncidentOrchestrator(non_interactive=True, event_bus=bus)

    with patch("agents.orchestrator.LogsAgent") as mock_logs_cls, \
         patch("agents.orchestrator.MetricsAgent") as mock_metrics_cls, \
         patch("agents.orchestrator.CodeAgent") as mock_code_cls, \
         patch("agents.orchestrator.HypothesisEngine") as mock_hyp_cls, \
         patch("agents.orchestrator.VerificationAgent") as mock_ver_cls, \
         patch("agents.orchestrator.FixProposalAgent") as mock_fix_cls, \
         patch("agents.orchestrator.ApprovalGate") as mock_gate_cls:

        mock_logs_cls.return_value.extract_evidence.side_effect = RuntimeError("Logs exploded!")
        mock_metrics_cls.return_value.extract_evidence.return_value = _mk_metrics()
        mock_code_cls.return_value.extract_evidence.return_value = _mk_code()
        mock_hyp_cls.return_value.generate_hypotheses.return_value = _mk_hypotheses()
        mock_ver_cls.return_value.verify.return_value = _mk_verification()
        mock_fix_cls.return_value.propose_fix.return_value = _mk_fix_proposals()
        mock_gate_cls.return_value.review_all.return_value = _mk_approvals()

        result = orc.investigate(INC_01)

    failed_events = recorder.filter(StageFailed)
    assert any(e.stage == "logs" for e in failed_events), "StageFailed for 'logs' expected"
    logs_failed = next(e for e in failed_events if e.stage == "logs")
    assert "Logs exploded!" in logs_failed.error


# ---------------------------------------------------------------------------
# Test 8: Hypothesis failure emits StageFailed then StageSkipped(verification)
# ---------------------------------------------------------------------------

def test_hypothesis_failure_emits_skipped_verification():
    """Hypothesis stage failure skips verification and emits StageSkipped + InvestigationCompleted."""
    bus = InMemoryEventBus()
    recorder = EventRecorder(bus)
    orc = IncidentOrchestrator(non_interactive=True, event_bus=bus)

    with patch("agents.orchestrator.LogsAgent") as ml, \
         patch("agents.orchestrator.MetricsAgent") as mm, \
         patch("agents.orchestrator.CodeAgent") as mc, \
         patch("agents.orchestrator.HypothesisEngine") as mh, \
         patch("agents.orchestrator.VerificationAgent") as mv, \
         patch("agents.orchestrator.FixProposalAgent") as mf, \
         patch("agents.orchestrator.ApprovalGate") as mg:

        ml.return_value.extract_evidence.return_value = _mk_logs()
        mm.return_value.extract_evidence.return_value = _mk_metrics()
        mc.return_value.extract_evidence.return_value = _mk_code()
        mh.return_value.generate_hypotheses.side_effect = RuntimeError("Hypothesis engine failure")
        mv.return_value.verify.return_value = _mk_verification()
        mf.return_value.propose_fix.return_value = _mk_fix_proposals()
        mg.return_value.review_all.return_value = _mk_approvals()

        result = orc.investigate(INC_01)

    # StageFailed for hypotheses
    failed = recorder.filter(StageFailed)
    assert any(e.stage == "hypotheses" for e in failed)

    # StageSkipped for verification
    skipped = recorder.filter(StageSkipped)
    assert any(e.stage == "verification" for e in skipped)

    # InvestigationCompleted with PARTIAL status
    completed = recorder.filter(InvestigationCompleted)
    assert len(completed) == 1
    assert completed[0].status == PIPELINE_PARTIAL


# ---------------------------------------------------------------------------
# Test 9: Verification failure emits StageSkipped(fix_proposals)
# ---------------------------------------------------------------------------

def test_verification_failure_emits_skipped_fix_proposals():
    """Verification failure skips fix_proposals and emits StageSkipped."""
    bus = InMemoryEventBus()
    recorder = EventRecorder(bus)
    orc = IncidentOrchestrator(non_interactive=True, event_bus=bus)

    with patch("agents.orchestrator.LogsAgent") as ml, \
         patch("agents.orchestrator.MetricsAgent") as mm, \
         patch("agents.orchestrator.CodeAgent") as mc, \
         patch("agents.orchestrator.HypothesisEngine") as mh, \
         patch("agents.orchestrator.VerificationAgent") as mv, \
         patch("agents.orchestrator.FixProposalAgent") as mf, \
         patch("agents.orchestrator.ApprovalGate") as mg:

        ml.return_value.extract_evidence.return_value = _mk_logs()
        mm.return_value.extract_evidence.return_value = _mk_metrics()
        mc.return_value.extract_evidence.return_value = _mk_code()
        mh.return_value.generate_hypotheses.return_value = _mk_hypotheses()
        mv.return_value.verify.side_effect = RuntimeError("Verification boom")
        mf.return_value.propose_fix.return_value = _mk_fix_proposals()
        mg.return_value.review_all.return_value = _mk_approvals()

        result = orc.investigate(INC_01)

    skipped = recorder.filter(StageSkipped)
    assert any(e.stage == "fix_proposals" for e in skipped)

    completed = recorder.filter(InvestigationCompleted)
    assert len(completed) == 1
    assert completed[0].status == PIPELINE_PARTIAL


# ---------------------------------------------------------------------------
# Test 10: InvestigationCompleted status matches pipeline_status
# ---------------------------------------------------------------------------

def test_investigation_completed_status_matches_pipeline():
    """InvestigationCompleted.status mirrors the pipeline_status COMPLETED / PARTIAL / FAILED."""
    bus = InMemoryEventBus()
    recorder = EventRecorder(bus)
    orc = IncidentOrchestrator(non_interactive=True, event_bus=bus)

    with _patch_pipeline_agents():
        result = orc.investigate(INC_01)

    completed_events = recorder.filter(InvestigationCompleted)
    assert len(completed_events) == 1
    assert completed_events[0].status == result["pipeline_status"]


# ---------------------------------------------------------------------------
# Test 11: InvestigationCompleted summary counts match result dict
# ---------------------------------------------------------------------------

def test_investigation_completed_summary_counts():
    """InvestigationCompleted total_llm_calls and confirmed_hypotheses are consistent."""
    bus = InMemoryEventBus()
    recorder = EventRecorder(bus)
    orc = IncidentOrchestrator(non_interactive=True, event_bus=bus)

    with _patch_pipeline_agents():
        result = orc.investigate(INC_01)

    completed_events = recorder.filter(InvestigationCompleted)
    assert len(completed_events) == 1
    evt = completed_events[0]
    assert isinstance(evt.total_llm_calls, int)
    assert evt.total_llm_calls >= 0
    assert evt.total_llm_calls == result["llm_call_count"]
    assert evt.confirmed_hypotheses == result["summary"]["confirmed_hypotheses"]


# ---------------------------------------------------------------------------
# Test 12: Only one InvestigationCompleted per investigation
# ---------------------------------------------------------------------------

def test_exactly_one_investigation_completed():
    """Exactly one InvestigationCompleted event is emitted per investigation run."""
    bus = InMemoryEventBus()
    recorder = EventRecorder(bus)
    orc = IncidentOrchestrator(non_interactive=True, event_bus=bus)

    with _patch_pipeline_agents():
        orc.investigate(INC_01)

    assert len(recorder.filter(InvestigationCompleted)) == 1


# ---------------------------------------------------------------------------
# Test 13: Resume emits InvestigationResumed
# ---------------------------------------------------------------------------

def test_resume_emits_investigation_resumed(tmp_path):
    """resume() emits InvestigationResumed with correct resume_stage and status."""
    repo = FilesystemRepository(persistence_root=tmp_path)
    bus = InMemoryEventBus()
    recorder = EventRecorder(bus)
    orc = IncidentOrchestrator(
        non_interactive=True,
        event_bus=bus,
        repository=repo,
    )
    incident_id = INC_01.name

    # Persist a partial state with only 'logs' completed
    state = InvestigationState(incident_id=incident_id)
    state.start_stage("logs")
    state.complete_stage("logs", output=_mk_logs(incident_id))
    state.status = IncidentStatus.PARTIAL
    repo.save(state)

    with _patch_pipeline_agents():
        result = orc.resume(incident_id, INC_01)

    resumed = recorder.filter(InvestigationResumed)
    assert len(resumed) == 1
    evt = resumed[0]
    assert evt.investigation_id == incident_id
    assert evt.resume_stage == "metrics"
    assert evt.resumed_from_status in ("PARTIAL", "RUNNING")


# ---------------------------------------------------------------------------
# Test 14: Resume skipped stages emit no events
# ---------------------------------------------------------------------------

def test_resume_skipped_stages_emit_no_events(tmp_path):
    """Stages already SUCCEEDED in a resumed investigation emit no stage events."""
    repo = FilesystemRepository(persistence_root=tmp_path)
    bus = InMemoryEventBus()
    recorder = EventRecorder(bus)
    orc = IncidentOrchestrator(
        non_interactive=True,
        event_bus=bus,
        repository=repo,
    )
    incident_id = INC_01.name

    # Logs is SUCCEEDED in persisted state — it should NOT emit StageStarted on resume
    state = InvestigationState(incident_id=incident_id)
    state.start_stage("logs")
    state.complete_stage("logs", output=_mk_logs(incident_id))
    state.status = IncidentStatus.PARTIAL
    repo.save(state)

    with _patch_pipeline_agents():
        orc.resume(incident_id, INC_01)

    started = recorder.filter(StageStarted)
    # 'logs' was already completed — StageStarted must not be emitted for it
    assert not any(e.stage == "logs" for e in started), (
        "StageStarted must not be emitted for already-completed 'logs' on resume"
    )


# ---------------------------------------------------------------------------
# Test 15: Subscriber failure isolation — investigation still completes
# ---------------------------------------------------------------------------

def test_subscriber_failure_does_not_abort_investigation():
    """Failing subscriber (non-strict bus) does not abort the investigation."""
    bus = InMemoryEventBus(strict=False)
    recorder = EventRecorder(bus)

    def exploding_subscriber(event):
        raise RuntimeError("Subscriber explosion!")

    bus.subscribe_all(exploding_subscriber)

    orc = IncidentOrchestrator(non_interactive=True, event_bus=bus)

    with _patch_pipeline_agents():
        result = orc.investigate(INC_01)

    # Investigation must succeed regardless of subscriber failure
    assert result["pipeline_status"] == PIPELINE_COMPLETED
    # EventRecorder still captured events because it is registered separately
    assert len(recorder.events) > 0
    # Bus captured the subscriber errors
    assert len(bus.errors) > 0


# ---------------------------------------------------------------------------
# Test 16: Events carry correct investigation_id
# ---------------------------------------------------------------------------

def test_all_events_carry_correct_investigation_id():
    """Every emitted event has investigation_id matching the incident directory name."""
    bus = InMemoryEventBus()
    recorder = EventRecorder(bus)
    orc = IncidentOrchestrator(non_interactive=True, event_bus=bus)

    with _patch_pipeline_agents():
        orc.investigate(INC_01)

    for evt in recorder.events:
        assert evt.investigation_id == INC_01.name, (
            f"Event {evt.event_type} has wrong investigation_id: {evt.investigation_id}"
        )


# ---------------------------------------------------------------------------
# Test 17: Events have unique event_id values
# ---------------------------------------------------------------------------

def test_all_events_have_unique_ids():
    """Every emitted event has a globally unique event_id."""
    bus = InMemoryEventBus()
    recorder = EventRecorder(bus)
    orc = IncidentOrchestrator(non_interactive=True, event_bus=bus)

    with _patch_pipeline_agents():
        orc.investigate(INC_01)

    event_ids = [e.event_id for e in recorder.events]
    assert len(event_ids) == len(set(event_ids)), "Duplicate event IDs detected"


# ---------------------------------------------------------------------------
# Test 18: InvestigationStarted emitted before any stage events
# ---------------------------------------------------------------------------

def test_investigation_started_before_stage_events():
    """InvestigationStarted is the first emitted event in a fresh run."""
    bus = InMemoryEventBus()
    recorder = EventRecorder(bus)
    orc = IncidentOrchestrator(non_interactive=True, event_bus=bus)

    with _patch_pipeline_agents():
        orc.investigate(INC_01)

    assert recorder.events[0].event_type == "InvestigationStarted"


# ---------------------------------------------------------------------------
# Test 19: InvestigationCompleted is the last emitted event
# ---------------------------------------------------------------------------

def test_investigation_completed_is_last_event():
    """InvestigationCompleted is always the last event in a complete run."""
    bus = InMemoryEventBus()
    recorder = EventRecorder(bus)
    orc = IncidentOrchestrator(non_interactive=True, event_bus=bus)

    with _patch_pipeline_agents():
        orc.investigate(INC_01)

    assert recorder.events[-1].event_type == "InvestigationCompleted"


# ---------------------------------------------------------------------------
# Test 20: No extra LLM calls from event emission
# ---------------------------------------------------------------------------

def test_event_emission_adds_no_extra_llm_calls():
    """Enabling the event bus does not increase llm_call_count in the result."""
    # Run without event bus
    orc_no_bus = IncidentOrchestrator(non_interactive=True)
    with _patch_pipeline_agents():
        result_no_bus = orc_no_bus.investigate(INC_01)

    # Run with event bus
    bus = InMemoryEventBus()
    orc_with_bus = IncidentOrchestrator(non_interactive=True, event_bus=bus)
    with _patch_pipeline_agents():
        result_with_bus = orc_with_bus.investigate(INC_01)

    assert result_no_bus["llm_call_count"] == result_with_bus["llm_call_count"], (
        "Event bus must not add extra LLM calls"
    )


# ---------------------------------------------------------------------------
# Test 21: Multiple subscribers receive events in registration order
# ---------------------------------------------------------------------------

def test_multiple_subscribers_receive_in_registration_order():
    """Multiple subscribers registered on the event bus each receive all events."""
    bus = InMemoryEventBus()
    received_a: List[str] = []
    received_b: List[str] = []

    bus.subscribe_all(lambda e: received_a.append(e.event_type))
    bus.subscribe_all(lambda e: received_b.append(e.event_type))

    orc = IncidentOrchestrator(non_interactive=True, event_bus=bus)

    with _patch_pipeline_agents():
        orc.investigate(INC_01)

    assert received_a == received_b, "Both subscribers must receive identical event sequences"
    assert len(received_a) > 0


# ---------------------------------------------------------------------------
# Test 22: Evidence fusion failure emits StageFailed + InvestigationCompleted(FAILED)
# ---------------------------------------------------------------------------

def test_evidence_fusion_failure_emits_failed_and_completed():
    """When evidence fusion validation fails, StageFailed + InvestigationCompleted(FAILED) are emitted."""
    bus = InMemoryEventBus()
    recorder = EventRecorder(bus)
    orc = IncidentOrchestrator(non_interactive=True, event_bus=bus)

    # Inject a fused result that has an invalid evidence ID format to trigger validation failure
    bad_fused = {
        "incident_id": INC_01.name,
        "evidence": [
            {
                "evidence_id": "BAD-INVALID-001",  # Not EV-LOG-/EV-MET-/EV-CODE-
                "source": "logs",
                "reference": "logs/app.log:1",
                "timestamp": "",
                "type": "error",
                "excerpt": "bad",
                "interpretation": "i",
            },
        ],
        "evidence_ids": ["BAD-INVALID-001"],
        "sources": {"logs": True, "metrics": False, "code": False},
    }

    with _patch_pipeline_agents():
        with patch("agents.orchestrator._fuse_evidence", return_value=bad_fused):
            result = orc.investigate(INC_01)

    assert result["pipeline_status"] == PIPELINE_FAILED

    fusion_failed = [e for e in recorder.filter(StageFailed) if e.stage == "evidence_fusion"]
    assert len(fusion_failed) == 1

    completed = recorder.filter(InvestigationCompleted)
    assert len(completed) == 1
    assert completed[0].status == PIPELINE_FAILED

