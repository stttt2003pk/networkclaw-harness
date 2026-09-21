from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from networkclaw_harness.runtime import (
    DurableWriteError,
    FactKind,
    FairSessionScheduler,
    HermesSessionPersistence,
    HistoryError,
    InteractionStatus,
    InvocationStatus,
    ReferenceDurableSessionStore,
    ReferenceSessionHistory,
    RunStatus,
    ScheduledInput,
    SchedulingError,
    SemanticFact,
    SemanticPersistenceService,
    SessionRegistry,
    SessionRuntimeError,
    TranscriptMessage,
    WorkspaceDurableSessionStore,
)
from networkclaw_harness.workspace import (
    ArtifactMetadataStore,
    ArtifactService,
    CheckpointStore,
    EpochGuard,
    LeasePolicy,
    QuotaLedger,
    QuotaPolicy,
    ReferenceLeaseAuthority,
    SessionWorkspace,
)


def lease_fixture(session_id: str = "session-a", owner_id: str = "owner-a"):
    authority = ReferenceLeaseAuthority()
    policy = LeasePolicy(ttl_ms=60_000, renew_interval_ms=20_000, grace_ms=5_000)
    record = authority.acquire(
        session_id=session_id, owner_id=owner_id, policy=policy,
        expected_lease_version=0, expected_execution_epoch=0,
    )
    return authority, EpochGuard(authority), record, authority.token(record)


def test_workspace_durable_store_survives_reopen_and_deduplicates(tmp_path: Path):
    workspace = SessionWorkspace.open(tmp_path / "session", tenant_id="tenant", session_id="session-a")
    first = WorkspaceDurableSessionStore(workspace)
    fact = SemanticFact(FactKind.RUN, "run-1", RunStatus.RUNNING, {"goal_id": "goal-1"})
    ack = first.commit(session_id="session-a", event_id="event-1", facts=(fact,))
    assert ack.cursor == 1
    first.close()

    reopened = WorkspaceDurableSessionStore(workspace)
    assert reopened.cursor("session-a") == 1
    assert reopened.load("session-a")[0].fact == fact
    duplicate = reopened.commit(session_id="session-a", event_id="event-1", facts=(fact,))
    assert duplicate.deduplicated is True and duplicate.cursor == 1
    reopened.close()


def test_registry_open_resume_close_capacity_and_isolation(tmp_path: Path):
    authority = ReferenceLeaseAuthority()
    registry = SessionRegistry(authority, capacity=2)
    a = registry.open(
        tenant_id="tenant", session_id="a", owner_id="owner-a",
        workspace_root=tmp_path / "a", idempotency_key="open-a",
    )
    b = registry.open(
        tenant_id="tenant", session_id="b", owner_id="owner-b",
        workspace_root=tmp_path / "b", idempotency_key="open-b",
    )
    a.events.append("a-event")
    a.budgets["tokens"] = 10
    a.approvals["approval-a"] = "approved"
    a.cache["key"] = "a"
    assert not b.events and not b.budgets and not b.approvals and not b.cache
    with pytest.raises(SessionRuntimeError) as full:
        registry.open(
            tenant_id="tenant", session_id="c", owner_id="owner-c",
            workspace_root=tmp_path / "c", idempotency_key="open-c",
        )
    assert full.value.code == "session_capacity_exceeded"

    closed = registry.close("a")
    assert registry.list_open() == ("b",)
    assert registry.require("b") is b
    resumed = registry.resume(
        tenant_id="tenant", session_id="a", owner_id="owner-new",
        workspace_root=tmp_path / "a", idempotency_key="resume-a",
        expected_lease_version=closed.lease.lease_version,
        expected_execution_epoch=closed.lease.execution_epoch,
    )
    assert resumed.lease.execution_epoch == closed.lease.execution_epoch + 1
    assert resumed.cache == {}


def test_concurrent_duplicate_open_converges_and_conflicts_fail(tmp_path: Path):
    authority = ReferenceLeaseAuthority()
    registry = SessionRegistry(authority, capacity=2)
    barrier = threading.Barrier(2)

    def open_once():
        barrier.wait()
        return registry.open(
            tenant_id="tenant", session_id="same", owner_id="owner",
            workspace_root=tmp_path / "same", idempotency_key="request-1",
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        first, second = list(pool.map(lambda _: open_once(), range(2)))
    assert first is second
    assert authority.current("same") == first.lease
    with pytest.raises(SessionRuntimeError) as duplicate_conflict:
        registry.open(
            tenant_id="tenant", session_id="same", owner_id="owner",
            workspace_root=tmp_path / "same", idempotency_key="request-2",
        )
    assert duplicate_conflict.value.code == "session_open_conflict"
    with pytest.raises(SessionRuntimeError) as owner_conflict:
        registry.open(
            tenant_id="tenant", session_id="same", owner_id="other",
            workspace_root=tmp_path / "same", idempotency_key="request-1",
        )
    assert owner_conflict.value.code == "session_owned"


def test_one_active_run_per_session_and_fair_priority_scheduler(tmp_path: Path):
    authority = ReferenceLeaseAuthority()
    registry = SessionRegistry(authority, capacity=2)
    for session_id in ("a", "b"):
        registry.open(
            tenant_id="tenant", session_id=session_id, owner_id=f"owner-{session_id}",
            workspace_root=tmp_path / session_id, idempotency_key=f"open-{session_id}",
        )
    registry.begin_run("a", "run-a1")
    with pytest.raises(SessionRuntimeError) as active:
        registry.begin_run("a", "run-a2")
    assert active.value.code == "run_already_active"
    registry.begin_run("b", "run-b1")
    registry.end_run("a", "run-a1")
    assert registry.require("b").active_run_id == "run-b1"

    scheduler = FairSessionScheduler(max_active_runs=1, max_queued_per_session=4)
    scheduler.enqueue(ScheduledInput("a", "a1", {"value": "a1"}))
    scheduler.enqueue(ScheduledInput("a", "a2", {"value": "a2"}))
    scheduler.enqueue(ScheduledInput("b", "b1", {"value": "b1"}))
    scheduler.enqueue(ScheduledInput("a", "cancel-a", {}, control=True))
    assert scheduler.take_control("a").item_id == "cancel-a"
    first = scheduler.dispatch()
    assert first.item_id == "a1"
    assert scheduler.dispatch() is None
    scheduler.complete("a", "a1")
    second = scheduler.dispatch()
    assert second.item_id == "b1"
    scheduler.complete("b", "b1")
    third = scheduler.dispatch()
    assert third.item_id == "a2"
    with pytest.raises(SchedulingError):
        scheduler.close_session("a")
    scheduler.complete("a", "a2")
    scheduler.close_session("a")


def test_history_phases_role_alternation_and_tool_pairing():
    history = ReferenceSessionHistory()
    history.open("session")
    messages = (
        TranscriptMessage("user", "inspect", "user_interaction"),
        TranscriptMessage("assistant", None, "model_step", tool_calls=("call-1", "call-2")),
        TranscriptMessage("tool", "one", "tool_round", tool_call_id="call-1", tool_name="inspect"),
        TranscriptMessage("tool", "two", "tool_round", tool_call_id="call-2", tool_name="inspect"),
        TranscriptMessage("assistant", "done", "final_delivery"),
    )
    history.append("session", messages)
    assert [item.phase for item in history.load("session")] == [
        "user_interaction", "model_step", "tool_round", "tool_round", "final_delivery"
    ]
    with pytest.raises(HistoryError) as alternation:
        history.append("session", (TranscriptMessage("assistant", "again", "model_step"),))
    assert alternation.value.code == "role_alternation"

    invalid = ReferenceSessionHistory()
    invalid.open("bad")
    with pytest.raises(HistoryError) as mismatch:
        invalid.append("bad", (
            TranscriptMessage("user", "go", "user_interaction"),
            TranscriptMessage("assistant", None, "model_step", tool_calls=("expected",)),
            TranscriptMessage("tool", "wrong", "tool_round", tool_call_id="other"),
        ))
    assert mismatch.value.code == "tool_result_mismatch"


class FakeHermesDB:
    def __init__(self):
        self.messages = {}
        self.ended = []

    def create_session(self, session_id, source, **kwargs):
        self.messages.setdefault(session_id, [])
        return session_id

    def reopen_session(self, session_id):
        self.messages.setdefault(session_id, [])

    def end_session(self, session_id, end_reason):
        self.ended.append((session_id, end_reason))

    def append_messages_batch(self, session_id, messages, **kwargs):
        self.messages.setdefault(session_id, []).extend(messages)
        return len(messages)

    def get_messages(self, session_id, **kwargs):
        return list(self.messages.get(session_id, ()))


def test_hermes_persistence_is_used_through_narrow_adapter():
    database = FakeHermesDB()
    adapter = HermesSessionPersistence(database)
    adapter.open("session")
    adapter.append("session", (
        TranscriptMessage("user", "hello", "user_interaction"),
        TranscriptMessage("assistant", "answer", "final_delivery"),
    ))
    assert adapter.load("session")[1].phase == "final_delivery"
    assert database.messages["session"][0]["display_kind"] == "user_interaction"
    adapter.close("session", "complete")
    assert database.ended == [("session", "complete")]
    adapter.resume("session")


def test_durable_batches_are_atomic_idempotent_and_session_partitioned():
    store = ReferenceDurableSessionStore()
    batch = (
        SemanticFact(FactKind.GOAL, "goal-1", "active", {"acceptance": ["works"]}),
        SemanticFact(FactKind.PLAN, "plan-1", "active", {"steps": ["one"]}),
    )
    first = store.commit(session_id="a", event_id="event-1", facts=batch)
    duplicate = store.commit(session_id="a", event_id="event-1", facts=batch)
    assert duplicate.deduplicated and duplicate.cursor == first.cursor == 2
    with pytest.raises(DurableWriteError) as conflict:
        store.commit(
            session_id="a", event_id="event-1",
            facts=(SemanticFact(FactKind.GOAL, "goal-1", "completed"),),
        )
    assert conflict.value.code == "idempotency_conflict"

    store.fail_next_commit()
    with pytest.raises(DurableWriteError):
        store.commit(
            session_id="a", event_id="event-2",
            facts=(SemanticFact(FactKind.RUN, "run-1", RunStatus.RUNNING),
                   SemanticFact(FactKind.INTERACTION, "interaction-1", InteractionStatus.PENDING)),
        )
    assert store.cursor("a") == 2
    store.commit(
        session_id="b", event_id="event-b",
        facts=(SemanticFact(FactKind.GOAL, "goal-b", "active"),),
    )
    assert {item.fact.entity_id for item in store.load("a")} == {"goal-1", "plan-1"}
    assert {item.fact.entity_id for item in store.load("b")} == {"goal-b"}


def test_all_semantic_assets_and_state_distinctions_are_durable():
    store = ReferenceDurableSessionStore()
    facts = (
        SemanticFact(FactKind.GOAL, "goal", "active"),
        SemanticFact(FactKind.PLAN, "plan", "versioned", {"version": 2}),
        SemanticFact(FactKind.RUN, "run", RunStatus.WAITING),
        SemanticFact(FactKind.INTERACTION, "question", InteractionStatus.PENDING),
        SemanticFact(FactKind.INVOCATION, "tool", InvocationStatus.SUCCEEDED),
        SemanticFact(FactKind.OUTCOME, "tool", "observed", {"business_success": False}),
        SemanticFact(FactKind.ARTIFACT_REF, "artifact", "retained", {"sha256": "a" * 64}),
        SemanticFact(FactKind.UNRESOLVED_ITEM, "gap", "open"),
    )
    store.commit(session_id="session", event_id="semantic-assets", facts=facts)
    loaded = store.load("session")
    assert {item.fact.kind for item in loaded} == {
        FactKind.GOAL, FactKind.PLAN, FactKind.RUN, FactKind.INTERACTION,
        FactKind.INVOCATION, FactKind.OUTCOME, FactKind.ARTIFACT_REF,
        FactKind.UNRESOLVED_ITEM,
    }
    outcome = next(item.fact for item in loaded if item.fact.kind is FactKind.OUTCOME)
    assert outcome.payload["business_success"] is False


class TracingStore(ReferenceDurableSessionStore):
    def __init__(self, trace):
        super().__init__()
        self.trace = trace

    def commit(self, **kwargs):
        self.trace.append(f"durable:{kwargs['event_id']}")
        return super().commit(**kwargs)


def test_tool_intent_and_approval_ack_precede_effect_and_result(tmp_path: Path):
    del tmp_path
    authority, guard, _, token = lease_fixture()
    trace = []
    store = TracingStore(trace)
    service = SemanticPersistenceService(store, guard)

    def execute():
        trace.append("effect")
        return {"exit_code": 0, "verified": False}

    def artifact(result):
        trace.append("artifact")
        return "artifact-1"

    result = service.execute_tool(
        token, event_id="tool-1", invocation_id="invocation-1",
        intent={"tool": "command", "side_effect": True}, approval={"approval_id": "approval-1"},
        executor=execute, artifact_commit=artifact,
        business_success=lambda value: value["verified"],
    )
    assert trace == ["durable:tool-1:intent", "effect", "artifact", "durable:tool-1:result"]
    assert result.status is InvocationStatus.SUCCEEDED
    assert result.business_success is False
    assert result.artifact_id == "artifact-1"


def test_durable_failure_stops_side_effect_and_disconnect_is_unknown():
    authority, guard, _, token = lease_fixture()
    store = ReferenceDurableSessionStore()
    service = SemanticPersistenceService(store, guard)
    effects = []
    store.fail_next_commit()
    with pytest.raises(DurableWriteError):
        service.execute_tool(
            token, event_id="blocked", invocation_id="inv-blocked", intent={}, approval=None,
            executor=lambda: effects.append("ran") or {},
        )
    assert effects == []

    unknown = service.execute_tool(
        token, event_id="disconnect", invocation_id="inv-unknown", intent={}, approval=None,
        executor=lambda: (_ for _ in ()).throw(ConnectionError("lost after send")),
    )
    assert unknown.status is InvocationStatus.UNKNOWN
    latest = store.load("session-a")[-1].fact
    assert latest.state == "unknown" and latest.payload["replay_allowed"] is False


def test_cancel_request_is_distinct_from_confirmation():
    authority, guard, _, token = lease_fixture()
    store = ReferenceDurableSessionStore()
    service = SemanticPersistenceService(store, guard)
    service.request_cancel(token, event_id="cancel-request", invocation_id="inv")
    assert store.load("session-a")[-1].fact.state == "cancel_requested"
    service.confirm_cancelled(token, event_id="cancel-confirm", invocation_id="inv")
    assert [item.fact.state for item in store.load("session-a")] == ["cancel_requested", "cancelled"]


class FakeCheckpointStore:
    def __init__(self, trace, fail=False):
        self.trace = trace
        self.fail = fail

    def write(self, token, **kwargs):
        self.trace.append("checkpoint")
        if self.fail:
            raise OSError("checkpoint crash")
        return kwargs


def test_artifact_and_durable_ack_precede_checkpoint_and_failure_is_not_completed():
    authority, guard, _, token = lease_fixture()
    trace = []
    store = TracingStore(trace)
    service = SemanticPersistenceService(store, guard)
    checkpoint = FakeCheckpointStore(trace)
    result = service.commit_delivery(
        token, event_id="delivery", delivery_facts=(SemanticFact(FactKind.DELIVERY, "delivery", "completed"),),
        artifact_commit=lambda: trace.append("artifact") or "a" * 64,
        checkpoint_store=checkpoint, hermes_snapshot_hash="b" * 64, state={"complete": True},
    )
    assert trace == ["artifact", "durable:delivery", "checkpoint"]
    assert result["durable_cursor"] == "1"

    failing_trace = []
    failing_store = TracingStore(failing_trace)
    failing = SemanticPersistenceService(failing_store, guard)
    with pytest.raises(OSError, match="checkpoint crash"):
        failing.commit_delivery(
            token, event_id="delivery-fail",
            delivery_facts=(SemanticFact(FactKind.DELIVERY, "delivery-fail", "completed"),),
            artifact_commit=lambda: failing_trace.append("artifact") or "c" * 64,
            checkpoint_store=FakeCheckpointStore(failing_trace, fail=True),
            hermes_snapshot_hash="d" * 64, state={"complete": True},
        )
    assert failing_trace == ["artifact", "durable:delivery-fail", "checkpoint"]
    assert failing_store.cursor("session-a") == 1


def test_new_process_loads_only_own_facts_and_checkpoint_without_replay(tmp_path: Path):
    authority, guard, record, token = lease_fixture()
    store = ReferenceDurableSessionStore()
    store.commit(
        session_id="session-a", event_id="done-a",
        facts=(SemanticFact(FactKind.RUN, "run-a", RunStatus.COMPLETED),),
    )
    store.commit(
        session_id="session-b", event_id="done-b",
        facts=(SemanticFact(FactKind.RUN, "run-b", RunStatus.COMPLETED),),
    )
    workspace = SessionWorkspace.open(tmp_path / "a", tenant_id="tenant", session_id="session-a")
    checkpoints = CheckpointStore(workspace, guard)
    expected = checkpoints.write(
        token, durable_cursor="1", hermes_snapshot_hash="a" * 64,
        artifact_manifest_hash="b" * 64,
        state={"safe_summary": "retained", "pid": 123, "tool_handle": "old", "subagent_handle": "old-agent"},
    )
    recovered = SemanticPersistenceService(store, guard).recover(token, checkpoint_store=checkpoints)
    assert recovered.checkpoint is not None
    assert recovered.checkpoint.durable_cursor == expected.durable_cursor
    assert recovered.checkpoint.state == {"safe_summary": "retained"}
    assert {item.fact.entity_id for item in recovered.facts} == {"run-a"}
    assert recovered.runnable_actions == () and recovered.ephemeral_handles == ()


def test_reopened_session_loads_only_its_artifact_metadata(tmp_path: Path):
    authority = ReferenceLeaseAuthority()
    guard = EpochGuard(authority)
    policy = LeasePolicy(ttl_ms=60_000, renew_interval_ms=20_000, grace_ms=5_000)
    metadata = ArtifactMetadataStore(guard)
    ledger = QuotaLedger()
    quota = QuotaPolicy(10_000, 10_000, 10, 10_000, 10_000)
    services = {}
    tokens = {}
    for session_id in ("a", "b"):
        lease = authority.acquire(
            session_id=session_id, owner_id=f"owner-{session_id}", policy=policy,
            expected_lease_version=0, expected_execution_epoch=0,
        )
        token = authority.token(lease)
        workspace = SessionWorkspace.open(tmp_path / session_id, tenant_id="tenant", session_id=session_id)
        services[session_id] = ArtifactService(workspace, guard, metadata, ledger, quota)
        tokens[session_id] = token
        services[session_id].write(
            token, artifact_id=f"artifact-{session_id}", category="generated",
            chunks=(session_id.encode(),), mime_type="text/plain", source={"session": session_id},
        )

    reopened_a = ArtifactService(services["a"].workspace, guard, metadata, ledger, quota)
    assert [item.artifact_id for item in metadata.list_session("tenant", "a")] == ["artifact-a"]
    assert reopened_a.read_range(tokens["a"], "artifact-a", offset=0, length=1) == b"a"
    with pytest.raises(Exception) as cross_session:
        reopened_a.read_range(tokens["a"], "artifact-b", offset=0, length=1)
    assert getattr(cross_session.value, "code", None) == "artifact_not_found"


def test_recovery_marks_running_work_interrupted_or_unknown_without_replay():
    authority, guard, _, token = lease_fixture()
    store = ReferenceDurableSessionStore()
    store.commit(
        session_id="session-a", event_id="before-crash",
        facts=(
            SemanticFact(FactKind.RUN, "run", RunStatus.RUNNING, {"pid": 999}),
            SemanticFact(FactKind.INVOCATION, "tool", InvocationStatus.RUNNING, {"handle": "old"}),
        ),
    )
    recovered = SemanticPersistenceService(store, guard).recover(token)
    assert recovered.interrupted_entities == ("run",)
    assert recovered.unknown_invocations == ("tool",)
    assert recovered.runnable_actions == () and recovered.ephemeral_handles == ()
    states = {(item.fact.kind, item.fact.entity_id): item.fact.state for item in recovered.facts}
    assert states[(FactKind.RUN, "run")] == "interrupted"
    assert states[(FactKind.INVOCATION, "tool")] == "unknown"
    assert store.load("session-a")[-1].fact.payload["replay_allowed"] is False
