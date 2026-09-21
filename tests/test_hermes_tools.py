import json
from datetime import datetime, timedelta, timezone

from networkclaw_harness.runtime.hermes_bootstrap import open_hermes_runtime
from networkclaw_harness.runtime.hermes_tools import (
    TOOL_NAME,
    HermesHostToolSession,
    install_hermes_host_tools,
)
from networkclaw_harness.workspace import SessionWorkspace


class EventSink:
    def __init__(self):
        self.events = []

    def publish(self, event_type, payload):
        self.events.append((event_type, dict(payload)))


def lease(*, expired=False, version=1):
    issued = datetime.now(timezone.utc) - (timedelta(minutes=2) if expired else timedelta())
    stamp = lambda value: value.isoformat().replace("+00:00", "Z")
    return {
        "session_id": "session", "owner_id": "owner", "lease_id": "lease",
        "lease_version": version, "execution_epoch": 1,
        "issued_at": stamp(issued), "renew_by": stamp(issued + timedelta(seconds=30)),
        "expires_at": stamp(issued + timedelta(seconds=60)),
        "grace_expires_at": stamp(issued + timedelta(seconds=70)),
        "ttl_ms": 60000, "renew_interval_ms": 30000, "grace_ms": 10000,
    }


def tool_session(tmp_path, *, expired=False):
    workspace = SessionWorkspace.open(tmp_path / "workspace", tenant_id="tenant", session_id="session")
    sink = EventSink()
    session = HermesHostToolSession(
        tenant_id="tenant", user_id="user", workspace=workspace,
        lease=lease(expired=expired), bridge=sink, profile_name="customer",
    )
    return workspace, sink, session


def test_workspace_read_is_policy_fenced_bounded_and_audited_without_content(tmp_path):
    workspace, sink, session = tool_session(tmp_path)
    workspace.path_for("notes.txt").write_text("private-value", encoding="utf-8")
    try:
        result = json.loads(session.execute({"path": "notes.txt"}))
        assert result == {
            "artifacts": [], "bytes_read": 13, "content": "private-value",
            "next_actions": [], "path": "notes.txt",
            "status": "success", "summary": "Read notes.txt (13 bytes).",
            "truncated": False,
        }
        assert [kind for kind, _ in sink.events] == ["tool.started", "tool.completed"]
        started, completed = (payload for _, payload in sink.events)
        assert started["invocation_id"] == completed["invocation_id"]
        assert completed["status"] == "succeeded"
        durable = repr([dict(item.fact.payload) for item in session.durable.load("session")])
        assert "private-value" not in durable
        assert "notes.txt" in durable
    finally:
        session.close()


def test_workspace_read_rejects_escape_and_expired_lease_without_reading(tmp_path):
    workspace, sink, session = tool_session(tmp_path, expired=True)
    outside = tmp_path / "outside.txt"
    outside.write_text("must-not-read", encoding="utf-8")
    try:
        expired = json.loads(session.execute({"path": "../outside.txt"}))
        assert expired["status"] == "error"
        assert expired["error_code"] in {"lease_expired", "path_not_allowed"}
        assert "must-not-read" not in json.dumps(expired)
        assert sink.events[-1][1]["status"] == "failed"
    finally:
        session.close()


def test_workspace_read_failure_has_one_correlated_terminal_event(tmp_path):
    workspace, sink, session = tool_session(tmp_path)
    workspace.path_for("binary.dat").write_bytes(b"\xff")
    try:
        result = json.loads(session.execute({"path": "binary.dat"}))
        assert result["status"] == "error"
        assert result["error_code"] == "invalid_encoding"
        assert [kind for kind, _ in sink.events] == ["tool.started", "tool.completed"]
        assert sink.events[0][1]["invocation_id"] == sink.events[1][1]["invocation_id"]
        assert sink.events[1][1]["status"] == "failed"
    finally:
        session.close()


def test_vendored_hermes_exposes_only_the_networkclaw_toolset(tmp_path):
    workspace = SessionWorkspace.open(tmp_path / "workspace", tenant_id="tenant", session_id="session")
    runtime = open_hermes_runtime(workspace)
    try:
        install_hermes_host_tools()
        model_tools = runtime.module("model_tools")
        definitions = model_tools.get_tool_definitions(
            enabled_toolsets=["networkclaw"], quiet_mode=True,
        )
        assert [item["function"]["name"] for item in definitions] == [TOOL_NAME]
        assert runtime.module("agent.tool_executor") is not None
    finally:
        runtime.close()
