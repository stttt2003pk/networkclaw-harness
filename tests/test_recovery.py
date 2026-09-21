from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from networkclaw_harness.runtime import (
    ActualEffectState,
    ApprovalDecision,
    CheckpointDisposition,
    FactKind,
    InteractionController,
    InteractionError,
    InvocationStatus,
    ReconciliationAction,
    RecoveryClassification,
    RecoveryError,
    RecoveryReconciler,
    ReferenceDurableSessionStore,
    RunStatus,
    SemanticFact,
    SemanticPersistenceService,
    SideEffectReconciler,
    ToolRecoveryContract,
    UserEffectDecision,
    WaitingRecoveryChoice,
)
from networkclaw_harness.workspace import (
    ArtifactMetadataStore,
    ArtifactService,
    CheckpointStore,
    EpochError,
    EpochGuard,
    LeasePolicy,
    QuotaLedger,
    QuotaPolicy,
    ReferenceLeaseAuthority,
    SessionWorkspace,
)


def _lease(session_id="session", owner_id="owner-a"):
    authority = ReferenceLeaseAuthority()
    policy = LeasePolicy(ttl_ms=60_000, renew_interval_ms=20_000, grace_ms=5_000)
    record = authority.acquire(
        session_id=session_id, owner_id=owner_id, policy=policy,
        expected_lease_version=0, expected_execution_epoch=0,
    )
    return authority, EpochGuard(authority), record, authority.token(record)


def _contract(**changes):
    values = {
        "tool_name": "network.change",
        "read_only": False,
        "idempotent": True,
        "retryable": True,
        "side_effecting": True,
        "idempotency_key": "operation-1",
        "event_dedupe_key": "event-1",
        "status_query_key": "change-42",
    }
    values.update(changes)
    return ToolRecoveryContract(**values)


def _intent(contract, *, run_id="run-1"):
    return {
        "run_id": run_id,
        "tool_name": contract.tool_name,
        "read_only": contract.read_only,
        "idempotent": contract.idempotent,
        "retryable": contract.retryable,
        "side_effecting": contract.side_effecting,
        "idempotency_key": contract.idempotency_key,
        "event_dedupe_key": contract.event_dedupe_key,
        "status_query_key": contract.status_query_key,
    }


def test_recovery_classifies_terminal_waiting_retry_unknown_and_strips_handles():
    _, guard, _, token = _lease()
    durable = ReferenceDurableSessionStore()
    safe = _contract(
        tool_name="inventory.read", read_only=True, side_effecting=False,
        status_query_key=None,
    )
    unsafe = _contract()
    durable.commit(session_id="session", event_id="facts", facts=(
        SemanticFact(FactKind.RUN, "done", RunStatus.COMPLETED),
        SemanticFact(FactKind.RUN, "cancelled", RunStatus.CANCELLED),
        SemanticFact(FactKind.RUN, "waiting", RunStatus.WAITING),
        SemanticFact(FactKind.INTERACTION, "question", "pending", {
            "run_id": "waiting", "interaction_version": 2,
        }),
        SemanticFact(FactKind.INVOCATION, "safe", InvocationStatus.UNKNOWN, {
            **_intent(safe), "pid": 123, "tool_handle": "dead",
        }),
        SemanticFact(FactKind.INVOCATION, "unsafe", InvocationStatus.UNKNOWN, {
            **_intent(unsafe), "browser_handle": "dead",
        }),
    ))

    snapshot = RecoveryReconciler(durable, guard).recover(token)
    classifications = {
        (item.kind, item.entity_id): item.classification for item in snapshot.items
    }

    assert classifications[(FactKind.RUN, "done")] is RecoveryClassification.COMPLETED
    assert classifications[(FactKind.RUN, "cancelled")] is RecoveryClassification.CANCELLED
    assert classifications[(FactKind.RUN, "waiting")] is RecoveryClassification.WAITING
    assert classifications[(FactKind.INTERACTION, "question")] is RecoveryClassification.WAITING
    assert classifications[(FactKind.INVOCATION, "safe")] is RecoveryClassification.SAFE_TO_RETRY
    assert classifications[(FactKind.INVOCATION, "unsafe")] is RecoveryClassification.UNKNOWN
    assert snapshot.waiting_interaction_ids == ("question",)
    assert snapshot.runnable_actions == () and snapshot.ephemeral_handles == ()
    safe_item = next(item for item in snapshot.items if item.entity_id == "safe")
    unsafe_item = next(item for item in snapshot.items if item.entity_id == "unsafe")
    assert "pid" not in safe_item.payload and "tool_handle" not in safe_item.payload
    assert "browser_handle" not in unsafe_item.payload


@pytest.mark.parametrize("crash_stage,effect_count,fact_count", (
    ("before_intent", 0, 0),
    ("after_intent", 0, 1),
    ("after_effect", 1, 1),
    ("before_result_commit", 1, 1),
))
def test_failure_injection_never_blindly_replays_unknown_effect(
    crash_stage, effect_count, fact_count,
):
    _, guard, _, token = _lease()
    durable = ReferenceDurableSessionStore()
    service = SemanticPersistenceService(durable, guard)
    effects = []

    def inject(stage):
        if stage == crash_stage:
            raise RuntimeError(f"crash at {stage}")

    with pytest.raises(RuntimeError, match="crash"):
        service.execute_tool(
            token, event_id="change", invocation_id="inv-1",
            intent=_intent(_contract()), approval={"approval_id": "approval-1"},
            executor=lambda: effects.append("applied") or {"change_id": "42"},
            failure_injector=inject,
        )
    assert len(effects) == effect_count
    assert len(durable.load("session")) == fact_count + (1 if fact_count else 0)

    snapshot = RecoveryReconciler(durable, guard).recover(token)
    if crash_stage == "before_intent":
        assert not snapshot.items
    else:
        invocation = next(item for item in snapshot.items if item.kind is FactKind.INVOCATION)
        assert invocation.state == InvocationStatus.UNKNOWN
        assert invocation.classification is RecoveryClassification.UNKNOWN
        assert invocation.payload["replay_allowed"] is False
        assert effects == (["applied"] if effect_count else [])


def test_unknown_side_effect_queries_actual_state_or_requires_explicit_safe_decision():
    _, guard, _, token = _lease()
    durable = ReferenceDurableSessionStore()
    contract = _contract()
    durable.commit(session_id="session", event_id="unknown", facts=(SemanticFact(
        FactKind.INVOCATION, "inv-1", InvocationStatus.UNKNOWN,
        {**_intent(contract), "replay_allowed": False},
    ),))
    reconciler = SideEffectReconciler(durable, guard)

    blocked = reconciler.reconcile(token, invocation_id="inv-1", contract=contract)
    assert blocked.action is ReconciliationAction.QUERY_REQUIRED
    assert blocked.classification is RecoveryClassification.UNKNOWN

    inconclusive = reconciler.reconcile(
        token, invocation_id="inv-1", contract=contract,
        status_query=lambda value: ActualEffectState.UNKNOWN,
    )
    assert inconclusive.action is ReconciliationAction.USER_DECISION_REQUIRED

    queried = []
    completed = reconciler.reconcile(
        token, invocation_id="inv-1", contract=contract,
        status_query=lambda value: queried.append(value.status_query_key) or ActualEffectState.SUCCEEDED,
    )
    assert queried == ["change-42"]
    assert completed.action is ReconciliationAction.OBSERVED_SUCCEEDED
    latest = [item.fact for item in durable.load("session") if item.fact.kind is FactKind.INVOCATION][-1]
    assert latest.state == InvocationStatus.SUCCEEDED
    assert latest.payload["idempotency_key"] == "operation-1"
    assert latest.payload["event_dedupe_key"] == "event-1"
    assert latest.payload["status_query_key"] == "change-42"


def test_disconnect_unknown_preserves_only_recovery_identity_from_durable_intent():
    _, guard, _, token = _lease()
    durable = ReferenceDurableSessionStore()
    contract = _contract()

    result = SemanticPersistenceService(durable, guard).execute_tool(
        token, event_id="disconnect", invocation_id="inv-1",
        intent={**_intent(contract), "arguments": {"secret": "must-not-copy"}},
        approval=None,
        executor=lambda: (_ for _ in ()).throw(ConnectionError("lost after dispatch")),
    )

    assert result.status is InvocationStatus.UNKNOWN
    latest = [item.fact for item in durable.load("session") if item.fact.kind is FactKind.INVOCATION][-1]
    assert latest.payload["idempotency_key"] == "operation-1"
    assert latest.payload["status_query_key"] == "change-42"
    assert "arguments" not in latest.payload
    pending = SideEffectReconciler(durable, guard).reconcile(
        token, invocation_id="inv-1", contract=contract,
    )
    assert pending.action is ReconciliationAction.QUERY_REQUIRED


def test_reconciliation_contract_must_match_durable_intent():
    _, guard, _, token = _lease()
    durable = ReferenceDurableSessionStore()
    contract = _contract()
    durable.commit(session_id="session", event_id="unknown", facts=(SemanticFact(
        FactKind.INVOCATION, "inv-1", InvocationStatus.UNKNOWN, _intent(contract),
    ),))

    with pytest.raises(RecoveryError) as mismatch:
        SideEffectReconciler(durable, guard).reconcile(
            token, invocation_id="inv-1",
            contract=_contract(read_only=True, side_effecting=False, status_query_key=None),
            new_invocation_id="unsafe-new",
        )

    assert mismatch.value.code == "recovery_contract_mismatch"
    assert durable.cursor("session") == 1


def test_retry_uses_new_invocation_and_non_idempotent_unknown_cannot_retry():
    _, guard, _, token = _lease()
    durable = ReferenceDurableSessionStore()
    read = _contract(
        tool_name="inventory.read", read_only=True, side_effecting=False,
        status_query_key=None,
    )
    durable.commit(session_id="session", event_id="safe", facts=(SemanticFact(
        FactKind.INVOCATION, "read-old", InvocationStatus.UNKNOWN, _intent(read),
    ),))
    reconciler = SideEffectReconciler(durable, guard)
    proposed = reconciler.reconcile(token, invocation_id="read-old", contract=read)
    assert proposed.action is ReconciliationAction.RETRY_NEW_INVOCATION
    assert proposed.requires_new_invocation
    queued = reconciler.reconcile(
        token, invocation_id="read-old", contract=read, new_invocation_id="read-new",
    )
    assert queued.action is ReconciliationAction.RETRY_NEW_INVOCATION
    assert durable.load("session")[-1].fact.entity_id == "read-new"
    assert durable.load("session")[-1].fact.state == InvocationStatus.QUEUED

    unsafe = _contract(idempotent=False, idempotency_key=None, status_query_key=None)
    durable.commit(session_id="session", event_id="unsafe", facts=(SemanticFact(
        FactKind.INVOCATION, "unsafe", InvocationStatus.UNKNOWN, _intent(unsafe),
    ),))
    with pytest.raises(RecoveryError) as denied:
        reconciler.reconcile(
            token, invocation_id="unsafe", contract=unsafe,
            user_decision=UserEffectDecision.RETRY_NEW_INVOCATION,
            new_invocation_id="unsafe-new",
        )
    assert denied.value.code == "unsafe_retry"


def test_checkpoint_rebuild_hash_diagnostics_schema_migration_and_artifact_integrity(tmp_path: Path):
    _, guard, _, token = _lease()
    durable = ReferenceDurableSessionStore()
    durable.commit(session_id="session", event_id="legacy", facts=(SemanticFact(
        FactKind.PLAN, "plan-1", "active", {"schema_version": 0, "old_summary": "legacy"},
    ),))
    workspace = SessionWorkspace.open(tmp_path / "workspace", tenant_id="tenant", session_id="session")
    metadata = ArtifactMetadataStore(guard)
    artifacts = ArtifactService(
        workspace, guard, metadata, QuotaLedger(),
        QuotaPolicy(100_000, 100_000, 10, 100_000, 10_000),
    )
    artifacts.write(
        token, artifact_id="artifact-1", category="generated", chunks=(b"content",),
        mime_type="text/plain", source={"test": "recovery"},
    )
    durable.commit(session_id="session", event_id="artifact", facts=(SemanticFact(
        FactKind.ARTIFACT_REF, "artifact-1", "retained", {"sha256": metadata.get("artifact-1").sha256},
    ),))
    checkpoint_store = CheckpointStore(workspace, guard)
    checkpoint_store.write(
        token, durable_cursor=str(durable.cursor("session")),
        hermes_snapshot_hash="h" * 64,
        artifact_manifest_hash=artifacts.manifest_hash(),
        state={"safe": True, "pid": 9, "browser_handle": "dead"},
    )
    reconciler = RecoveryReconciler(
        durable, guard, fact_schema_version=1,
        migrations={(FactKind.PLAN, 0): lambda payload: {"summary": payload["old_summary"]}},
    )
    migrated = reconciler.recover(
        token, checkpoint_store=checkpoint_store,
        hermes_snapshot_hash="h" * 64,
        artifact_manifest_hash=artifacts.manifest_hash(),
        artifact_verify=lambda artifact_id: artifacts.verify_integrity(token, artifact_id),
    )
    assert migrated.checkpoint_disposition is CheckpointDisposition.REBUILD
    checkpoint_store.write(
        token, durable_cursor=str(durable.cursor("session")),
        hermes_snapshot_hash="h" * 64,
        artifact_manifest_hash=artifacts.manifest_hash(),
        state={"safe": True, "pid": 9, "browser_handle": "dead"},
    )
    healthy = reconciler.recover(
        token, checkpoint_store=checkpoint_store,
        hermes_snapshot_hash="h" * 64,
        artifact_manifest_hash=artifacts.manifest_hash(),
        artifact_verify=lambda artifact_id: artifacts.verify_integrity(token, artifact_id),
    )
    assert healthy.checkpoint_disposition is CheckpointDisposition.ACCEPTED
    assert healthy.checkpoint.state == {"safe": True}
    plan = next(item for item in healthy.items if item.entity_id == "plan-1")
    assert plan.payload["summary"] == "legacy" and plan.payload["schema_version"] == 1
    assert any(item.code == "fact_migrated" for item in healthy.diagnostics)
    assert any(
        stored.fact.kind is FactKind.OBSERVATION and stored.fact.state == "migrated"
        for stored in durable.load("session")
    )

    rebuilt = reconciler.recover(
        token, checkpoint_store=checkpoint_store, hermes_snapshot_hash="x" * 64,
    )
    assert rebuilt.checkpoint_disposition is CheckpointDisposition.REBUILD
    assert any(item.code == "hermes_snapshot_hash_mismatch" for item in rebuilt.diagnostics)

    record = metadata.get("artifact-1")
    path = workspace.path_for(f"artifacts/{record.category}/{record.sha256[:2]}/{record.sha256}")
    path.write_bytes(b"tampered")
    corrupt = reconciler.recover(
        token, artifact_verify=lambda artifact_id: artifacts.verify_integrity(token, artifact_id),
    )
    assert corrupt.checkpoint_disposition is CheckpointDisposition.BLOCKED
    assert any(item.code == "artifact_hash_mismatch" for item in corrupt.diagnostics)
    artifact = next(item for item in corrupt.items if item.entity_id == "artifact-1")
    assert artifact.classification is RecoveryClassification.CORRUPTED


def test_waiting_approval_restores_version_scope_deadline_and_user_choices():
    _, guard, _, token = _lease()
    durable = ReferenceDurableSessionStore()
    first = InteractionController(durable)
    first.register_run(
        tenant_id="tenant", user_id="user", session_id="session", run_id="run-1",
        runnable_items=("apply",),
    )
    now = datetime(2026, 9, 18, tzinfo=timezone.utc)
    first.request_approval(
        session_id="session", run_id="run-1", interaction_id="approval-1",
        item_id="apply", version=3, expected_run_version=1,
        tool_name="network.change", arguments={"target": "edge-1"},
        arguments_summary="update edge-1", risk="write", scope=("edge-1",),
        reason="change requires approval", blocked_items=("apply",),
        timeout_seconds=60, now=now,
    )

    replacement = InteractionController(durable)
    restored = replacement.restore_waiting(
        tenant_id="tenant", user_id="user", session_id="session", run_id="run-1",
        now=now + timedelta(seconds=10),
    )
    assert restored.status == RunStatus.WAITING
    assert restored.requests[0].version == 3
    assert restored.requests[0].approval.scope == ("edge-1",)
    with pytest.raises(InteractionError) as stale:
        replacement.resolve_approval(
            session_id="session", run_id="run-1", interaction_id="approval-1",
            interaction_version=2, expected_run_version=1,
            resolution_id="wrong-version", decision=ApprovalDecision.APPROVED,
            now=now + timedelta(seconds=20),
        )
    assert stale.value.code == "stale_interaction_version"
    resolved = replacement.resolve_approval(
        session_id="session", run_id="run-1", interaction_id="approval-1",
        interaction_version=3, expected_run_version=1,
        resolution_id="approved", decision=ApprovalDecision.APPROVED,
        now=now + timedelta(seconds=20),
    )
    assert resolved.resumed_items == ("apply",)

    reconciler = RecoveryReconciler(durable, guard)
    choices = (
        ("run-continue", WaitingRecoveryChoice.CONTINUE),
        ("run-cancel", WaitingRecoveryChoice.CANCEL),
        ("run-start", WaitingRecoveryChoice.START_LINKED_RUN),
    )
    durable.commit(session_id="session", event_id="waiting-choice-runs", facts=tuple(
        SemanticFact(FactKind.RUN, choice_run_id, RunStatus.WAITING)
        for choice_run_id, _ in choices
    ))
    for index, (choice_run_id, choice) in enumerate(choices, start=1):
        fact = reconciler.record_waiting_choice(
            token, run_id=choice_run_id, choice=choice, decision_id=f"decision-{index}",
            linked_run_id="run-2" if choice is WaitingRecoveryChoice.START_LINKED_RUN else None,
        )
        assert fact.payload["choice"] == choice
    run_facts = [item.fact for item in durable.load("session") if item.fact.kind is FactKind.RUN]
    assert any(item.entity_id == "run-cancel" and item.state == RunStatus.CANCELLED for item in run_facts)
    assert any(item.entity_id == "run-2" and item.state == RunStatus.RUNNING for item in run_facts)
    with pytest.raises(RecoveryError) as conflict:
        reconciler.record_waiting_choice(
            token, run_id="run-cancel", choice=WaitingRecoveryChoice.START_LINKED_RUN,
            decision_id="decision-conflict", linked_run_id="run-3",
        )
    assert conflict.value.code == "recovery_choice_conflict"


def test_expired_waiting_approval_is_revalidated_and_blocked_on_restore():
    durable = ReferenceDurableSessionStore()
    controller = InteractionController(durable)
    controller.register_run(
        tenant_id="tenant", user_id="user", session_id="session", run_id="run-expired",
        runnable_items=("apply",),
    )
    now = datetime(2026, 9, 18, tzinfo=timezone.utc)
    controller.request_approval(
        session_id="session", run_id="run-expired", interaction_id="approval-expired",
        item_id="apply", version=1, expected_run_version=1,
        tool_name="network.change", arguments={"target": "edge-1"},
        arguments_summary="update edge-1", risk="write", scope=("edge-1",),
        reason="approval required", blocked_items=("apply",), timeout_seconds=5, now=now,
    )

    restored = InteractionController(durable).restore_waiting(
        tenant_id="tenant", user_id="user", session_id="session", run_id="run-expired",
        now=now + timedelta(seconds=6),
    )

    assert restored.status == RunStatus.BLOCKED
    assert restored.requests == ()
    latest_approval = [
        item.fact for item in durable.load("session")
        if item.fact.kind is FactKind.APPROVAL and item.fact.entity_id == "approval-expired"
    ][-1]
    assert latest_approval.state == "expired"


def test_takeover_a_to_b_to_a_fences_old_recovery_and_reconciliation_writes():
    authority, guard, a1, token_a1 = _lease()
    durable = ReferenceDurableSessionStore()
    contract = _contract()
    durable.commit(session_id="session", event_id="unknown", facts=(SemanticFact(
        FactKind.INVOCATION, "inv-1", InvocationStatus.UNKNOWN, _intent(contract),
    ),))
    b = authority.acquire(
        session_id="session", owner_id="owner-b", policy=a1.policy,
        expected_lease_version=a1.lease_version,
        expected_execution_epoch=a1.execution_epoch,
    )
    token_b = authority.token(b)
    with pytest.raises(EpochError):
        RecoveryReconciler(durable, guard).recover(token_a1)
    with pytest.raises(EpochError):
        SideEffectReconciler(durable, guard).reconcile(
            token_a1, invocation_id="inv-1", contract=contract,
        )
    assert RecoveryReconciler(durable, guard).recover(token_b).execution_epoch == 2

    a2 = authority.acquire(
        session_id="session", owner_id="owner-a", policy=b.policy,
        expected_lease_version=b.lease_version,
        expected_execution_epoch=b.execution_epoch,
    )
    with pytest.raises(EpochError):
        RecoveryReconciler(durable, guard).recover(token_b)
    assert RecoveryReconciler(durable, guard).recover(authority.token(a2)).execution_epoch == 3
