import json
import threading
import time
import pytest
from dataclasses import replace
from pathlib import Path
from datetime import datetime, timedelta, timezone

from networkclaw_harness.protocol import InputFrame
from networkclaw_harness.runtime.hermes_host_adapter import (
    HermesHostAdapter, HermesHostAdapterError, _failure_reason_code, _validated_turn_parameters, _run_native_turn,
)
from networkclaw_harness.workspace import SessionWorkspace


class FakeRuntime:
    def __init__(self, workspace):
        self.workspace = workspace
        self.session_db = object()
        self.closed = False

    def close(self):
        self.closed = True


class FakeNativeAgent:
    instances = []

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.closed = False
        self.interrupted = False
        self.steered = []
        self.run_calls = 0
        FakeNativeAgent.instances.append(self)

    def run_conversation(self, text):
        self.run_calls += 1
        self.kwargs["stream_delta_callback"]("native ")
        self.kwargs["stream_delta_callback"]("answer")
        return {
            "completed": True,
            "failed": False,
            "interrupted": False,
            "final_response": "native answer",
            "api_calls": 1,
        }

    def interrupt(self):
        self.interrupted = True
        return True

    def steer(self, text):
        self.steered.append(text)
        return True

    def close(self):
        self.closed = True


def test_failure_reason_codes_are_stable_for_terminal_mapping():
    assert _failure_reason_code("provider_interrupted") == "provider_interrupted"
    assert _failure_reason_code("timeout") == "turn_timeout"
    assert _failure_reason_code("liveness_timeout") == "liveness_timeout"
    assert _failure_reason_code("harness_shutdown") == "harness_shutdown"
    assert _failure_reason_code("unknown-provider-error") == "provider_interrupted"


def _frame(frame_type="user.input", payload=None):
    return InputFrame(
        type=frame_type,
        request_id="request-1",
        tenant_id="tenant",
        user_id="user",
        session_id="session",
        turn_id="turn",
        run_id="run",
        payload=payload or {
            "text": "hello",
            "provider_selection": {
                "provider_id": "openai",
                "model_id": "gpt-5.5",
                "config_ref": "env:openai",
            },
        },
    )


def _adapter(tmp_path: Path):
    runtimes = []

    def runtime_factory(workspace):
        runtime = FakeRuntime(workspace)
        runtimes.append(runtime)
        return runtime

    adapter = HermesHostAdapter(
        environ={
            "OPENAI_API_KEY": "test-key",
            "OPENAI_BASE_URL": "https://provider.example/v1",
            "NETWORKCLAW_HARNESS_ALLOWED_MODELS": "gpt-5.5",
            "NETWORKCLAW_HARNESS_ALLOWED_EGRESS_HOSTS": "provider.example",
        },
        agent_factory=FakeNativeAgent,
        runtime_factory=runtime_factory,
    )
    workspace = SessionWorkspace.open(tmp_path / "workspace", tenant_id="tenant", session_id="session")
    adapter.open(
        session_id="session", tenant_id="tenant", user_id="user",
        workspace=workspace, lease=_lease(),
    )
    return adapter, runtimes


def _lease(version=1):
    issued = datetime.now(timezone.utc)
    stamp = lambda value: value.isoformat().replace("+00:00", "Z")
    return {
        "session_id": "session", "owner_id": "owner", "lease_id": "lease",
        "lease_version": version, "execution_epoch": 1,
        "issued_at": stamp(issued), "renew_by": stamp(issued + timedelta(seconds=30)),
        "expires_at": stamp(issued + timedelta(seconds=60 + version)),
        "grace_expires_at": stamp(issued + timedelta(seconds=70 + version)),
        "ttl_ms": 60000, "renew_interval_ms": 30000, "grace_ms": 10000,
    }


def test_native_adapter_streams_one_vertical_turn(tmp_path: Path):
    FakeNativeAgent.instances.clear()
    adapter, runtimes = _adapter(tmp_path)
    emitted = []
    terminal = adapter.handle_stream(_frame(), lambda kind, payload: emitted.append((kind, payload)))

    assert [kind for kind, _ in emitted] == ["turn.started", "assistant.delta", "assistant.delta"]
    assert "".join(payload["content"] for kind, payload in emitted if kind == "assistant.delta") == "native answer"
    assert terminal == (("turn.completed", {
        "status": "completed",
        "runtime": "hermes_native",
        "generation": 1,
        "api_calls": 1,
        "streamed": True,
    }),)
    agent = FakeNativeAgent.instances[0]
    assert agent.run_calls == 1
    assert agent.kwargs["cwd"] == str(runtimes[0].workspace.root)
    assert agent.kwargs["session_db"] is runtimes[0].session_db
    assert agent.kwargs["enabled_toolsets"] == ["networkclaw", "todo", "delegation", "clarify"]
    assert agent.suppress_status_output is True

    adapter.close("session")
    assert agent.closed is True
    assert runtimes[0].closed is True


def test_native_callbacks_keep_hermes_names_and_gate_restricted_events(tmp_path: Path):
    class EventAgent(FakeNativeAgent):
        def run_conversation(self, text):
            self.kwargs["tool_progress_callback"](
                "subagent.start", "delegate_task", "inspect", None,
                subagent_id="child-a", parent_id="parent", task_index=0, task_count=1,
                allocation_id="allocation-1",
            )
            self.kwargs["tool_progress_callback"]("subagent.text", "", "found files", None,
                                                    subagent_id="child-a")
            self.kwargs["tool_progress_callback"]("tool.progress", "search", "2 files", None,
                                                    invocation_id="invocation-1")
            self.kwargs["tool_gen_callback"]("search")
            self.kwargs["reasoning_callback"]("private reasoning")
            self.kwargs["event_callback"]("moa.progress", {"refs_done": 1, "refs_total": 2})
            return {
                "completed": True, "failed": False, "interrupted": False,
                "final_response": "done", "api_calls": 1,
                "provider_attempt": 2, "context_compacted": True,
                "checkpoint": {"status": "updated", "checkpoint_id": "cp-1"},
                "usage": {"input_tokens": 10, "output_tokens": 4, "reasoning_tokens": 2},
                "provider_metadata": {"status": "completed"},
            }

    adapter, _ = _adapter(tmp_path)
    adapter._agent_factory = EventAgent
    events = []
    adapter.handle_stream(_frame(), lambda kind, payload: events.append((kind, payload)))
    kinds = [kind for kind, _ in events]
    assert "subagent.start" in kinds
    assert "subagent.text" in kinds
    assert "tool.progress" in kinds
    assert "tool.generating" in kinds
    assert "provider.attempt" in kinds and "provider.retry" not in kinds
    assert "context.continued" in kinds
    assert "checkpoint.updated" in kinds
    assert "warning" in kinds  # reasoning, MoA, and usage are restricted by default
    assert "reasoning.delta" not in kinds
    assert "usage.updated" not in kinds
    assert "moa.progress" not in kinds
    assert next(payload for kind, payload in events if kind == "subagent.start")["subagent_id"] == "child-a"
    adapter.close("session")


def test_native_compaction_status_is_safe_generation_bound_and_not_duplicated(tmp_path: Path):
    from networkclaw_harness.runtime.hermes_native_probe import load_agent_class
    load_agent_class()
    from agent.conversation_compression import COMPACTION_STATUS, COMPACTION_HEARTBEAT_STATUS

    class CompactingAgent(FakeNativeAgent):
        def run_conversation(self, text):
            callback = self.status_callback
            callback("lifecycle", "provider secret unrelated to compaction")
            callback("lifecycle", COMPACTION_STATUS)
            callback("lifecycle", COMPACTION_HEARTBEAT_STATUS)
            callback("compacted", "private summary must never reach clients")
            self.previous_callback = callback
            return {"completed": True, "context_compacted": True}

    adapter, _ = _adapter(tmp_path)
    adapter._agent_factory = CompactingAgent
    events = []
    adapter.handle_stream(_frame(), lambda kind, payload: events.append((kind, payload)))
    context = [(kind, payload) for kind, payload in events if kind.startswith("context.")]
    assert [kind for kind, _ in context] == ["context.started", "context.continued"]
    assert [payload["version"] for _, payload in context] == [1, 1]
    assert "private" not in json.dumps(events) and "secret" not in json.dumps(events)
    state = adapter._sessions["session"]
    old_callback = state.agent.previous_callback
    state.bridge.begin(lambda kind, payload: events.append((kind, payload)), generation=2)
    count = len(events)
    old_callback("compacted", "late result")
    assert len(events) == count
    adapter.close("session")


def test_restricted_event_capabilities_allow_reasoning_usage_and_moa(tmp_path: Path):
    class EventAgent(FakeNativeAgent):
        def run_conversation(self, text):
            self.kwargs["reasoning_callback"]("bounded reasoning")
            self.kwargs["event_callback"]("moa.progress", {"refs_done": 1, "refs_total": 1})
            return {"completed": True, "failed": False, "interrupted": False,
                    "final_response": "done", "usage": {"input_tokens": 1, "output_tokens": 1}}

    adapter, _ = _adapter(tmp_path)
    adapter._agent_factory = EventAgent
    state = adapter._sessions["session"]
    state.host_grant = {"event_capabilities": ("reasoning.delta", "usage.updated", "moa.events")}
    events = []
    adapter.handle_stream(_frame(), lambda kind, payload: events.append((kind, payload)))
    kinds = [kind for kind, _ in events]
    assert "reasoning.delta" in kinds
    assert "moa.progress" in kinds
    assert "usage.updated" in kinds
    assert "reasoning_event_restricted" not in {payload.get("code") for kind, payload in events if kind == "warning"}
    adapter.close("session")


def test_turn_parameters_are_passed_to_native_facade_and_restored(tmp_path: Path):
    class ParamAgent(FakeNativeAgent):
        def run_conversation(self, text, **kwargs):
            self.run_calls += 1
            self.turn_kwargs = kwargs
            self.reasoning_seen = self.reasoning_config
            self.tools_seen = list(getattr(self, "valid_tool_names", ()))
            return {"completed": True, "failed": False, "interrupted": False, "final_response": "ok", "api_calls": 1}

    ParamAgent.instances.clear()
    workspace = SessionWorkspace.open(tmp_path / "workspace", tenant_id="tenant", session_id="session")
    adapter = HermesHostAdapter(
        environ={"OPENAI_API_KEY": "test-key", "OPENAI_BASE_URL": "https://provider.example/v1", "NETWORKCLAW_HARNESS_ALLOWED_MODELS": "gpt-5.5", "NETWORKCLAW_HARNESS_ALLOWED_EGRESS_HOSTS": "provider.example"},
        agent_factory=ParamAgent,
        runtime_factory=FakeRuntime,
    )
    adapter.open(session_id="session", tenant_id="tenant", user_id="user", workspace=workspace, lease=_lease(), host_grant={"session_id": "session", "execution_epoch": 1, "workspace": str(workspace.root), "turn_budget": {"max_steps": 8, "max_retries": 2, "max_actions": 8, "timeout_seconds": 30}, "resource_profile": "customer", "allowed_tools": ["networkclaw"]})
    frame = _frame(payload={
        "text": "hello",
        "provider_selection": {"provider_id": "openai", "model_id": "gpt-5.5", "config_ref": "env:openai"},
        "system_message": "be concise",
        "reasoning_effort": "high",
        "allowed_tools": ["networkclaw"],
        "turn_author": {"id": "user"},
        "relay_metadata": {"source": "gateway"},
    })
    adapter.handle(frame)
    agent = ParamAgent.instances[0]
    assert agent.reasoning_config is None
    assert agent.run_calls == 1
    assert agent.turn_kwargs == {"system_message": "be concise", "turn_author": {"id": "user"}, "relay_metadata": {"source": "gateway"}}
    assert agent.reasoning_seen["effort"] == "high"
    assert agent.tools_seen == [] or agent.tools_seen == ["networkclaw"]


def test_native_turn_reads_session_history_again_after_agent_rebuild():
    class Database:
        def __init__(self):
            self.calls = []
            self.history = [{"role": "user", "content": "prior turn", "_row_id": 1}]

        def get_messages_as_conversation(self, session_id, **kwargs):
            self.calls.append((session_id, kwargs))
            return list(self.history)

    class Agent:
        session_id = "hermes-session"

        def __init__(self, database):
            self._session_db = database

        def run_conversation(self, text, conversation_history=None):
            return {"messages": conversation_history}

    database = Database()
    assert _run_native_turn(Agent(database), "next", {})["messages"] == database.history
    database.history.append({"role": "assistant", "content": "prior answer", "_row_id": 2})
    assert _run_native_turn(Agent(database), "after rebuild", {})["messages"] == database.history
    assert database.calls == [("hermes-session", {"repair_alternation": True, "include_row_ids": True})] * 2


def test_turn_parameters_reject_tools_outside_host_grant(tmp_path: Path):
    workspace = SessionWorkspace.open(tmp_path / "workspace", tenant_id="tenant", session_id="session")
    adapter = HermesHostAdapter(
        environ={"OPENAI_API_KEY": "test-key", "OPENAI_BASE_URL": "https://provider.example/v1", "NETWORKCLAW_HARNESS_ALLOWED_MODELS": "gpt-5.5", "NETWORKCLAW_HARNESS_ALLOWED_EGRESS_HOSTS": "provider.example"},
        agent_factory=FakeNativeAgent,
        runtime_factory=FakeRuntime,
    )
    adapter.open(session_id="session", tenant_id="tenant", user_id="user", workspace=workspace, lease=_lease(), host_grant={"session_id": "session", "execution_epoch": 1, "workspace": str(workspace.root), "turn_budget": {"max_steps": 8, "max_retries": 2, "max_actions": 8, "timeout_seconds": 30}, "resource_profile": "customer", "allowed_tools": ["networkclaw"]})
    events = adapter.handle(_frame(payload={
        "text": "hello",
        "provider_selection": {"provider_id": "openai", "model_id": "gpt-5.5", "config_ref": "env:openai"},
        "allowed_tools": ["not-in-grant"],
    }))
    assert events[0][0] == "turn.failed"
    assert events[0][1]["code"] == "allowed_tools_exceed_grant"


def test_native_adapter_fails_closed_without_credentials(tmp_path: Path):
    workspace = SessionWorkspace.open(tmp_path / "workspace", tenant_id="tenant", session_id="session")
    adapter = HermesHostAdapter(
        environ={"NETWORKCLAW_HARNESS_ALLOWED_MODELS": "gpt-5.5"},
        agent_factory=FakeNativeAgent,
        runtime_factory=FakeRuntime,
    )
    adapter.open(
        session_id="session", tenant_id="tenant", user_id="user",
        workspace=workspace, lease=_lease(),
    )
    events = adapter.handle(_frame())
    assert events[0][0] == "turn.failed"
    assert events[0][1]["code"] == "provider_credentials_missing"
    assert not any("test-key" in str(event) for event in events)


def test_native_adapter_controls_existing_agent(tmp_path: Path):
    FakeNativeAgent.instances.clear()
    adapter, _ = _adapter(tmp_path)
    adapter.handle(_frame())
    agent = FakeNativeAgent.instances[0]

    assert adapter.handle(_frame("turn.steer", {"text": "focus"}))[0][1]["state"] == "accepted"
    assert agent.steered == ["focus"]
    assert adapter.handle(_frame("turn.cancel", {}))[0][1]["state"] == "idle"
    assert agent.interrupted is False


def test_different_sessions_can_execute_in_parallel(tmp_path: Path):
    started = threading.Event()
    started_count = 0
    started_lock = threading.Lock()
    release = threading.Event()

    class ParallelAgent(FakeNativeAgent):
        def run_conversation(self, text):
            nonlocal started_count
            with started_lock:
                started_count += 1
                if started_count == 2:
                    started.set()
            release.wait(2)
            return {"completed": True, "failed": False, "interrupted": False,
                    "final_response": "parallel", "api_calls": 1}

    adapter, _ = _adapter(tmp_path)
    second_workspace = SessionWorkspace.open(tmp_path / "workspace-2", tenant_id="tenant", session_id="session-2")
    second_lease = dict(_lease())
    second_lease["session_id"] = "session-2"
    adapter.open(session_id="session-2", tenant_id="tenant", user_id="user",
                 workspace=second_workspace, lease=second_lease, host_grant=None)
    adapter._agent_factory = ParallelAgent
    frames = [
        _frame(),
        replace(_frame(), session_id="session-2", request_id="request-2", turn_id="turn-2", run_id="run-2"),
    ]
    results = []
    workers = [threading.Thread(target=lambda frame=frame: results.append(adapter.handle(frame))) for frame in frames]
    for worker in workers:
        worker.start()
    assert started.wait(2)
    release.set()
    for worker in workers:
        worker.join(2)
    assert len(results) == 2
    assert all(result[-1][0] == "turn.completed" for result in results)


def test_same_session_rejects_a_concurrent_turn_before_native_execution(tmp_path: Path):
    started = threading.Event()
    release = threading.Event()

    class SerialAgent(FakeNativeAgent):
        def run_conversation(self, text):
            started.set()
            release.wait(2)
            return {"completed": True, "failed": False, "interrupted": False,
                    "final_response": "serial", "api_calls": 1}

    adapter, _ = _adapter(tmp_path)
    adapter._agent_factory = SerialAgent
    first_result = []
    worker = threading.Thread(target=lambda: first_result.extend(adapter.handle(_frame())))
    worker.start()
    assert started.wait(1)
    second = replace(_frame(), request_id="request-2", turn_id="turn-2", run_id="run-2")
    rejected = adapter.handle(second)
    release.set()
    worker.join(2)
    assert rejected[0][1]["code"] == "turn_already_active"
    assert first_result[-1][0] == "turn.completed"


def test_todo_completion_is_committed_then_projected_as_plan(tmp_path: Path):
    class TodoAgent(FakeNativeAgent):
        def run_conversation(self, text):
            self.kwargs["tool_complete_callback"](
                "todo-call-1", "todo_list", {},
                json.dumps({"revision": 2, "todos": [
                    {"id": "step-1", "content": "Inspect state", "status": "completed"},
                    {"id": "step-2", "content": "Apply fix", "status": "in_progress"},
                ]}),
            )
            return {"completed": True, "failed": False, "interrupted": False,
                    "final_response": "done", "api_calls": 1}

    adapter, _ = _adapter(tmp_path)
    adapter._agent_factory = TodoAgent
    emitted = []
    terminal = adapter.handle_stream(_frame(), lambda kind, payload: emitted.append((kind, payload)))
    plan = next(payload for kind, payload in emitted if kind == "plan.updated")
    assert plan["revision"] == 2
    assert plan["schema_version"] == "networkclaw.plan.v1"
    assert plan["items"][1] == {"id": "step-2", "title": "Apply fix", "status": "in_progress"}
    assert plan["cursor"] == "1"
    assert terminal[0][0] == "turn.completed"


def test_native_cancel_reports_cancelled_terminal_state(tmp_path: Path):
    class BlockingAgent(FakeNativeAgent):
        started = threading.Event()

        def run_conversation(self, text):
            self.started.set()
            while not self.interrupted:
                self.started.wait(0.01)
            return {"completed": False, "failed": False, "interrupted": True,
                    "final_response": "", "api_calls": 1}

    adapter, _ = _adapter(tmp_path)
    adapter._agent_factory = BlockingAgent
    terminal = []
    worker = threading.Thread(target=lambda: terminal.extend(adapter.handle_stream(_frame(), lambda *_: None)))
    worker.start()
    assert BlockingAgent.started.wait(1)
    control = adapter.handle(_frame("turn.cancel", {}))
    worker.join(2)
    assert control[0][1]["state"] == "cancel_requested"
    assert terminal == [("turn.cancelled", {
        "status": "cancelled", "runtime": "hermes_native", "reason_code": "user_cancel", "generation": 1, "api_calls": 1,
    })]


def test_controls_reject_stale_run_generation(tmp_path: Path):
    class BlockingAgent(FakeNativeAgent):
        started = threading.Event()

        def run_conversation(self, text):
            self.started.set()
            while not self.interrupted:
                self.started.wait(0.01)
            return {"completed": False, "failed": False, "interrupted": True, "api_calls": 1}

    adapter, _ = _adapter(tmp_path)
    adapter._agent_factory = BlockingAgent
    worker = threading.Thread(target=lambda: adapter.handle_stream(
        _frame(), lambda *_: None,
    ))
    worker.start()
    assert BlockingAgent.started.wait(1)
    stale = replace(_frame("turn.cancel", {}), run_id="old-run")
    assert adapter.handle(stale)[0][1]["state"] == "stale_turn"
    current = _frame("turn.cancel", {})
    assert adapter.handle(current)[0][1]["state"] == "cancel_requested"
    worker.join(2)


def test_session_reuses_agent_then_rebuilds_after_eviction(tmp_path: Path):
    FakeNativeAgent.instances.clear()
    adapter, _ = _adapter(tmp_path)
    first = _frame()
    adapter.handle(first)
    assert len(FakeNativeAgent.instances) == 1
    second = _frame()
    second = replace(second, request_id="request-2", turn_id="turn-2", run_id="run-2")
    adapter.handle(second)
    assert len(FakeNativeAgent.instances) == 1
    assert adapter.evict_agent("session") is True
    assert adapter.cache_entry("session") is None
    third = replace(second, request_id="request-3", turn_id="turn-3", run_id="run-3")
    adapter.handle(third)
    assert len(FakeNativeAgent.instances) == 2
    assert FakeNativeAgent.instances[0].closed is True


def test_each_turn_calls_only_the_native_conversation_entry_once(tmp_path: Path):
    FakeNativeAgent.instances.clear()
    adapter, _ = _adapter(tmp_path)
    adapter.handle(_frame())
    second = replace(_frame(), request_id="request-2", turn_id="turn-2", run_id="run-2")
    adapter.handle(second)
    assert len(FakeNativeAgent.instances) == 1
    assert FakeNativeAgent.instances[0].run_calls == 2


def test_late_callback_from_previous_generation_is_dropped(tmp_path: Path):
    FakeNativeAgent.instances.clear()
    adapter, _ = _adapter(tmp_path)
    emitted = []
    adapter.handle_stream(_frame(), lambda kind, payload: emitted.append((kind, payload)))
    agent = FakeNativeAgent.instances[0]
    old_callback = agent.stream_delta_callback
    second = replace(_frame(), request_id="request-2", turn_id="turn-2", run_id="run-2")
    adapter.handle_stream(second, lambda kind, payload: emitted.append((kind, payload)))
    before = len(emitted)
    old_callback("late old-generation delta")
    assert len(emitted) == before
    assert not any(payload.get("content") == "late old-generation delta" for _, payload in emitted)


def test_route_change_rebuilds_agent_without_closing_session(tmp_path: Path):
    FakeNativeAgent.instances.clear()
    adapter, _ = _adapter(tmp_path)
    adapter.handle(_frame())
    changed = _frame(payload={"text": "hello", "provider_selection": {
        "provider_id": "openai", "model_id": "gpt-5.5", "config_ref": "env:openai",
    }})
    adapter._env["OPENAI_BASE_URL"] = "https://other.example/v1"
    adapter._env["NETWORKCLAW_HARNESS_ALLOWED_EGRESS_HOSTS"] = "other.example"
    adapter.handle(changed)
    assert len(FakeNativeAgent.instances) == 2
    assert FakeNativeAgent.instances[0].closed is True


def test_profile_change_rebuilds_agent_cache_entry(tmp_path: Path):
    FakeNativeAgent.instances.clear()
    adapter, _ = _adapter(tmp_path)
    adapter.handle(_frame())
    adapter._env["NETWORKCLAW_HARNESS_PROFILE"] = "customer"
    adapter._env["NETWORKCLAW_HARNESS_ALLOWED_EGRESS_HOSTS"] = "provider.example"
    adapter.handle(_frame(payload={"text": "hello", "provider_selection": {
        "provider_id": "openai", "model_id": "gpt-5.5", "config_ref": "env:openai",
    }}))
    assert len(FakeNativeAgent.instances) == 2
    assert FakeNativeAgent.instances[0].closed is True


def test_host_grant_tool_and_profile_signatures_select_cache_entry(tmp_path: Path):
    FakeNativeAgent.instances.clear()
    adapter = HermesHostAdapter(
        environ={
            "OPENAI_API_KEY": "test-key", "OPENAI_BASE_URL": "https://provider.example/v1",
            "NETWORKCLAW_HARNESS_ALLOWED_MODELS": "gpt-5.5",
            "NETWORKCLAW_HARNESS_ALLOWED_EGRESS_HOSTS": "provider.example",
        }, agent_factory=FakeNativeAgent, runtime_factory=FakeRuntime,
    )
    workspace = SessionWorkspace.open(tmp_path / "workspace", tenant_id="tenant", session_id="session")
    grant = {
        "session_id": "session", "execution_epoch": 1, "workspace": str(workspace.root),
        "turn_budget": {"max_steps": 1, "max_retries": 1, "max_actions": 1, "timeout_seconds": 1},
        "resource_profile": {"tier": "standard"}, "allowed_tools": ["networkclaw"],
    }
    adapter.open(session_id="session", tenant_id="tenant", user_id="user", workspace=workspace,
                 lease=_lease(), host_grant=grant)
    adapter.handle(_frame())
    assert adapter.cache_entry("session").route[-2:] == ("networkclaw", '{"tier":"standard"}')
    adapter.close("session")


@pytest.mark.parametrize("change", ["agent", "profile", "tools", "budget", "resource_profile"])
def test_configuration_refresh_replaces_only_target_agent(tmp_path: Path, change: str):
    adapter, runtimes = _adapter(tmp_path)
    workspace = SessionWorkspace.open(tmp_path / "other", tenant_id="tenant", session_id="other")
    adapter.open(session_id="other", tenant_id="tenant", user_id="user", workspace=workspace,
                 lease={**_lease(), "session_id": "other"})
    adapter.handle(_frame())
    adapter.handle(replace(_frame(), session_id="other"))
    first = adapter.cache_entry("session").agent
    other = adapter.cache_entry("other").agent
    state = adapter._sessions["session"]
    resources = (state.runtime, state.tool_session, state.bridge, state.admission)
    grant = {
        "session_id": "session", "execution_epoch": 1, "workspace": str(state.workspace.root),
        "turn_budget": {"max_steps": 3, "max_retries": 1, "max_actions": 2, "timeout_seconds": 20},
        "allowed_tools": ["networkclaw"], "resource_profile": {"tier": "standard"},
    }
    config = {"agent_id": "agent-a", "profile_id": "profile-a", "host_grant": grant}
    adapter.configure_session("session", **config)
    adapter.handle(_frame())
    cached = adapter.cache_entry("session").agent
    adapter.configure_session("session", **config)
    assert adapter.cache_entry("session").agent is cached
    if change == "agent":
        config["agent_id"] = "agent-b"
    elif change == "profile":
        config["profile_id"] = "profile-b"
    elif change == "tools":
        config["host_grant"] = {**grant, "allowed_tools": ["todo"]}
    elif change == "budget":
        config["host_grant"] = {**grant, "turn_budget": {**grant["turn_budget"], "max_steps": 4}}
    else:
        config["host_grant"] = {**grant, "resource_profile": {"tier": "large"}}
    adapter.configure_session("session", **config)
    adapter.handle(_frame())
    rebuilt = adapter.cache_entry("session").agent
    assert first.closed and cached.closed
    assert rebuilt is not cached
    if change == "budget":
        assert rebuilt.kwargs["max_iterations"] == 4
    assert adapter.cache_entry("other").agent is other and not other.closed
    assert resources == (state.runtime, state.tool_session, state.bridge, state.admission)
    assert not any(runtime.closed for runtime in runtimes)
    adapter.close("session")
    adapter.close("other")


def test_configuration_refresh_rejects_active_or_fenced_session(tmp_path: Path):
    adapter, _ = _adapter(tmp_path)
    state = adapter._sessions["session"]
    state.active_turn_id = "turn"
    with pytest.raises(HermesHostAdapterError) as active:
        adapter.configure_session("session", agent_id="different")
    assert active.value.code == "turn_already_active"
    assert state.agent_id == ""
    state.active_turn_id = None
    adapter.fence("session")
    with pytest.raises(HermesHostAdapterError) as fenced:
        adapter.configure_session("session", agent_id="different")
    assert fenced.value.code == "stale_epoch"
    adapter.close("session")


def test_turn_uses_granted_tools_when_request_omits_tool_selection():
    grant = {"allowed_tools": ["networkclaw"]}
    assert _validated_turn_parameters({}, grant)["allowed_tools"] == ("networkclaw",)
    assert _validated_turn_parameters({}, {"allowed_tools": ("networkclaw",)})["allowed_tools"] == ("networkclaw",)
    assert _validated_turn_parameters({"allowed_tools": []}, grant)["allowed_tools"] == ()
    with pytest.raises(HermesHostAdapterError) as denied:
        _validated_turn_parameters({"allowed_tools": ["delegation"]}, grant)
    assert denied.value.code == "allowed_tools_exceed_grant"


def test_native_delegation_binds_independent_child_resources_and_releases_them(tmp_path: Path):
    adapter, runtimes = _adapter(tmp_path)
    result = []
    worker = threading.Thread(target=lambda: result.append(adapter._take_delegation_grant(
        adapter._sessions["session"], task_index=0, task_count=1, depth=1,
        requested_iterations=4,
    )))
    worker.start()
    state = adapter._sessions["session"]
    allocation = None
    for _ in range(100):
        if state.broker is not None and state.broker._pending:
            allocation = next(iter(state.broker._pending))
            break
        worker.join(0.01)
    assert allocation is not None
    state.broker.resolve(allocation, {
        "decision": "grant", "child_session_id": "child-1",
        "workspace_root": str(tmp_path / "child-1"),
        "lease": {**_lease(), "session_id": "child-1"},
        "budget": {"max_iterations": 4, "timeout_seconds": 30, "max_output_bytes": 4096},
    })
    worker.join(2)
    assert result[0]["session_id"] == "child-1"
    assert result[0]["lineage"]["parent_session_id"] == "session"
    assert allocation in state.child_resources
    child_runtime, child_tools = state.child_resources[allocation]
    adapter._release_delegation_grants(state, (allocation,))
    assert child_runtime.closed is True if hasattr(child_runtime, "closed") else True
    assert allocation not in state.child_resources


def test_reopening_same_workspace_after_adapter_restart_reuses_session_identity(tmp_path: Path):
    FakeNativeAgent.instances.clear()
    workspace = SessionWorkspace.open(tmp_path / "restart-workspace", tenant_id="tenant", session_id="session")
    env = {"OPENAI_API_KEY": "test-key", "OPENAI_BASE_URL": "https://provider.example/v1",
           "NETWORKCLAW_HARNESS_ALLOWED_MODELS": "gpt-5.5",
           "NETWORKCLAW_HARNESS_ALLOWED_EGRESS_HOSTS": "provider.example"}
    adapter_one = HermesHostAdapter(environ=env, agent_factory=FakeNativeAgent)
    adapter_one.open(session_id="session", tenant_id="tenant", user_id="user",
                     workspace=workspace, lease=_lease())
    adapter_one.handle(_frame())
    adapter_one.close("session")
    reopened = SessionWorkspace.open(workspace.root, tenant_id="tenant", session_id="session")
    adapter_two = HermesHostAdapter(environ=env, agent_factory=FakeNativeAgent)
    adapter_two.open(session_id="session", tenant_id="tenant", user_id="user",
                     workspace=reopened, lease=_lease())
    adapter_two.handle(replace(_frame(), request_id="request-2", turn_id="turn-2", run_id="run-2"))
    assert reopened.root == workspace.root
    assert workspace.path_for("session-state/hermes-session.db").exists()
    assert FakeNativeAgent.instances[-1].kwargs["cwd"] == str(workspace.root)
    adapter_two.close("session")


def test_failed_child_allocation_does_not_modify_parent_durable_state(tmp_path: Path):
    runtimes = []

    def runtime_factory(workspace):
        if workspace.session_id.startswith("child-"):
            raise RuntimeError("child runtime unavailable")
        runtime = FakeRuntime(workspace)
        runtimes.append(runtime)
        return runtime

    adapter = HermesHostAdapter(
        environ={"OPENAI_API_KEY": "test-key", "OPENAI_BASE_URL": "https://provider.example/v1",
                 "NETWORKCLAW_HARNESS_ALLOWED_MODELS": "gpt-5.5",
                 "NETWORKCLAW_HARNESS_ALLOWED_EGRESS_HOSTS": "provider.example"},
        agent_factory=FakeNativeAgent, runtime_factory=runtime_factory,
    )
    workspace = SessionWorkspace.open(tmp_path / "parent", tenant_id="tenant", session_id="session")
    adapter.open(session_id="session", tenant_id="tenant", user_id="user", workspace=workspace, lease=_lease())
    state = adapter._sessions["session"]
    parent_path = state.tool_session.durable.path
    before = parent_path.read_bytes() if parent_path.exists() else b""
    result = []
    worker = threading.Thread(target=lambda: result.append(_take_grant_for_test(adapter, state)))
    worker.start()
    allocation = None
    for _ in range(100):
        if state.broker is not None and state.broker._pending:
            allocation = next(iter(state.broker._pending))
            break
        time.sleep(0.01)
    assert allocation is not None
    state.broker.resolve(allocation, {
        "decision": "grant", "child_session_id": "child-failed",
        "workspace_root": str(tmp_path / "child-failed"),
        "lease": {**_lease(), "session_id": "child-failed"},
        "budget": {"max_iterations": 4, "timeout_seconds": 30, "max_output_bytes": 4096},
    })
    worker.join(2)
    assert result and result[0] == "RuntimeError"
    after = parent_path.read_bytes() if parent_path.exists() else b""
    assert after == before


def _take_grant_for_test(adapter, state):
    try:
        adapter._take_delegation_grant(state, task_index=0, task_count=1, depth=1, requested_iterations=4)
    except Exception as error:
        return type(error).__name__
    return "unexpected-success"


def test_epoch_fence_interrupts_turn_and_drops_late_events(tmp_path: Path):
    class EpochAgent(FakeNativeAgent):
        started = threading.Event()

        def run_conversation(self, text):
            self.started.set()
            while not self.interrupted:
                time.sleep(0.005)
            self.kwargs["stream_delta_callback"]("late")
            return {"completed": False, "failed": False, "interrupted": True, "api_calls": 1}

    import time
    adapter, _ = _adapter(tmp_path)
    adapter._agent_factory = EpochAgent
    emitted = []
    worker = threading.Thread(target=lambda: emitted.extend(adapter.handle_stream(_frame(), lambda *event: emitted.append(event))))
    worker.start()
    assert EpochAgent.started.wait(1)
    adapter.fence("session")
    worker.join(2)
    assert not any(kind == "assistant.delta" and payload.get("content") == "late" for kind, payload in emitted)
    assert emitted[-1][0] == "turn.cancelled"


def test_epoch_fence_emits_exactly_one_terminal_outcome(tmp_path: Path):
    class FencedAgent(FakeNativeAgent):
        started = threading.Event()

        def run_conversation(self, text):
            self.started.set()
            while not self.interrupted:
                time.sleep(0.005)
            return {"completed": False, "failed": False, "interrupted": True, "api_calls": 1}

    import time
    adapter, _ = _adapter(tmp_path)
    adapter._agent_factory = FencedAgent
    emitted = []
    terminal = []
    worker = threading.Thread(target=lambda: terminal.extend(adapter.handle_stream(
        _frame(), lambda *event: emitted.append(event),
    )))
    worker.start()
    assert FencedAgent.started.wait(1)
    adapter.fence("session", "provider_lease_lost")
    worker.join(2)
    terminals = [kind for kind, _ in (*emitted, *terminal) if kind in {"turn.completed", "turn.failed", "turn.cancelled"}]
    assert terminals == ["turn.cancelled"]
