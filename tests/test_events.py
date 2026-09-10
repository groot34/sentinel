"""Unit tests for Sentinel 2.0 Domain Events and InMemoryEventBus."""

import pytest
from pydantic import ValidationError

from core.events.base import DomainEvent
from core.events.bus import EventBus, EventRecorder, InMemoryEventBus
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


def test_event_creation_and_type_correctness():
    """Events instantiate cleanly with correct event_type and defaults."""
    e1 = InvestigationStarted(investigation_id="inc_01", initial_stage="logs")
    assert e1.event_type == "InvestigationStarted"
    assert e1.investigation_id == "inc_01"
    assert e1.initial_stage == "logs"
    assert e1.event_id.startswith("evt_")
    assert e1.timestamp

    e2 = InvestigationResumed(investigation_id="inc_01", resume_stage="metrics", resumed_from_status="FAILED")
    assert e2.event_type == "InvestigationResumed"
    assert e2.resume_stage == "metrics"
    assert e2.resumed_from_status == "FAILED"

    e3 = StageStarted(investigation_id="inc_01", stage="logs")
    assert e3.event_type == "StageStarted"
    assert e3.stage == "logs"

    e4 = StageCompleted(investigation_id="inc_01", stage="logs", llm_calls=1, prompt_tokens=100, completion_tokens=50, total_tokens=150, item_count=2)
    assert e4.event_type == "StageCompleted"
    assert e4.llm_calls == 1
    assert e4.total_tokens == 150
    assert e4.item_count == 2

    e5 = StageFailed(investigation_id="inc_01", stage="metrics", error="Connection timeout")
    assert e5.event_type == "StageFailed"
    assert e5.error == "Connection timeout"

    e6 = StageCached(investigation_id="inc_01", stage="code")
    assert e6.event_type == "StageCached"
    assert e6.stage == "code"

    e7 = StageSkipped(investigation_id="inc_01", stage="hypotheses", reason="No evidence collected")
    assert e7.event_type == "StageSkipped"
    assert e7.reason == "No evidence collected"

    e8 = InvestigationCompleted(
        investigation_id="inc_01",
        status="COMPLETED",
        total_llm_calls=5,
        total_tokens=1000,
        confirmed_hypotheses=1,
        proposals_generated=1,
        proposals_approved=1,
    )
    assert e8.event_type == "InvestigationCompleted"
    assert e8.status == "COMPLETED"


def test_event_immutability():
    """Domain events are frozen; modifying attributes raises ValidationError."""
    event = StageStarted(investigation_id="inc_01", stage="logs")
    with pytest.raises(ValidationError):
        event.stage = "metrics"  # type: ignore


def test_extra_field_rejection():
    """Domain events forbid unknown extra fields."""
    with pytest.raises(ValidationError):
        StageStarted(investigation_id="inc_01", stage="logs", extra_bogus_field="malicious")  # type: ignore


def test_unique_event_ids():
    """Successive events have distinct UUID-based IDs."""
    e1 = StageStarted(investigation_id="inc_01", stage="logs")
    e2 = StageStarted(investigation_id="inc_01", stage="logs")
    assert e1.event_id != e2.event_id


def test_json_serialization():
    """Events serialize to dict and JSON round-trip seamlessly."""
    event = StageCompleted(
        investigation_id="inc_01",
        stage="logs",
        llm_calls=2,
        prompt_tokens=500,
        completion_tokens=200,
        total_tokens=700,
        item_count=3,
    )
    data = event.to_dict()
    assert data["event_type"] == "StageCompleted"
    assert data["investigation_id"] == "inc_01"
    assert data["total_tokens"] == 700

    reconstructed = StageCompleted.from_dict(data)
    assert reconstructed == event


def test_specific_subscription():
    """Subscribers registered for a specific event type only receive that event."""
    bus = InMemoryEventBus()
    received: list[DomainEvent] = []

    bus.subscribe(StageCompleted, lambda evt: received.append(evt))

    e_started = StageStarted(investigation_id="inc_01", stage="logs")
    e_completed = StageCompleted(investigation_id="inc_01", stage="logs", llm_calls=1)

    bus.publish(e_started)
    bus.publish(e_completed)

    assert len(received) == 1
    assert received[0] == e_completed


def test_global_subscription():
    """Global subscriber receives all emitted events in sequence."""
    bus = InMemoryEventBus()
    recorder = EventRecorder(bus)

    e1 = InvestigationStarted(investigation_id="inc_01")
    e2 = StageStarted(investigation_id="inc_01", stage="logs")
    e3 = StageCompleted(investigation_id="inc_01", stage="logs")

    bus.publish(e1)
    bus.publish(e2)
    bus.publish(e3)

    assert recorder.events == [e1, e2, e3]


def test_multiple_subscribers_and_registration_ordering():
    """Multiple subscribers execute in deterministic registration order."""
    bus = InMemoryEventBus()
    order: list[str] = []

    bus.subscribe_all(lambda evt: order.append("global_1"))
    bus.subscribe(StageStarted, lambda evt: order.append("specific_1"))
    bus.subscribe_all(lambda evt: order.append("global_2"))
    bus.subscribe(StageStarted, lambda evt: order.append("specific_2"))

    bus.publish(StageStarted(investigation_id="inc_01", stage="logs"))

    assert order == ["global_1", "specific_1", "global_2", "specific_2"]


def test_subscriber_exception_isolation():
    """In default non-strict mode, failing subscriber does not abort execution of remaining subscribers."""
    bus = InMemoryEventBus(strict=False)
    log: list[str] = []

    def failing_handler(evt):
        log.append("failing")
        raise RuntimeError("Subscriber explosion!")

    def working_handler(evt):
        log.append("working")

    bus.subscribe_all(failing_handler)
    bus.subscribe_all(working_handler)

    # Publishing must NOT raise
    bus.publish(StageStarted(investigation_id="inc_01", stage="logs"))

    assert log == ["failing", "working"]
    assert len(bus.errors) == 1
    assert "Subscriber explosion!" in str(bus.errors[0])


def test_strict_mode_propagates_exception():
    """In strict mode, subscriber exception is re-raised immediately."""
    bus = InMemoryEventBus(strict=True)

    def failing_handler(evt):
        raise ValueError("Strict mode error")

    bus.subscribe_all(failing_handler)

    with pytest.raises(ValueError, match="Strict mode error"):
        bus.publish(StageStarted(investigation_id="inc_01", stage="logs"))


def test_event_recorder_filter_and_clear():
    """EventRecorder filters by type and clears history."""
    bus = InMemoryEventBus()
    recorder = EventRecorder(bus)

    bus.publish(InvestigationStarted(investigation_id="inc_01"))
    bus.publish(StageStarted(investigation_id="inc_01", stage="logs"))
    bus.publish(StageCompleted(investigation_id="inc_01", stage="logs"))

    started_events = recorder.filter(StageStarted)
    assert len(started_events) == 1
    assert started_events[0].stage == "logs"

    recorder.clear()
    assert recorder.events == []
