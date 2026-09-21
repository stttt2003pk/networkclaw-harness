from networkclaw_harness.projection import (
    SEMANTIC_SCHEMA_VERSION,
    TIMELINE_SCHEMA_VERSION,
    TimelineProjector,
    versioned_fact,
)
from networkclaw_harness.runtime import FactKind, ReferenceDurableSessionStore, RunStatus, SemanticFact


def commit(store, event_id, fact):
    return store.commit(session_id="session", event_id=event_id, facts=(fact,))


def test_versioned_fact_is_bounded_and_redacted():
    envelope = versioned_fact(SemanticFact(FactKind.GRACE_SUMMARY, "g", "completed", {
        "summary": "safe", "authorization": "secret", "content": "x" * 1000,
    }), secret_values=("secret",), max_payload_bytes=100)
    assert envelope.schema_version == SEMANTIC_SCHEMA_VERSION
    assert envelope.redacted_payload["content_truncated"] is True
    assert "secret" not in str(envelope.redacted_payload)


def test_timeline_identity_merge_and_terminal_late_fencing():
    store = ReferenceDurableSessionStore()
    commit(store, "start", SemanticFact(FactKind.RUN, "run", RunStatus.RUNNING))
    commit(store, "retry", SemanticFact(FactKind.PROVIDER_ATTEMPT, "attempt", "retrying", {
        "run_id": "run", "attempt": 2, "raw_response": "private",
    }))
    terminal = commit(store, "done", SemanticFact(FactKind.RUN, "run", RunStatus.COMPLETED))
    commit(store, "late", SemanticFact(FactKind.TOOL_ROUND, "round", "started", {"run_id": "run"}))
    projector = TimelineProjector()
    full = projector.project(store.load("session"))
    suffix = projector.project(store.load("session"), after_cursor=terminal.cursor)
    assert TIMELINE_SCHEMA_VERSION == full[0].schema_version
    assert any(item.stage == "provider_retry" for item in full)
    assert suffix[-1].reason == "late_fact_ignored"
    assert projector.merge(full, full) == full
    assert all("private" not in str(item.payload) for item in full)
