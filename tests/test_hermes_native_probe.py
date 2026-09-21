from pathlib import Path

from networkclaw_harness.runtime.hermes_native_probe import (
    ProbeEvents, callback_bundle, configure_headless_agent, probe_constructor, run_live_probe,
)
from networkclaw_harness.runtime.hermes_bootstrap import open_hermes_runtime
from networkclaw_harness.workspace import SessionWorkspace


class FakeAgent:
    def __init__(self, **kwargs):
        self.kwargs = kwargs
        for name in ("stream_delta_callback", "tool_start_callback", "tool_complete_callback",
                     "status_callback", "event_callback"):
            setattr(self, name, kwargs[name])

    def run_conversation(self, message):
        return {"completed": True, "final_response": message, "api_calls": 1}

    def interrupt(self):
        return True

    def steer(self, text):
        return True

    def close(self):
        return None


def test_callback_bundle_is_bounded_and_non_printing():
    events = ProbeEvents()
    callbacks = callback_bundle(events)
    callbacks["stream_delta_callback"]("hello")
    callbacks["tool_start_callback"]("host_bash")
    callbacks["tool_complete_callback"]("host_bash")
    callbacks["status_callback"]("lifecycle", "ready")
    callbacks["event_callback"]("turn.started", {"run_id": "run-1"})
    assert events.deltas == ["hello"]
    assert events.tools_started == ["host_bash"]
    assert events.tools_completed == ["host_bash"]
    assert events.statuses == [("lifecycle", "ready")]
    assert events.events[0][0] == "turn.started"


def test_probe_constructs_native_agent_with_host_workspace(tmp_path: Path):
    workspace = SessionWorkspace.open(tmp_path / "session", tenant_id="tenant", session_id="session")
    report = probe_constructor(workspace, session_db=object(), agent_factory=FakeAgent)
    assert report.passed
    assert report.workspace == str(workspace.root)
    assert report.session_id == "session"


def test_live_probe_returns_only_bounded_result_metadata():
    report = run_live_probe(FakeAgent(**callback_bundle(ProbeEvents())), "sensitive prompt")
    assert report == {
        "completed": True,
        "failed": False,
        "interrupted": False,
        "has_final_response": True,
        "api_calls": 1,
    }
    assert "sensitive prompt" not in str(report)


def test_headless_agent_disables_cli_output():
    agent = configure_headless_agent(FakeAgent(**callback_bundle(ProbeEvents())))
    assert agent.suppress_status_output is True
    assert agent._print_fn("must not print") is None


def test_vendored_native_agent_constructor_is_closed(tmp_path: Path):
    workspace = SessionWorkspace.open(tmp_path / "native", tenant_id="tenant", session_id="native-session")
    runtime = open_hermes_runtime(workspace)
    try:
        report = probe_constructor(workspace, session_db=runtime.session_db)
    finally:
        runtime.close()
    assert report.passed, report.notes
