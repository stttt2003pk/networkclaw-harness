import io
import json
import queue
import threading
from pathlib import Path

from networkclaw_harness.host import JsonlHost


class FeedStream:
    def __init__(self):
        self._lines = queue.Queue()

    def feed(self, frame):
        self._lines.put(json.dumps(frame) + "\n")

    def close(self):
        self._lines.put(None)

    def __iter__(self):
        return self

    def __next__(self):
        value = self._lines.get(timeout=5)
        if value is None:
            raise StopIteration
        return value


def command(kind: str, request_id: str, **extra):
    return {"protocol_version": "1.0", "type": kind, "request_id": request_id, **extra}


def session_command(kind: str, request_id: str, session_id: str = "session-1", **extra):
    identity = {"tenant_id": "tenant-1", "user_id": "user-1", "session_id": session_id}
    identity.update(extra)
    return command(kind, request_id, **identity)


def lease(session_id: str = "session-1", *, version: int = 1):
    if version == 1:
        issued_at, renew_by = "2026-09-17T00:00:00Z", "2026-09-17T00:00:30Z"
        expires_at, grace_expires_at = "2026-09-17T00:01:00Z", "2026-09-17T00:01:10Z"
    else:
        issued_at, renew_by = "2026-09-17T00:00:20Z", "2026-09-17T00:00:50Z"
        expires_at, grace_expires_at = "2026-09-17T00:01:20Z", "2026-09-17T00:01:30Z"
    return {
        "session_id": session_id, "owner_id": "owner-1", "lease_id": "lease-1",
        "issued_at": issued_at, "expires_at": expires_at,
        "renew_by": renew_by, "grace_expires_at": grace_expires_at,
        "lease_version": version, "execution_epoch": 1,
        "ttl_ms": 60000, "renew_interval_ms": 30000, "grace_ms": 10000,
    }


def open_command(root: Path, request_id: str = "req-open", session_id: str = "session-1"):
    return session_command("session.open", request_id, session_id, payload={
        "workspace_root": str(root), "owner_id": "owner-1", "execution_epoch": 1,
        "lease": lease(session_id),
    })


def run_host(frames, runtime=None):
    output = io.StringIO()
    host = JsonlHost(
        io.StringIO("".join(json.dumps(frame) + "\n" for frame in frames)),
        output,
        runtime=runtime,
    )
    code = host.serve()
    return code, [json.loads(line) for line in output.getvalue().splitlines()]


def test_request_stream_sequences_restart_and_end(tmp_path: Path):
    frames = [
        open_command(tmp_path / "session-1"),
        session_command("user.input", "req-turn", turn_id="turn-1", payload={"text": "hello"}),
        command("shutdown", "req-stop"),
    ]
    code, events = run_host(frames)
    assert code == 0
    streams = {request_id: [event for event in events if event["request_id"] == request_id] for request_id in {"req-open", "req-turn", "req-stop"}}
    for stream in streams.values():
        assert [event["sequence"] for event in stream] == list(range(1, len(stream) + 1))
        assert stream[-1]["type"] == "end"
        assert stream[-1]["end"] is True
    assert [event["type"] for event in streams["req-turn"]] == ["request.accepted", "turn.queued", "turn.failed", "end"]


def test_duplicate_request_replays_and_conflicting_request_fails(tmp_path: Path):
    original = open_command(tmp_path / "session-1")
    conflicting = dict(original, type="session.resume")
    _, events = run_host([original, original, conflicting])
    assert [event["type"] for event in events[:3]] == ["request.accepted", "session.opened", "end"]
    assert [event["type"] for event in events[3:6]] == ["request.accepted", "session.opened", "end"]
    assert events[-1]["type"] == "error"
    assert events[-1]["payload"]["code"] == "request_id_conflict"
    assert events[-1]["sequence"] == 1


def test_rejected_request_is_deduplicated_and_conflicting_retry_fails():
    rejected = session_command("user.input", "req-rejected", turn_id="turn-1", payload={"text": "hello"})
    conflicting = dict(rejected, turn_id="turn-2")
    _, events = run_host([rejected, rejected, conflicting])
    assert [event["payload"]["code"] for event in events] == [
        "session_not_open", "session_not_open", "request_id_conflict"
    ]


def test_process_and_session_identity_rules_are_fail_closed(tmp_path: Path):
    frames = [
        command("health.query", "req-health", session_id="fake"),
        command("user.input", "req-missing", payload={"text": "hello"}),
        command("authorization.future", "req-control"),
        command("presentation.future", "req-display"),
    ]
    _, events = run_host(frames)
    by_request = {event["request_id"]: event for event in events if event["type"] in {"error", "warning"}}
    assert by_request["req-health"]["payload"]["code"] == "invalid_identity"
    assert by_request["req-missing"]["payload"]["code"] == "invalid_identity"
    assert by_request["req-control"]["payload"]["code"] == "unknown_control"
    assert by_request["req-display"]["payload"]["code"] == "presentation_degraded"


def test_sessions_are_isolated_and_controls_take_explicit_path(tmp_path: Path):
    frames = [open_command(tmp_path / "s1"), open_command(tmp_path / "s2", "req-open-2", "session-2")]
    frames += [
        session_command("turn.cancel", "req-cancel", turn_id="turn-1", payload={}),
        session_command("user.input", "req-cross", "session-2", turn_id="turn-2", payload={"text": "two"}),
        session_command("session.close", "req-wrong", "session-1", user_id="user-2", payload={}),
    ]
    _, events = run_host(frames)
    cancel = [e for e in events if e["request_id"] == "req-cancel"]
    assert [e["type"] for e in cancel] == ["request.accepted", "turn.controlled", "end"]
    cross = [e for e in events if e["request_id"] == "req-cross"]
    assert all(e["session_id"] == "session-2" for e in cross)
    wrong = [e for e in events if e["request_id"] == "req-wrong"]
    assert wrong[-1]["payload"]["code"] == "session_identity_mismatch"


def test_malformed_and_half_written_json_are_terminal_errors():
    output = io.StringIO()
    host = JsonlHost(io.StringIO('{"protocol_version":"1.0"\n'), output)
    assert host.serve() == 0
    event = json.loads(output.getvalue())
    assert event["type"] == "error"
    assert event["payload"]["code"] == "invalid_json"
    assert event["end"] is True


def test_clean_input_pipe_closure_finishes_without_protocol_noise():
    output = io.StringIO()
    assert JsonlHost(io.StringIO(""), output).serve() == 0
    assert output.getvalue() == ""


def test_broken_output_pipe_stops_without_escaping_exception():
    class BrokenOutput(io.StringIO):
        def write(self, value):
            raise BrokenPipeError

    host = JsonlHost(io.StringIO(json.dumps(command("health.query", "req")) + "\n"), BrokenOutput())
    assert host.serve() == 74


def test_short_output_write_is_treated_as_closed_stream():
    class ShortOutput(io.StringIO):
        def write(self, value):
            return max(0, len(value) - 1)

    host = JsonlHost(io.StringIO(json.dumps(command("health.query", "req")) + "\n"), ShortOutput())
    assert host.serve() == 74


def test_version_handshake_and_capability_catalog():
    frames = [
        command("protocol.negotiate", "req-negotiate", payload={"supported_protocol_versions": ["1.0"]}),
        command("capabilities.query", "req-capabilities"),
    ]
    _, events = run_host(frames)
    negotiated = next(event for event in events if event["type"] == "protocol.negotiated")
    capabilities = next(event for event in events if event["type"] == "capabilities.report")
    assert negotiated["payload"]["protocol_version"] == "1.0"
    assert "approval.resolve" in capabilities["payload"]["commands"]
    assert capabilities["payload"]["limits"]["max_frame_bytes"] == 65536


def test_metrics_query_returns_bounded_prometheus_text():
    _, events = run_host([command("metrics.query", "req-metrics")])
    snapshot = next(event for event in events if event["type"] == "metrics.snapshot")
    assert snapshot["payload"]["format"] == "prometheus_text"
    assert "networkclaw_harness_readiness" in snapshot["payload"]["prometheus"]


def test_all_session_command_paths_are_admitted(tmp_path: Path):
    frames = [open_command(tmp_path / "s"),
              session_command("session.resume", "resume", payload={"workspace_root": str(tmp_path / "s"), "owner_id": "owner-1", "execution_epoch": 1, "lease": lease()}),
              session_command("session.lease.update", "lease-update", payload={"lease": lease(version=2)}),
              session_command("turn.steer", "steer", turn_id="turn-1", payload={"text": "new direction"}),
              session_command("clarification.answer", "clarification", interaction_id="interaction-1", payload={"answer": "yes"}),
              session_command("approval.resolve", "approval", interaction_id="interaction-2", payload={"decision": "deny"}),
              session_command("turn.cancel", "cancel", turn_id="turn-1", payload={}),
              session_command("session.close", "close", payload={})]
    _, events = run_host(frames)
    for request_id in {"req-open", "resume", "lease-update", "steer", "clarification", "approval", "cancel", "close"}:
        assert any(event["request_id"] == request_id and event["type"] == "end" for event in events)


def test_lease_update_must_match_open_session_identity(tmp_path: Path):
    changed = lease()
    changed["owner_id"] = "other-owner"
    _, events = run_host([
        open_command(tmp_path / "s"),
        session_command("session.lease.update", "bad-lease", payload={"lease": changed}),
    ])
    error = next(event for event in events if event["request_id"] == "bad-lease")
    assert error["payload"]["code"] == "lease_lost"


def test_valid_lease_update_is_propagated_to_runtime(tmp_path: Path):
    class Runtime:
        def __init__(self):
            self.opened = None
            self.updated = None

        def open(self, **kwargs):
            self.opened = kwargs

        def update_lease(self, session_id, value):
            self.updated = (session_id, value)

        def close(self, session_id):
            pass

        def handle(self, frame):
            return ()

        def host_disconnected(self, session_ids):
            pass

    runtime = Runtime()
    _, events = run_host([
        open_command(tmp_path / "s"),
        session_command("session.lease.update", "lease-update", payload={"lease": lease(version=2)}),
        command("shutdown", "stop"),
    ], runtime=runtime)
    assert runtime.opened["user_id"] == "user-1"
    assert runtime.updated == ("session-1", lease(version=2))
    assert any(event["request_id"] == "lease-update" and event["type"] == "session.lease.updated"
               for event in events)


def test_runtime_open_failure_does_not_admit_session(tmp_path: Path):
    class Runtime:
        def open(self, **kwargs):
            raise RuntimeError("internal detail")

        def host_disconnected(self, session_ids):
            pass

    _, events = run_host([
        open_command(tmp_path / "s"),
        session_command("user.input", "after-failure", turn_id="turn", payload={"text": "hello"}),
    ], runtime=Runtime())
    opened = [event for event in events if event["request_id"] == "req-open"]
    after = [event for event in events if event["request_id"] == "after-failure"]
    assert opened[-1]["payload"] == {
        "code": "runtime_open_failed", "message": "runtime could not open the assigned session",
    }
    assert after[-1]["payload"]["code"] == "session_not_open"


def test_failed_lease_renewal_fences_session_actions(tmp_path: Path):
    _, events = run_host([
        open_command(tmp_path / "s"),
        session_command("session.lease.update", "lease-failed", payload={"renewal_succeeded": False}),
        session_command("turn.cancel", "after-fence", turn_id="turn-1", payload={}),
    ])
    failed = [event for event in events if event["request_id"] == "lease-failed"]
    fenced = [event for event in events if event["request_id"] == "after-fence"]
    assert failed[-1]["payload"]["code"] == "lease_lost"
    assert fenced[-1]["payload"]["code"] == "lease_lost"


def test_skipped_lease_version_fails_closed(tmp_path: Path):
    _, events = run_host([
        open_command(tmp_path / "s"),
        session_command("session.lease.update", "lease-skipped", payload={"lease": lease(version=3)}),
        session_command("turn.cancel", "after-skipped", turn_id="turn-1", payload={}),
    ])
    skipped = [event for event in events if event["request_id"] == "lease-skipped"]
    fenced = [event for event in events if event["request_id"] == "after-skipped"]
    assert skipped[-1]["payload"]["code"] == "lease_lost"
    assert fenced[-1]["payload"]["code"] == "lease_lost"


def test_incompatible_handshake_exits_with_stable_code():
    code, events = run_host([command("protocol.negotiate", "req", payload={"supported_protocol_versions": ["2.0"]})])
    assert code == 64
    assert events[-1]["payload"]["code"] == "unsupported_protocol_version"


def test_control_frames_preempt_an_active_streaming_turn(tmp_path: Path):
    class Runtime:
        def __init__(self):
            self.started = threading.Event()
            self.cancelled = threading.Event()
            self.steers = []

        def open(self, **kwargs):
            pass

        def close(self, session_id):
            self.cancelled.set()

        def host_disconnected(self, session_ids):
            self.cancelled.set()

        def handle_stream(self, frame, emit):
            emit("turn.started", {"runtime": "test"})
            self.started.set()
            assert self.cancelled.wait(2)
            return (("turn.cancelled", {"status": "cancelled"}),)

        def handle(self, frame):
            if frame.type == "turn.steer":
                self.steers.append(frame.payload["text"])
                return (("turn.controlled", {"command": frame.type, "state": "accepted"}),)
            if frame.type == "turn.cancel":
                self.cancelled.set()
                return (("turn.controlled", {"command": frame.type, "state": "cancel_requested"}),)
            return ()

    runtime = Runtime()
    source = FeedStream()
    output = io.StringIO()
    host = JsonlHost(source, output, runtime=runtime)
    serving = threading.Thread(target=host.serve)
    serving.start()
    source.feed(open_command(tmp_path / "s"))
    source.feed(session_command("user.input", "run", turn_id="turn", run_id="run",
                                payload={"text": "block"}))
    assert runtime.started.wait(1)
    source.feed(session_command("turn.steer", "steer-live", turn_id="turn",
                                run_id="run", payload={"text": "new direction"}))
    source.feed(session_command("turn.cancel", "cancel-live", turn_id="turn",
                                run_id="run", payload={}))
    assert runtime.cancelled.wait(1)
    source.feed(command("shutdown", "stop"))
    source.close()
    serving.join(3)
    assert not serving.is_alive()
    events = [json.loads(line) for line in output.getvalue().splitlines()]
    assert runtime.steers == ["new direction"]
    assert any(event["request_id"] == "cancel-live" and event["type"] == "turn.controlled"
               for event in events)
    assert any(event["request_id"] == "run" and event["type"] == "turn.cancelled"
               for event in events)


def test_chatsvc_flow_resolves_delegation_while_parent_turn_is_active(tmp_path: Path):
    class Runtime:
        def __init__(self):
            self.resolved = threading.Event()

        def open(self, **kwargs):
            pass

        def close(self, session_id):
            pass

        def host_disconnected(self, session_ids):
            pass

        def handle_stream(self, frame, emit):
            emit("delegation.requested", {
                "allocation_id": "allocation-1", "task_index": 0, "task_count": 1,
                "requested_budget": {"max_iterations": 4},
            })
            assert self.resolved.wait(2)
            emit("subagent.started", {"subagent_id": "child-1", "status": "running"})
            emit("subagent.completed", {"subagent_id": "child-1", "status": "completed"})
            return (("turn.completed", {"status": "completed"}),)

        def handle(self, frame):
            if frame.type == "delegation.resolve":
                self.resolved.set()
                return (("turn.controlled", {"command": frame.type, "state": "granted"}),)
            return ()

    runtime = Runtime()
    source = FeedStream()
    output = io.StringIO()
    serving = threading.Thread(target=lambda: JsonlHost(source, output, runtime=runtime).serve())
    serving.start()
    source.feed(open_command(tmp_path / "parent"))
    source.feed(session_command("user.input", "parent-run", turn_id="turn", run_id="run",
                                payload={"text": "delegate"}))
    while "delegation.requested" not in output.getvalue():
        serving.join(0.01)
    source.feed(session_command(
        "delegation.resolve", "grant", turn_id="turn", run_id="run",
        interaction_id="allocation-1", payload={"decision": "grant"},
    ))
    assert runtime.resolved.wait(1)
    source.feed(command("shutdown", "stop"))
    source.close()
    serving.join(3)
    events = [json.loads(line) for line in output.getvalue().splitlines()]
    parent_types = [event["type"] for event in events if event["request_id"] == "parent-run"]
    assert parent_types.index("delegation.requested") < parent_types.index("subagent.started")
    assert parent_types.index("subagent.started") < parent_types.index("subagent.completed")
    assert parent_types.index("subagent.completed") < parent_types.index("turn.completed")
