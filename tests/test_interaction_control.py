from __future__ import annotations

import io
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from networkclaw_harness.host.interactions import InteractionHostAdapter
from networkclaw_harness.host.server import JsonlHost
from networkclaw_harness.runtime import (
    ApprovalDecision,
    CancelDisposition,
    DisconnectDisposition,
    FactKind,
    InputKind,
    InteractionController,
    InteractionError,
    ReferenceDurableSessionStore,
    RunStatus,
    RuntimeControlPolicy,
    SemanticFact,
    SteerUpdate,
    TimeoutDisposition,
)


NOW = datetime(2026, 9, 17, 10, 0, tzinfo=timezone.utc)


def controller(*, policy=RuntimeControlPolicy()):
    durable = ReferenceDurableSessionStore()
    controls = InteractionController(durable, policy)
    controls.register_run(
        tenant_id="tenant", user_id="user", session_id="session", run_id="run",
        runnable_items=("independent", "dependent"),
    )
    return controls, durable


def test_input_classification_is_explicit():
    classify = InteractionController.classify
    assert classify("user.input", active_run=False) is InputKind.NEW_GOAL
    assert classify("user.input", active_run=True) is InputKind.GOAL_SUPPLEMENT
    assert classify("turn.steer", active_run=True) is InputKind.STEER
    assert classify("clarification.answer", active_run=True) is InputKind.CLARIFICATION_ANSWER
    assert classify("approval.resolve", active_run=True) is InputKind.APPROVAL_RESOLUTION
    assert classify("turn.cancel", active_run=True) is InputKind.CANCEL


def test_new_goal_and_goal_supplement_inputs_are_durable_and_versioned():
    durable = ReferenceDurableSessionStore()
    controls = InteractionController(durable)
    created = controls.record_user_input(
        session_id="new-session", interaction_id="input-1",
        item_id="turn-1", text="new task",
    )
    controls.register_run(
        tenant_id="tenant", user_id="user", session_id="new-session", run_id="run",
    )
    supplemented = controls.record_user_input(
        session_id="new-session", run_id="run", interaction_id="input-2",
        item_id="turn-2", text="also keep it read only", expected_run_version=1,
    )
    duplicate = controls.record_user_input(
        session_id="new-session", run_id="run", interaction_id="input-2",
        item_id="turn-2", text="also keep it read only", expected_run_version=1,
    )
    assert created.run_status == "queued"
    assert supplemented.run_version == 2 and duplicate.deduplicated
    kinds = [item.fact.payload["kind"] for item in durable.load("new-session")
             if item.fact.kind is FactKind.INTERACTION]
    assert kinds == [InputKind.NEW_GOAL, InputKind.GOAL_SUPPLEMENT]


def test_clarification_waits_only_blocked_branch_and_recovers_without_model():
    controls, durable = controller()
    requested = controls.request_clarification(
        session_id="session", run_id="run", interaction_id="question-1",
        item_id="dependent", version=1, expected_run_version=1,
        question="Which region?", options=("east", "west"),
        reason="deployment target is ambiguous", blocked_items=("dependent",),
        timeout_seconds=60, now=NOW,
    )
    assert requested.run_status == RunStatus.RUNNING
    view = controls.waiting(session_id="session", run_id="run")
    assert view.runnable_items == ("independent",)
    assert view.requests[0].options == ("east", "west")
    recovered = controls.recover_waiting(session_id="session", run_id="run")
    assert recovered.requests[0].interaction_id == "question-1"
    assert recovered.requests[0].reason == "deployment target is ambiguous"

    resolved = controls.answer_clarification(
        session_id="session", run_id="run", interaction_id="question-1",
        interaction_version=1, expected_run_version=1,
        resolution_id="answer-1", answer="east", now=NOW,
    )
    duplicate = controls.answer_clarification(
        session_id="session", run_id="run", interaction_id="question-1",
        interaction_version=1, expected_run_version=1,
        resolution_id="answer-1", answer="east", now=NOW,
    )
    assert resolved.run_version == 2 and resolved.resumed_items == ("dependent",)
    assert duplicate.deduplicated
    assert controls.recover_waiting(session_id="session", run_id="run").requests == ()
    with pytest.raises(InteractionError) as conflict:
        controls.answer_clarification(
            session_id="session", run_id="run", interaction_id="question-1",
            interaction_version=1, expected_run_version=2,
            resolution_id="answer-2", answer="west", now=NOW,
        )
    assert conflict.value.code == "resolution_conflict"
    states = [item.fact.state for item in durable.load("session")
              if item.fact.kind is FactKind.INTERACTION]
    assert states == ["pending", "answered"]


def test_stale_and_out_of_order_answers_fail_closed():
    controls, _ = controller()
    controls.request_clarification(
        session_id="session", run_id="run", interaction_id="question-1",
        item_id="dependent", version=2, expected_run_version=1,
        question="Choose", options=(), reason="needed", blocked_items=("dependent",),
        timeout_seconds=30, now=NOW,
    )
    with pytest.raises(InteractionError) as stale_interaction:
        controls.answer_clarification(
            session_id="session", run_id="run", interaction_id="question-1",
            interaction_version=1, expected_run_version=1,
            resolution_id="answer", answer="value", now=NOW,
        )
    assert stale_interaction.value.code == "stale_interaction_version"
    with pytest.raises(InteractionError) as wrong_run:
        controls.answer_clarification(
            session_id="session", run_id="other", interaction_id="question-1",
            interaction_version=2, expected_run_version=1,
            resolution_id="answer", answer="value", now=NOW,
        )
    assert wrong_run.value.code == "run_not_active"
    with pytest.raises(InteractionError) as stale_run:
        controls.answer_clarification(
            session_id="session", run_id="run", interaction_id="question-1",
            interaction_version=2, expected_run_version=9,
            resolution_id="answer", answer="value", now=NOW,
        )
    assert stale_run.value.code == "stale_run_version"


def test_approval_binds_tool_arguments_scope_expiry_and_revocation():
    controls, durable = controller()
    controls.request_approval(
        session_id="session", run_id="run", interaction_id="approval-1",
        item_id="restart-1", version=1, expected_run_version=1,
        tool_name="restart", arguments={"service": "api"},
        arguments_summary="restart api", risk="brief outage",
        scope=("service:api",), reason="recovery action",
        blocked_items=("dependent",), timeout_seconds=60, now=NOW,
    )
    resolved = controls.resolve_approval(
        session_id="session", run_id="run", interaction_id="approval-1",
        interaction_version=1, expected_run_version=1,
        resolution_id="decision-1", decision="approve", now=NOW,
    )
    snapshot = controls.approval_snapshot(
        session_id="session", run_id="run", interaction_id="approval-1",
        interaction_version=1, arguments={"service": "api"}, now=NOW,
    )
    assert resolved.run_version == 2
    assert snapshot.tool_name == "restart" and snapshot.approved
    approval = next(item.fact for item in durable.load("session")
                    if item.fact.kind is FactKind.APPROVAL and item.fact.state == "pending")
    assert approval.payload["arguments_summary"] == "restart api"
    assert approval.payload["risk"] == "brief outage"
    assert approval.payload["scope"] == ["service:api"]
    assert approval.payload["expires_at"].endswith("Z")

    with pytest.raises(InteractionError) as changed:
        controls.approval_snapshot(
            session_id="session", run_id="run", interaction_id="approval-1",
            interaction_version=1, arguments={"service": "worker"}, now=NOW,
        )
    assert changed.value.code == "approval_arguments_changed"
    controls.revoke_approval(
        session_id="session", run_id="run", interaction_id="approval-1",
        interaction_version=1, expected_run_version=2, revocation_id="revoke-1",
    )
    with pytest.raises(InteractionError) as revoked:
        controls.approval_snapshot(
            session_id="session", run_id="run", interaction_id="approval-1",
            interaction_version=1, arguments={"service": "api"}, now=NOW,
        )
    assert revoked.value.code == "approval_not_active"


def test_denied_and_expired_approvals_return_structured_outcomes():
    policy = RuntimeControlPolicy(timeout_disposition=TimeoutDisposition.PARTIAL)
    controls, durable = controller(policy=policy)
    controls.request_approval(
        session_id="session", run_id="run", interaction_id="denied",
        item_id="tool-denied", version=1, expected_run_version=1,
        tool_name="write", arguments={"x": 1}, arguments_summary="write x",
        risk="mutation", scope=("x",), reason="requested",
        blocked_items=("dependent",), timeout_seconds=30, now=NOW,
    )
    controls.resolve_approval(
        session_id="session", run_id="run", interaction_id="denied",
        interaction_version=1, expected_run_version=1,
        resolution_id="deny-1", decision=ApprovalDecision.DENIED, now=NOW,
    )
    assert any(item.fact.payload.get("error_code") == "approval_denied"
               for item in durable.load("session") if item.fact.kind is FactKind.OUTCOME)

    controls.request_approval(
        session_id="session", run_id="run", interaction_id="expired",
        item_id="tool-expired", version=1, expected_run_version=2,
        tool_name="write", arguments={"x": 2}, arguments_summary="write x2",
        risk="mutation", scope=("x",), reason="requested",
        blocked_items=("dependent",), timeout_seconds=10, now=NOW,
    )
    expired = controls.expire(now=NOW + timedelta(seconds=11))
    assert expired[0].run_status == RunStatus.BLOCKED
    assert any(item.fact.payload.get("error_code") == "approval_expired"
               for item in durable.load("session") if item.fact.kind is FactKind.OUTCOME)
    assert any(item.fact.kind is FactKind.DELIVERY and item.fact.state == "partial"
               for item in durable.load("session"))


def test_steer_is_versioned_idempotent_and_does_not_rewrite_history():
    controls, durable = controller()
    durable.commit(
        session_id="session", event_id="history-before",
        facts=(SemanticFact(
            FactKind.OBSERVATION, "completed-before", "completed", {"value": 1},
        ),),
    )
    first = controls.steer(
        session_id="session", run_id="run", interaction_id="steer-1",
        expected_run_version=1, text="inspect metrics first",
    )
    duplicate = controls.steer(
        session_id="session", run_id="run", interaction_id="steer-1",
        expected_run_version=1, text="inspect metrics first",
    )
    assert first.run_version == 2 and duplicate.deduplicated
    assert controls.steer_updates(session_id="session", run_id="run") == (
        SteerUpdate("steer-1", 2, "inspect metrics first"),
    )
    assert any(item.fact.entity_id == "completed-before" for item in durable.load("session"))
    with pytest.raises(InteractionError) as stale:
        controls.steer(
            session_id="session", run_id="run", interaction_id="steer-2",
            expected_run_version=1, text="old direction",
        )
    assert stale.value.code == "stale_run_version"


def test_cancel_propagates_and_distinguishes_acknowledged_pending_and_unknown():
    controls, durable = controller()
    cancellation = controls.cancellation_event(session_id="session", run_id="run")
    controls.register_invocation(
        session_id="session", run_id="run", invocation_id="read",
        side_effecting=False, cancel=lambda: CancelDisposition.PENDING,
    )
    controls.register_invocation(
        session_id="session", run_id="run", invocation_id="write",
        side_effecting=True, cancel=lambda: CancelDisposition.PENDING,
    )
    result = controls.request_cancel(
        session_id="session", run_id="run", interaction_id="cancel-1",
        expected_run_version=1,
    )
    assert cancellation.is_set()
    assert result.run_status == "cancel_requested"
    assert result.invocation_states == {
        "read": CancelDisposition.PENDING,
        "write": CancelDisposition.UNKNOWN,
    }
    ack = controls.acknowledge_cancellation(
        session_id="session", run_id="run", invocation_id="read",
        disposition=CancelDisposition.CANCELLED,
    )
    assert ack.run_status == RunStatus.CANCELLED
    duplicate = controls.request_cancel(
        session_id="session", run_id="run", interaction_id="cancel-1",
        expected_run_version=1,
    )
    assert duplicate.deduplicated
    invocation_states = [item.fact.state for item in durable.load("session")
                         if item.fact.kind is FactKind.INVOCATION]
    assert invocation_states == ["cancel_requested", "unknown", "cancelled"]


def test_disconnect_policy_and_waiting_timeout_are_deterministic():
    controls, durable = controller(policy=RuntimeControlPolicy(
        frontend_disconnect=DisconnectDisposition.CONTINUE,
        host_disconnect=DisconnectDisposition.BLOCK,
        background_allowed=True,
        timeout_disposition=TimeoutDisposition.BLOCK,
    ))
    continued = controls.handle_disconnect(
        session_id="session", run_id="run", source="frontend",
        interaction_id="frontend-away", expected_run_version=1,
    )
    blocked = controls.handle_disconnect(
        session_id="session", run_id="run", source="host",
        interaction_id="host-away", expected_run_version=2,
    )
    assert continued.run_status == RunStatus.RUNNING
    assert blocked.run_status == RunStatus.BLOCKED
    assert [item.fact.state for item in durable.load("session") if item.fact.kind is FactKind.OBSERVATION] == [
        "continue", "block",
    ]


def test_timeout_cancel_propagates_and_reconnected_host_attaches_to_waiting_run():
    controls, durable = controller(policy=RuntimeControlPolicy(
        timeout_disposition=TimeoutDisposition.CANCEL,
    ))
    controls.register_invocation(
        session_id="session", run_id="run", invocation_id="tool",
        side_effecting=True, cancel=lambda: CancelDisposition.PENDING,
    )
    controls.request_clarification(
        session_id="session", run_id="run", interaction_id="question-timeout",
        item_id="dependent", version=1, expected_run_version=1,
        question="Continue?", options=("yes", "no"), reason="decision required",
        blocked_items=("dependent",), timeout_seconds=5, now=NOW,
    )
    reconnected = InteractionHostAdapter(controls)
    reconnected.attach_run(session_id="session", run_id="run")
    result = controls.expire(now=NOW + timedelta(seconds=6))[0]
    assert result.run_status == RunStatus.CANCELLED
    invocation = [item.fact for item in durable.load("session")
                  if item.fact.kind is FactKind.INVOCATION]
    assert invocation[-1].state == "unknown"
    assert invocation[-1].payload["replay_allowed"] is False


def test_h3_protocol_e2e_routes_clarification_approval_steer_and_cancel(tmp_path: Path):
    durable = ReferenceDurableSessionStore()
    controls = InteractionController(durable)
    adapter = InteractionHostAdapter(controls)
    adapter.bind_run(
        tenant_id="tenant", user_id="user", session_id="session", run_id="run",
        runnable_items=("clarify-item", "approve-item"),
    )
    current = datetime.now(timezone.utc)
    controls.request_clarification(
        session_id="session", run_id="run", interaction_id="question",
        item_id="clarify-item", version=1, expected_run_version=1,
        question="Which target?", options=("a", "b"), reason="ambiguous",
        blocked_items=("clarify-item",), timeout_seconds=60, now=current,
    )
    controls.request_approval(
        session_id="session", run_id="run", interaction_id="approval",
        item_id="approve-item", version=1, expected_run_version=1,
        tool_name="change", arguments={"target": "a"}, arguments_summary="change a",
        risk="mutation", scope=("target:a",), reason="required",
        blocked_items=("approve-item",), timeout_seconds=60, now=current,
    )
    controls.register_invocation(
        session_id="session", run_id="run", invocation_id="tool",
        side_effecting=False, cancel=lambda: CancelDisposition.CANCELLED,
    )
    frames = [
        _open_frame(tmp_path),
        _session_frame("clarification.answer", "answer", run_id="run", interaction_id="question",
                       payload={"run_version": 1, "interaction_version": 1, "answer": "a"}),
        _session_frame("turn.steer", "steer", run_id="run", interaction_id="steer-1", turn_id="turn",
                       payload={"run_version": 2, "text": "verify before delivery"}),
        _session_frame("approval.resolve", "approve", run_id="run", interaction_id="approval",
                       payload={"run_version": 3, "interaction_version": 1, "decision": "approve"}),
        _session_frame("turn.cancel", "cancel", run_id="run", interaction_id="cancel-1", turn_id="turn",
                       payload={"run_version": 4}),
        {"protocol_version": "1.0", "type": "shutdown", "request_id": "shutdown", "payload": {}},
    ]
    input_stream = io.StringIO("".join(json.dumps(frame) + "\n" for frame in frames))
    output = io.StringIO()

    assert JsonlHost(input_stream, output, runtime=adapter).serve() == 0
    events = [json.loads(line) for line in output.getvalue().splitlines()]
    assert any(item["type"] == "clarification.resolved" for item in events)
    assert any(item["type"] == "approval.resolved" for item in events)
    controlled = [item for item in events if item["type"] == "turn.controlled"]
    assert [item["payload"]["command"] for item in controlled] == ["turn.steer", "turn.cancel"]
    assert controlled[-1]["payload"]["invocation_states"] == {"tool": "cancelled"}


def _lease():
    issued = datetime(2030, 1, 1, tzinfo=timezone.utc)
    return {
        "session_id": "session", "owner_id": "owner", "lease_id": "lease",
        "lease_version": 1, "execution_epoch": 1,
        "issued_at": issued.isoformat(),
        "renew_by": (issued + timedelta(seconds=20)).isoformat(),
        "expires_at": (issued + timedelta(seconds=60)).isoformat(),
        "grace_expires_at": (issued + timedelta(seconds=65)).isoformat(),
        "ttl_ms": 60_000, "renew_interval_ms": 20_000, "grace_ms": 5_000,
    }


def _open_frame(tmp_path: Path):
    return _session_frame("session.open", "open", payload={
        "workspace_root": str(tmp_path / "workspace"), "owner_id": "owner",
        "execution_epoch": 1, "lease": _lease(),
    })


def _session_frame(command: str, request_id: str, *, payload, run_id=None,
                   interaction_id=None, turn_id=None):
    frame = {
        "protocol_version": "1.0", "type": command, "request_id": request_id,
        "tenant_id": "tenant", "user_id": "user", "session_id": "session",
        "payload": payload,
    }
    if run_id is not None:
        frame["run_id"] = run_id
    if interaction_id is not None:
        frame["interaction_id"] = interaction_id
    if turn_id is not None:
        frame["turn_id"] = turn_id
    return frame
