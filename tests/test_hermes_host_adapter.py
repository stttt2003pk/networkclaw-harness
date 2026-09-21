import json
import threading
from pathlib import Path
from datetime import datetime, timedelta, timezone

from networkclaw_harness.protocol import InputFrame
from networkclaw_harness.runtime.hermes_host_adapter import HermesHostAdapter
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
        FakeNativeAgent.instances.append(self)

    def run_conversation(self, text):
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
        "api_calls": 1,
        "streamed": True,
    }),)
    agent = FakeNativeAgent.instances[0]
    assert agent.kwargs["cwd"] == str(runtimes[0].workspace.root)
    assert agent.kwargs["session_db"] is runtimes[0].session_db
    assert agent.kwargs["enabled_toolsets"] == ["networkclaw", "todo", "delegation"]
    assert agent.suppress_status_output is True

    adapter.close("session")
    assert agent.closed is True
    assert runtimes[0].closed is True


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
        "status": "cancelled", "runtime": "hermes_native", "api_calls": 1,
    })]
