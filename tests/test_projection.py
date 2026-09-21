from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from networkclaw_harness.projection import (
    ArtifactReferenceProjector,
    AuditContext,
    AuditError,
    AuditPolicy,
    AuditService,
    DeltaCoalescer,
    EventProjector,
    EventVisibilityPolicy,
    ProjectionContext,
    ProjectionError,
    ReferenceAuditStore,
    VisibilityMode,
    VisibilityRule,
    project_mapping,
)
from networkclaw_harness.runtime import (
    FactKind,
    ReferenceDurableSessionStore,
    RunStatus,
    SemanticFact,
)
from networkclaw_harness.workspace import ArtifactMetadata


def _context(**changes):
    values = {
        "tenant_id": "tenant-1",
        "user_id": "user-1",
        "session_id": "session-1",
        "profile": "customer",
        "secret_values": ("configured-secret",),
    }
    values.update(changes)
    return ProjectionContext(**values)


def _commit(store, event_id, *facts):
    return store.commit(session_id="session-1", event_id=event_id, facts=facts)


def test_projection_redacts_nested_secrets_but_preserves_hashes_and_usage():
    projected = project_mapping(
        {
            "tool": "web.fetch",
            "authorization": "Bearer secret",
            "nested": {
                "system_prompt": "hidden",
                "items": [{"password": "hidden"}, "configured-secret"],
                "status": "completed",
            },
            "authorization_hash": "sha256:visible",
            "token_count": 42,
            "blob": b"raw",
        },
        secret_values=("configured-secret",),
    )

    assert projected == {
        "tool": "web.fetch",
        "authorization": "[redacted]",
        "nested": {
            "system_prompt": "[redacted]",
            "items": [{"password": "[redacted]"}, "[redacted]"],
            "status": "completed",
        },
        "authorization_hash": "sha256:visible",
        "token_count": 42,
        "blob": "[binary omitted]",
    }


def test_visibility_rules_use_event_tool_tenant_profile_and_most_specific_match():
    policy = EventVisibilityPolicy((
        VisibilityRule("tool.completed", VisibilityMode.SUMMARY),
        VisibilityRule("tool.completed", VisibilityMode.ARTIFACT_ONLY, tool_name="shell"),
        VisibilityRule(
            "tool.completed", VisibilityMode.HIDDEN, tool_name="shell",
            tenant_id="tenant-1", profile="customer",
        ),
        VisibilityRule("*", VisibilityMode.SUMMARY, tenant_id="tenant-2"),
    ))

    assert policy.decide(
        event_type="tool.completed", tool_name="shell",
        tenant_id="tenant-1", profile="customer",
    ) is VisibilityMode.HIDDEN
    assert policy.decide(
        event_type="tool.completed", tool_name="shell",
        tenant_id="tenant-1", profile="development",
    ) is VisibilityMode.ARTIFACT_ONLY
    assert policy.decide(
        event_type="tool.completed", tool_name="read",
        tenant_id="tenant-1", profile="customer",
    ) is VisibilityMode.SUMMARY
    assert policy.decide(
        event_type="warning", tenant_id="tenant-2", profile="customer",
    ) is VisibilityMode.SUMMARY


def test_event_projection_covers_client_event_matrix_and_stable_identities():
    durable = ReferenceDurableSessionStore()
    _commit(durable, "start", SemanticFact(FactKind.RUN, "run-1", RunStatus.RUNNING))
    _commit(durable, "plan", SemanticFact(FactKind.PLAN, "plan-1", "updated", {
        "run_id": "run-1", "summary": "Inspect then act", "system_prompt": "hidden",
    }))
    _commit(durable, "tool-start", SemanticFact(FactKind.INVOCATION, "inv-1", "running", {
        "run_id": "run-1", "action_type": "tool", "tool_name": "shell",
        "arguments": {"command": "cat /secret"},
    }))
    _commit(durable, "tool-end", SemanticFact(FactKind.OUTCOME, "inv-1", "observed", {
        "run_id": "run-1", "action_type": "tool", "tool_name": "shell",
        "summary": "Command completed", "artifact_id": "artifact-1",
        "result": {"stdout": "configured-secret", "raw": "large output"},
    }))
    _commit(durable, "subagent", SemanticFact(FactKind.OUTCOME, "sub-1", "observed", {
        "run_id": "run-1", "action_type": "subagent", "summary": "Checked dependency",
    }))
    _commit(durable, "interaction", SemanticFact(FactKind.INTERACTION, "interaction-1", "pending", {
        "run_id": "run-1", "kind": "approval", "item_id": "inv-1", "summary": "Approve change",
    }))
    _commit(durable, "artifact", SemanticFact(FactKind.ARTIFACT_REF, "artifact-1", "retained", {
        "run_id": "run-1", "name": "output.txt", "size": 2048,
    }))
    _commit(durable, "warning", SemanticFact(FactKind.UNRESOLVED_ITEM, "gap-1", "open", {
        "run_id": "run-1", "summary": "Verification pending",
    }))
    _commit(durable, "error", SemanticFact(FactKind.OBSERVATION, "error-1", "error", {
        "run_id": "run-1", "message": "Provider unavailable",
    }))
    _commit(durable, "delivery", SemanticFact(FactKind.DELIVERY, "run-1", "delivered", {
        "content": "Final answer", "goal_complete": True,
    }))
    _commit(durable, "terminal", SemanticFact(FactKind.RUN, "run-1", RunStatus.COMPLETED))

    projector = EventProjector(durable)
    first = projector.project(_context())
    second = projector.project(_context())
    by_type = {event.type: event for event in first}

    assert {
        "turn.started", "plan.updated", "tool.started", "tool.completed",
        "subagent.completed", "approval.requested", "artifact.created",
        "warning", "error", "assistant.delta", "turn.completed",
    } <= set(by_type)
    assert [event.event_id for event in first] == [event.event_id for event in second]
    assert all(event.session_id == "session-1" for event in first)
    assert all(event.run_id == "run-1" for event in first)
    assert all(
        event.item_id or event.invocation_id or event.interaction_id or event.artifact_id
        for event in first
    )
    assert by_type["tool.completed"].invocation_id == "inv-1"
    assert by_type["artifact.created"].artifact_id == "artifact-1"
    assert by_type["assistant.delta"].payload["content"] == "Final answer"
    assert by_type["turn.completed"].terminal is True
    encoded = json.dumps([dict(event.payload) for event in first], sort_keys=True)
    assert "configured-secret" not in encoded
    assert "cat /secret" not in encoded
    assert "large output" not in encoded
    assert "hidden" not in encoded


def test_projection_cursor_suffix_limits_and_late_facts_cannot_reactivate_run():
    durable = ReferenceDurableSessionStore()
    _commit(durable, "start", SemanticFact(FactKind.RUN, "run-1", RunStatus.RUNNING))
    terminal = _commit(
        durable, "terminal", SemanticFact(FactKind.RUN, "run-1", RunStatus.COMPLETED),
    )
    _commit(durable, "late", SemanticFact(FactKind.PLAN, "plan-late", "updated", {
        "run_id": "run-1", "summary": "must not resume",
    }))
    projector = EventProjector(durable)

    full = projector.project(_context())
    incremental = projector.project(_context(), after_cursor=terminal.cursor)

    assert incremental == tuple(event for event in full if int(event.cursor) > terminal.cursor)
    assert [event.type for event in incremental] == ["warning"]
    assert incremental[0].payload["code"] == "late_fact_ignored"
    assert incremental[0].run_id == "run-1"

    with pytest.raises(ProjectionError) as backpressure:
        EventProjector(durable, max_events=1).project(_context())
    assert backpressure.value.code == "backpressure"

    _commit(durable, "large", SemanticFact(FactKind.OBSERVATION, "large", "warning", {
        "run_id": "run-new", "message": "x" * 1000,
    }))
    bounded = EventProjector(durable, max_payload_bytes=128).project(_context())[-1]
    assert bounded.payload["content_truncated"] is True


def test_projection_drops_finalizer_prompts_reasoning_and_raw_diagnostics_recursively():
    durable = ReferenceDurableSessionStore()
    _commit(durable, "start", SemanticFact(FactKind.RUN, "run-1", RunStatus.RUNNING))
    _commit(durable, "delivery", SemanticFact(FactKind.DELIVERY, "run-1", "delivered", {
        "content": "Safe final answer",
        "summary": "Safe summary",
        "internal_finalizer_prompt": "private prompt",
        "reasoning": "private reasoning",
        "provider_metadata": {
            "provider": "example",
            "raw_diagnostics": "private wire dump",
            "nested": {"chain_of_thought": "private chain", "attempt": 2},
        },
    }))
    _commit(durable, "terminal", SemanticFact(FactKind.RUN, "run-1", RunStatus.COMPLETED, {
        "summary": "Completed",
        "debug_trace": "private trace",
    }))

    events = EventProjector(durable).project(_context())
    encoded = json.dumps([dict(event.payload) for event in events], sort_keys=True)

    assert "Safe final answer" in encoded
    assert '"provider": "example"' in encoded
    assert '"attempt": 2' in encoded
    assert "private prompt" not in encoded
    assert "private reasoning" not in encoded
    assert "private wire dump" not in encoded
    assert "private chain" not in encoded
    assert "private trace" not in encoded


@pytest.mark.parametrize("fact", (
    SemanticFact(FactKind.RUN, "run-1", RunStatus.RUNNING),
    SemanticFact(FactKind.RUN, "run-1", RunStatus.WAITING),
    SemanticFact(FactKind.PLAN, "late-plan", "updated", {"run_id": "run-1"}),
    SemanticFact(FactKind.INVOCATION, "late-tool", "running", {"run_id": "run-1"}),
    SemanticFact(FactKind.OUTCOME, "late-tool", "observed", {
        "run_id": "run-1", "action_type": "tool", "summary": "late callback",
    }),
    SemanticFact(FactKind.INTERACTION, "late-approval", "pending", {"run_id": "run-1"}),
    SemanticFact(FactKind.DELIVERY, "run-1", "delivered", {"content": "duplicate"}),
))
def test_every_late_callback_shape_that_could_activate_a_terminal_run_is_fenced(fact):
    durable = ReferenceDurableSessionStore()
    _commit(durable, "start", SemanticFact(FactKind.RUN, "run-1", RunStatus.RUNNING))
    _commit(durable, "terminal", SemanticFact(FactKind.RUN, "run-1", RunStatus.COMPLETED))
    _commit(durable, "late-callback", fact)

    events = EventProjector(durable).project(_context())
    late = events[-1]

    assert late.type == "warning"
    assert late.payload["code"] == "late_fact_ignored"
    assert late.run_id == "run-1"
    assert not any(event.type in {
        "turn.started", "plan.updated", "tool.started", "subagent.started",
        "approval.requested", "clarification.requested",
    } for event in events[2:])


def test_recovered_interrupted_run_projects_a_terminal_failure_not_active_execution():
    durable = ReferenceDurableSessionStore()
    _commit(durable, "old", SemanticFact(FactKind.RUN, "run-1", RunStatus.RUNNING))
    _commit(durable, "recovery", SemanticFact(FactKind.RUN, "run-1", RunStatus.INTERRUPTED, {
        "recovered": True,
    }))

    events = EventProjector(durable).project(_context())

    assert [event.type for event in events] == ["turn.started", "turn.failed"]
    assert events[-1].terminal is True
    assert events[-1].payload["status"] == RunStatus.INTERRUPTED


def test_delta_coalescing_preserves_batches_final_body_and_all_gap_markers():
    coalescer = DeltaCoalescer(max_chars=3, max_chunks=8)
    assert coalescer.add(1, "ab") is None
    first = coalescer.add(3, "c")
    assert first is not None
    assert (first.content, first.first_sequence, first.last_sequence, first.gaps, first.final) == (
        "abc", 1, 3, (2,), False,
    )
    assert coalescer.add(5, "d") is None
    batches = coalescer.finish("authoritative final body")
    assert batches[0].content == "d" and batches[0].gaps == (4,)
    assert batches[-1].content == "authoritative final body"
    assert batches[-1].gaps == (2, 4) and batches[-1].final is True
    with pytest.raises(ProjectionError) as out_of_order:
        coalescer.add(1, "old")
    assert out_of_order.value.code == "delta_out_of_order"


class _MetadataStore:
    def __init__(self, *records: ArtifactMetadata) -> None:
        self.records = {record.artifact_id: record for record in records}

    def get(self, artifact_id):
        return self.records.get(artifact_id)


def _artifact(artifact_id="artifact-1", **changes):
    values = {
        "artifact_id": artifact_id,
        "tenant_id": "tenant-1",
        "session_id": "session-1",
        "category": "raw",
        "sha256": "a" * 64,
        "size": 4096,
        "mime_type": "text/plain",
        "source": {"name": "command.txt", "tool": "shell", "authorization": "secret"},
        "references": ("inv-1",),
        "lifecycle": "retained",
        "created_at": "2026-09-17T00:00:00Z",
        "content_available": True,
    }
    values.update(changes)
    return ArtifactMetadata(**values)


def test_artifact_projection_is_metadata_only_authorized_and_reports_access_states():
    raw = _artifact()
    expired = _artifact("expired", lifecycle="expired")
    deleted = _artifact("deleted", content_available=False)
    normalized = _artifact("normalized", category="normalized", source={
        "name": "safe.txt", "note": "configured-secret",
    })
    projector = ArtifactReferenceProjector(_MetadataStore(raw, expired, deleted, normalized))

    restricted = projector.project(
        tenant_id="tenant-1", session_id="session-1", artifact_id="artifact-1",
    )
    available = projector.project(
        tenant_id="tenant-1", session_id="session-1", artifact_id="artifact-1",
        authorized=True,
    )
    safe = projector.project(
        tenant_id="tenant-1", session_id="session-1", artifact_id="normalized",
        authorized=True, secret_values=("configured-secret",),
    )

    assert restricted.status == "restricted" and restricted.view_ref is None
    assert available.status == "available" and available.view_ref == "artifact:artifact-1"
    assert available.name == "command.txt" and available.size == 4096
    assert available.source["authorization"] == "[redacted]"
    assert safe.source["note"] == "[redacted]"
    assert projector.project(
        tenant_id="tenant-1", session_id="session-1", artifact_id="expired",
    ).status == "expired"
    assert projector.project(
        tenant_id="tenant-1", session_id="session-1", artifact_id="deleted",
    ).status == "unavailable"
    assert projector.project(
        tenant_id="other", session_id="session-1", artifact_id="artifact-1",
        authorized=True,
    ).status == "not_found"
    assert projector.project(
        tenant_id="tenant-1", session_id="other", artifact_id="artifact-1",
        authorized=True,
    ).status == "not_found"
    assert "content" not in available.__dataclass_fields__


def test_audit_capture_export_access_retention_and_diagnostics_are_safe():
    durable = ReferenceDurableSessionStore()
    _commit(durable, "intent", SemanticFact(FactKind.INVOCATION, "inv-1", "running", {
        "run_id": "run-1", "tool_name": "shell", "arguments_hash": "sha256:args",
        "authorization": "configured-secret", "system_prompt": "hidden",
    }))
    first = durable.cursor("session-1")
    _commit(durable, "approval", SemanticFact(FactKind.APPROVAL, "approval-1", "approved", {
        "run_id": "run-1", "approval_id": "approval-1", "approval_version": 2,
        "authorization_hash": "sha256:auth",
    }))
    _commit(durable, "result", SemanticFact(FactKind.OUTCOME, "inv-1", "observed", {
        "run_id": "run-1", "execution_status": "succeeded", "business_success": True,
        "evidence_refs": ["evidence-1", "configured-secret"],
        "result": {"stdout": "configured-secret", "chain_of_thought": "hidden"},
    }))
    store = ReferenceAuditStore()
    service = AuditService(
        durable, store, AuditPolicy(retention_days=30, max_export_records=10),
    )
    old = datetime(2026, 7, 1, tzinfo=timezone.utc)
    context = AuditContext(
        tenant_id="tenant-1", user_id="user-1", session_id="session-1",
        actor="host:chatsvc", execution_epoch=7, request_id="request-1",
        run_id="run-1", secret_values=("configured-secret",),
    )
    service.capture(context, now=old)

    with pytest.raises(AuditError) as denied:
        service.export(tenant_id="tenant-1", role="member")
    assert denied.value.code == "audit_access_denied"

    exported = service.export(
        tenant_id="tenant-1", role="tenant_auditor", session_id="session-1",
    )
    rows = [json.loads(line) for line in exported.splitlines()]
    assert len(rows) == 3
    assert rows[0] | {
        "tenant_id": "tenant-1", "user_id": "user-1", "actor": "host:chatsvc",
        "execution_epoch": 7, "request_id": "request-1", "run_id": "run-1",
    } == rows[0]
    assert rows[0]["details"]["tool_name"] == "shell"
    assert rows[1]["details"]["approval_version"] == 2
    assert rows[2]["details"]["business_success"] is True
    assert "configured-secret" not in exported
    assert "system_prompt" not in exported
    assert "chain_of_thought" not in exported
    assert "stdout" not in exported
    assert rows[2]["evidence_refs"] == ["evidence-1", "[redacted]"]

    snapshot = service.diagnostic_snapshot(
        tenant_id="tenant-1", role="support_diagnostics", session_id="session-1",
    )
    assert snapshot["record_count"] == 3
    assert snapshot["latest_cursor"] == 3
    assert snapshot["actions"]["approval.approved"] == 1
    assert service.export(tenant_id="other", role="tenant_auditor") == ""

    assert len(service.capture(context, after_cursor=first, now=old)) == 2
    other_context = replace(context, tenant_id="tenant-2")
    service.capture(other_context, now=old)
    assert len(store.query("tenant-2", "session-1")) == 3
    assert service.enforce_retention(now=old + timedelta(days=31)) == 6


def test_audit_export_limit_fails_closed():
    durable = ReferenceDurableSessionStore()
    _commit(durable, "one", SemanticFact(FactKind.RUN, "run-1", RunStatus.RUNNING))
    _commit(durable, "two", SemanticFact(FactKind.RUN, "run-1", RunStatus.COMPLETED))
    service = AuditService(
        durable, ReferenceAuditStore(),
        AuditPolicy(retention_days=30, max_export_records=1),
    )
    service.capture(AuditContext(
        tenant_id="tenant-1", user_id="user-1", session_id="session-1",
        actor="host", execution_epoch=1,
    ))
    with pytest.raises(AuditError) as too_large:
        service.export(tenant_id="tenant-1", role="tenant_auditor")
    assert too_large.value.code == "audit_export_too_large"
