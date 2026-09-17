import io
import json
from pathlib import Path

from networkclaw_harness.host import JsonlHost


def test_host_separates_acceptance_from_completion_and_reports_h0_state(tmp_path: Path):
    frames = [
        {
            "protocol_version": "1.0",
            "type": "session.open",
            "request_id": "req-open",
            "session_id": "session-1",
            "payload": {
                "workspace_root": str(tmp_path / "session-1"),
                "execution_epoch": "epoch-7",
            },
        },
        {
            "protocol_version": "1.0",
            "type": "user.input",
            "request_id": "req-turn",
            "session_id": "session-1",
            "turn_id": "turn-1",
            "payload": {"text": "hello"},
        },
        {"protocol_version": "1.0", "type": "shutdown", "request_id": "req-stop"},
    ]
    output = io.StringIO()
    host = JsonlHost(io.StringIO("".join(json.dumps(frame) + "\n" for frame in frames)), output)

    assert host.serve() == 0
    events = [json.loads(line) for line in output.getvalue().splitlines()]

    assert [event["sequence"] for event in events] == list(range(1, len(events) + 1))
    assert [event["type"] for event in events] == [
        "request.accepted",
        "session.opened",
        "request.accepted",
        "turn.failed",
        "request.accepted",
        "shutdown.completed",
    ]
    assert events[3]["payload"]["code"] == "runtime_unavailable"

