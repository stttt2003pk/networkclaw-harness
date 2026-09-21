import pytest

from networkclaw_harness.protocol import COMMANDS, EVENTS, InputFrame, ProtocolError, event_frame
from networkclaw_harness.host.scheduler import AdmissionScheduler, ScheduledCommand


def test_input_frame_accepts_compatible_extensions_and_preserves_cursor():
    frame = InputFrame.from_mapping({
        "protocol_version": "1.0", "type": "user.input", "request_id": "req-1",
        "tenant_id": "tenant-1", "user_id": "user-1", "session_id": "session-1",
        "turn_id": "turn-1", "run_id": "run-1", "event_id": "event-1",
        "invocation_id": "invocation-1", "parent_item_id": "item-1",
        "interaction_id": "interaction-1", "artifact_id": "artifact-1",
        "cursor": "cursor-42", "trace_id": "trace-1", "deadline": "2026-09-17T00:00:00Z",
        "metadata": {"relay": "chatsvc"}, "payload": {"text": "hello"},
    })
    assert frame.cursor == "cursor-42"
    assert frame.metadata == {"relay": "chatsvc"}


def test_input_frame_rejects_unknown_version_and_non_ascii_identity():
    with pytest.raises(ProtocolError, match="supported versions"):
        InputFrame.from_mapping({"protocol_version": "2.0", "type": "health.query", "request_id": "req-2"})
    with pytest.raises(ProtocolError, match="ASCII"):
        InputFrame.from_mapping({"protocol_version": "1.0", "type": "health.query", "request_id": "请求"})
    with pytest.raises(ProtocolError, match="unknown input fields"):
        InputFrame.from_mapping({"protocol_version": "1.0", "type": "health.query", "request_id": "req", "sequence": 9})


def test_event_envelope_keeps_transport_sequence_separate_from_cursor():
    event = event_frame("artifact.created", sequence=1, request_id="req", cursor="177", event_id="event-1", payload={"summary": "result"}, end=False)
    assert event["sequence"] == 1
    assert event["cursor"] == "177"
    assert event["event_id"] == "event-1"
    assert event["end"] is False


def test_catalog_freezes_all_v1_command_and_event_families():
    assert {
        "session.open", "session.resume", "session.close", "session.lease.update",
        "user.input", "turn.steer", "turn.cancel", "clarification.answer",
        "approval.resolve", "health.query", "capabilities.query", "shutdown",
    } <= COMMANDS
    assert {
        "assistant.delta", "plan.updated", "tool.started", "tool.progress",
        "subagent.started", "artifact.created", "clarification.requested",
        "approval.requested", "warning", "error", "heartbeat", "end",
    } <= EVENTS


def test_scheduler_prioritizes_controls_and_fairly_round_robins_sessions():
    scheduler = AdmissionScheduler(per_session_limit=2)
    scheduler.submit(ScheduledCommand("s1", "normal-1", None))
    scheduler.submit(ScheduledCommand("s1", "normal-2", None))
    scheduler.submit(ScheduledCommand("s2", "normal-3", None))
    scheduler.submit(ScheduledCommand("s1", "control", None), control=True)
    assert scheduler.next().request_id == "control"
    assert scheduler.next().request_id == "normal-1"
    assert scheduler.next().request_id == "normal-3"
    assert scheduler.next().request_id == "normal-2"


def test_scheduler_rejects_unbounded_session_queue():
    scheduler = AdmissionScheduler(per_session_limit=1)
    scheduler.submit(ScheduledCommand("s1", "first", None))
    with pytest.raises(ProtocolError, match="queue is full"):
        scheduler.submit(ScheduledCommand("s1", "second", None))
