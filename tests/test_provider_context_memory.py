from __future__ import annotations

from collections.abc import Iterator

import pytest

from networkclaw_harness.runtime import (
    CompactionRequest,
    CompactionResult,
    CompactionService,
    ContextAssembler,
    ContextBudget,
    ContextError,
    ContextItem,
    FactKind,
    HermesCompactionAdapter,
    MemoryError,
    MemoryPolicy,
    MemoryRecord,
    MemoryScope,
    PromptCacheRegistry,
    PromptConfiguration,
    ProviderChunk,
    ProviderConfigRef,
    ProviderError,
    ProviderRateLimited,
    ProviderRequest,
    ProviderRuntime,
    ReferenceDurableSessionStore,
    ReferenceHermesCompaction,
    ReferenceMemoryStore,
    ReferenceProviderResolver,
    TranscriptMessage,
)


class FakeProvider:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.requests = []

    def stream(self, request, *, model_id: str, timeout_ms: int) -> Iterator[ProviderChunk]:
        self.requests.append((request, model_id, timeout_ms))
        response = next(self.responses)
        if isinstance(response, Exception):
            raise response
        yield from response


def request(session_id: str = "session-a") -> ProviderRequest:
    return ProviderRequest(
        session_id=session_id, system_prompt=b"stable system prompt",
        messages=({"role": "user", "content": "hello"},),
        prompt_cache_key="cache-key", max_output_tokens=100,
    )


def config(provider: str, reference: str, model: str = "model") -> ProviderConfigRef:
    return ProviderConfigRef(provider, model, reference, timeout_ms=250, max_attempts=2)


def test_provider_streaming_retry_fallback_usage_and_reference_only_config():
    primary = config("primary", "host-config-primary")
    fallback = config("fallback", "host-config-fallback", "backup-model")
    primary_client = FakeProvider([ProviderRateLimited(), ProviderRateLimited()])
    fallback_client = FakeProvider([(
        ProviderChunk("hel", {"input_tokens": 12}),
        ProviderChunk("lo", {"output_tokens": 2}, "stop"),
    )])
    resolver = ReferenceProviderResolver(
        lambda value: primary_client if value.provider_id == "primary" else fallback_client
    )
    runtime = ProviderRuntime(resolver)
    deltas = []
    result = runtime.invoke(request(), primary=primary, fallbacks=(fallback,), on_chunk=lambda chunk: deltas.append(chunk.delta))
    assert result.text == "hello"
    assert deltas == ["hel", "lo"]
    assert result.usage == {"input_tokens": 12, "output_tokens": 2}
    assert result.config == fallback and result.attempts == 1
    assert result.route_snapshot is not None
    assert [item.outcome for item in result.attempt_ledger] == ["rate_limited", "rate_limited", "succeeded"]
    assert runtime.client_count == 2
    assert all("key" not in field for field in primary.__dataclass_fields__)
    assert primary_client.requests[0][0].session_id == "session-a"


def test_provider_clients_reuse_by_host_config_without_cross_session_request_state():
    shared = config("provider", "host-config")
    alternate = config("provider", "other-host-config")
    clients = {
        "host-config": FakeProvider([(
            ProviderChunk("a"),
        ), (ProviderChunk("b"),)]),
        "other-host-config": FakeProvider([(ProviderChunk("c"),)]),
    }
    resolver = ReferenceProviderResolver(lambda value: clients[value.config_ref])
    runtime = ProviderRuntime(resolver)
    assert runtime.invoke(request("a"), primary=shared).text == "a"
    assert runtime.invoke(request("b"), primary=shared).text == "b"
    assert runtime.invoke(request("a"), primary=alternate).text == "c"
    assert runtime.client_count == 2
    assert [entry[0].session_id for entry in clients["host-config"].requests] == ["a", "b"]


def test_provider_failure_reports_no_prompt_or_credential_material():
    resolver = ReferenceProviderResolver(lambda value: FakeProvider([ProviderError("bad", "backend failed")]))
    runtime = ProviderRuntime(resolver)
    with pytest.raises(ProviderError) as failed:
        runtime.invoke(request(), primary=config("provider", "credential-reference"))
    assert failed.value.code == "provider_unavailable"
    assert "hello" not in str(failed.value)
    assert "credential-reference" not in str(failed.value)


def test_partially_projected_provider_stream_is_not_retried_or_fallen_back():
    class PartialProvider:
        def stream(self, request, *, model_id, timeout_ms):
            yield ProviderChunk("visible")
            raise ProviderRateLimited()

    fallback = FakeProvider([(ProviderChunk("duplicate"),)])
    resolver = ReferenceProviderResolver(
        lambda value: PartialProvider() if value.provider_id == "primary" else fallback
    )
    with pytest.raises(ProviderError) as interrupted:
        ProviderRuntime(resolver).invoke(
            request(), primary=config("primary", "primary-ref"),
            fallbacks=(config("fallback", "fallback-ref"),), on_chunk=lambda chunk: None,
        )
    assert interrupted.value.code == "provider_stream_interrupted"
    assert fallback.requests == []


def test_prompt_prefix_is_byte_stable_and_tools_or_skills_are_frozen_per_session():
    registry = PromptCacheRegistry()
    configuration = PromptConfiguration(b"system\nexact", ("skill-a",), "a" * 64)
    assembler = ContextAssembler()
    budget = ContextBudget(100, 20, 10, 20, 20, 5)
    first = assembler.assemble(session_id="session", configuration=configuration, registry=registry, budget=budget, items=())
    second = assembler.assemble(session_id="session", configuration=configuration, registry=registry, budget=budget, items=())
    assert first.system_prompt == second.system_prompt == b"system\nexact"
    assert first.cache_key == second.cache_key
    with pytest.raises(ContextError) as changed:
        assembler.assemble(
            session_id="session", configuration=PromptConfiguration(b"system\nexact", ("skill-b",), "a" * 64),
            registry=registry, budget=budget, items=(),
        )
    assert changed.value.code == "prompt_prefix_changed"
    registry.close("session")
    assert registry.bind("new-session", PromptConfiguration(b"system\nexact", ("skill-b",), "a" * 64))


def test_context_budget_uses_summaries_and_requires_selection_for_critical_facts():
    registry = PromptCacheRegistry()
    config_value = PromptConfiguration(b"system", (), "b" * 64)
    budget = ContextBudget(100, 20, 10, 20, 20, 5)
    items = (
        ContextItem("history", "history", "recent", 5, {"source": "history"}, critical=True),
        ContextItem("large-artifact", "artifact", "full report", 40, {"artifact_id": "a"}, summary="brief report", summary_tokens=10),
        ContextItem("optional", "attachment", "large", 30, {"attachment": "x"}),
    )
    assembled = ContextAssembler().assemble(
        session_id="session", configuration=config_value, registry=registry, budget=budget, items=items,
    )
    assert [item.item_id for item in assembled.items] == ["history", "large-artifact"]
    assert assembled.used_summaries == ("large-artifact",)
    assert assembled.omitted_items == ("optional",)
    with pytest.raises(ContextError) as selection:
        ContextAssembler().assemble(
            session_id="other", configuration=config_value, registry=registry, budget=budget,
            items=(ContextItem("critical", "plan", "required", 40, {"plan": "p"}, critical=True),),
        )
    assert selection.value.code == "context_selection_required"


def compaction_request() -> CompactionRequest:
    return CompactionRequest(
        session_id="session", input_start=1, input_end=5,
        messages=(
            TranscriptMessage("user", "inspect", "user_interaction"),
            TranscriptMessage("assistant", None, "model_step", tool_calls=("call",)),
            TranscriptMessage("tool", "result", "tool_round", tool_call_id="call", tool_name="inspect"),
            TranscriptMessage("assistant", "continuing", "model_step"),
        ),
        unresolved_ids=("interaction",), unknown_invocation_ids=("unknown-tool",), source_refs=("artifact-1",),
    )


def test_compaction_records_version_range_artifact_and_preserves_unknown_state():
    durable = ReferenceDurableSessionStore()
    artifacts = []
    service = CompactionService(
        durable, ReferenceHermesCompaction(),
        artifact_commit=lambda request, result: artifacts.append((request.session_id, result.summary_artifact_id)),
    )
    result = service.compact(compaction_request(), event_id="compact-1")
    fact = durable.load("session")[0].fact
    assert artifacts == [("session", result.summary_artifact_id)]
    assert fact.kind is FactKind.COMPACTION and fact.payload["version"] == "hermes-reference-v1"
    assert fact.payload["input_start"] == 1 and fact.payload["input_end"] == 5
    assert fact.payload["unknown_invocation_ids"] == ["unknown-tool"]
    assert result.retained_messages[-1].role == "assistant"


def test_compaction_rejects_lost_unknown_or_unresolved_facts_before_artifact_write():
    class LosingCompactor:
        def compact(self, request):
            return CompactionResult("bad", "summary", "artifact", (), (), (), ())

    artifacts = []
    service = CompactionService(
        ReferenceDurableSessionStore(), LosingCompactor(),
        artifact_commit=lambda request, result: artifacts.append(result.summary_artifact_id),
    )
    with pytest.raises(ContextError) as failed:
        service.compact(compaction_request(), event_id="compact-bad")
    assert failed.value.code == "compaction_unknown_lost"
    assert artifacts == []


def test_hermes_compaction_is_injected_through_a_narrow_runtime_adapter():
    class Runtime:
        def __init__(self):
            self.request = None

        def compact_session(self, request):
            self.request = request
            return ReferenceHermesCompaction().compact(request)

    runtime = Runtime()
    result = HermesCompactionAdapter(runtime).compact(compaction_request())
    assert runtime.request.session_id == "session"
    assert result.version == "hermes-reference-v1"


def test_memory_is_scoped_by_tenant_user_session_policy_and_preserves_sources():
    memory = ReferenceMemoryStore()
    memory.record(MemoryRecord("session-a", "tenant-a", "user-a", "a", MemoryScope.SESSION, "prefer concise answers", ("turn-a",)))
    memory.record(MemoryRecord("preference", "tenant-a", "user-a", None, MemoryScope.USER_PREFERENCE, "prefer Chinese responses", ("profile-a",)))
    memory.record(MemoryRecord("cross", "tenant-a", "user-a", None, MemoryScope.CROSS_SESSION, "project uses python", ("artifact-a",)))
    memory.record(MemoryRecord("other-user", "tenant-a", "user-b", "a", MemoryScope.SESSION, "private", ("turn-b",)))
    memory.record(MemoryRecord("other-tenant", "tenant-b", "user-a", "a", MemoryScope.SESSION, "private", ("turn-c",)))
    session_a = memory.search(tenant_id="tenant-a", user_id="user-a", session_id="a", query="prefer", policy=MemoryPolicy())
    assert [item.memory_id for item in session_a] == ["preference", "session-a"]
    session_b = memory.search(tenant_id="tenant-a", user_id="user-a", session_id="b", query="project", policy=MemoryPolicy(cross_session_enabled=True))
    assert [(item.memory_id, item.source_refs) for item in session_b] == [("cross", ("artifact-a",))]
    assert not memory.search(tenant_id="tenant-a", user_id="user-a", session_id="b", query="project", policy=MemoryPolicy())


def test_memory_cannot_store_skill_tool_or_dependency_mutations():
    with pytest.raises(MemoryError) as rejected:
        MemoryRecord("bad", "tenant", "user", "session", MemoryScope.SESSION, "pip install", ("turn",), "dependency")
    assert rejected.value.code == "memory_mutation_forbidden"
