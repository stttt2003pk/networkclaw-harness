import json
import os
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


class _ProviderHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        request = json.loads(self.rfile.read(length))
        if request.get("stream"):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            chunks = (
                {"id": "probe", "object": "chat.completion.chunk", "created": 1,
                 "model": "gpt-5.5", "choices": [{"index": 0, "delta": {"role": "assistant"},
                                                     "finish_reason": None}]},
                {"id": "probe", "object": "chat.completion.chunk", "created": 1,
                 "model": "gpt-5.5", "choices": [{"index": 0, "delta": {"content": "vertical-ok"},
                                                     "finish_reason": None}]},
                {"id": "probe", "object": "chat.completion.chunk", "created": 1,
                 "model": "gpt-5.5", "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                 "usage": {"prompt_tokens": 4, "completion_tokens": 2, "total_tokens": 6}},
            )
            for chunk in chunks:
                self.wfile.write(f"data: {json.dumps(chunk)}\n\n".encode())
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
            return
        body = {
            "id": "probe", "object": "chat.completion", "created": 1, "model": "gpt-5.5",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "vertical-ok"},
                         "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 4, "completion_tokens": 2, "total_tokens": 6},
        }
        encoded = json.dumps(body).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def log_message(self, format, *args):
        return


class _ToolProviderHandler(BaseHTTPRequestHandler):
    calls = 0
    tool_calls = 0
    observations = []

    def do_POST(self):
        type(self).calls += 1
        length = int(self.headers.get("Content-Length", "0"))
        request = json.loads(self.rfile.read(length))
        tool_names = [item.get("function", {}).get("name") for item in request.get("tools", [])]
        if not tool_names:
            return self._respond_text("auxiliary-ok")
        assert "networkclaw_workspace_read" in tool_names
        assert "todo_list" in tool_names
        assert "delegate_task" in tool_names
        type(self).tool_calls += 1
        has_result = any(message.get("role") == "tool" for message in request.get("messages", []))
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        if not has_result:
            chunks = (
                {"id": "tool-probe", "object": "chat.completion.chunk", "created": 1,
                 "model": "gpt-5.5", "choices": [{"index": 0, "delta": {
                     "role": "assistant", "tool_calls": [{
                         "index": 0, "id": "call-workspace-read", "type": "function",
                         "function": {"name": "networkclaw_workspace_read", "arguments": "{\"path\":\"input.txt\"}"},
                     }],
                 }, "finish_reason": None}]},
                {"id": "tool-probe", "object": "chat.completion.chunk", "created": 1,
                 "model": "gpt-5.5", "choices": [{"index": 0, "delta": {},
                                                     "finish_reason": "tool_calls"}]},
            )
        else:
            tool_message = next(message for message in request["messages"] if message.get("role") == "tool")
            type(self).observations.append(tool_message["content"])
            chunks = (
                {"id": "tool-probe", "object": "chat.completion.chunk", "created": 1,
                 "model": "gpt-5.5", "choices": [{"index": 0,
                     "delta": {"role": "assistant", "content": "tool-vertical-ok"},
                     "finish_reason": None}]},
                {"id": "tool-probe", "object": "chat.completion.chunk", "created": 1,
                 "model": "gpt-5.5", "choices": [{"index": 0, "delta": {},
                                                     "finish_reason": "stop"}],
                 "usage": {"prompt_tokens": 8, "completion_tokens": 2, "total_tokens": 10}},
            )
        for chunk in chunks:
            self.wfile.write(f"data: {json.dumps(chunk)}\n\n".encode())
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()

    def _respond_text(self, content):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        chunk = {"id": "aux", "object": "chat.completion.chunk", "created": 1,
                 "model": "gpt-5.5", "choices": [{"index": 0,
                     "delta": {"role": "assistant", "content": content},
                     "finish_reason": "stop"}]}
        self.wfile.write(f"data: {json.dumps(chunk)}\n\n".encode())
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()

    def log_message(self, format, *args):
        return


class _SlowProviderHandler(BaseHTTPRequestHandler):
    started = threading.Event()

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        self.rfile.read(length)
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        role = {"id": "slow", "object": "chat.completion.chunk", "created": 1,
                "model": "gpt-5.5", "choices": [{"index": 0,
                    "delta": {"role": "assistant"}, "finish_reason": None}]}
        self.wfile.write(f"data: {json.dumps(role)}\n\n".encode())
        self.wfile.flush()
        type(self).started.set()
        for _ in range(200):
            chunk = {"id": "slow", "object": "chat.completion.chunk", "created": 1,
                     "model": "gpt-5.5", "choices": [{"index": 0,
                         "delta": {"content": "."}, "finish_reason": None}]}
            try:
                self.wfile.write(f"data: {json.dumps(chunk)}\n\n".encode())
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                return
            time.sleep(0.02)
        finish = {"id": "slow", "object": "chat.completion.chunk", "created": 1,
                  "model": "gpt-5.5", "choices": [{"index": 0, "delta": {},
                      "finish_reason": "stop"}]}
        self.wfile.write(f"data: {json.dumps(finish)}\n\ndata: [DONE]\n\n".encode())
        self.wfile.flush()

    def log_message(self, format, *args):
        return


class _SteerProviderHandler(BaseHTTPRequestHandler):
    started = threading.Event()
    lock = threading.Lock()
    calls = 0
    requests = []

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        request = json.loads(self.rfile.read(length))
        with type(self).lock:
            type(self).calls += 1
            call = type(self).calls
            type(self).requests.append(request)
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        if call == 1:
            role = {"id": "steer-1", "object": "chat.completion.chunk", "created": 1,
                    "model": "gpt-5.5", "choices": [{"index": 0,
                        "delta": {"role": "assistant"}, "finish_reason": None}]}
            self.wfile.write(f"data: {json.dumps(role)}\n\n".encode())
            self.wfile.flush()
            type(self).started.set()
            for _ in range(300):
                chunk = {"id": "steer-1", "object": "chat.completion.chunk", "created": 1,
                         "model": "gpt-5.5", "choices": [{"index": 0,
                             "delta": {"content": "."}, "finish_reason": None}]}
                try:
                    self.wfile.write(f"data: {json.dumps(chunk)}\n\n".encode())
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    return
                time.sleep(0.02)
            return
        chunks = (
            {"id": "steer-2", "object": "chat.completion.chunk", "created": 1,
             "model": "gpt-5.5", "choices": [{"index": 0,
                 "delta": {"role": "assistant", "content": "steered-ok"}, "finish_reason": None}]},
            {"id": "steer-2", "object": "chat.completion.chunk", "created": 1,
             "model": "gpt-5.5", "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
        )
        for chunk in chunks:
            self.wfile.write(f"data: {json.dumps(chunk)}\n\n".encode())
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()

    def log_message(self, format, *args):
        return


def _exchange(process, request):
    process.stdin.write(json.dumps(request) + "\n")
    process.stdin.flush()
    frames = []
    while True:
        line = process.stdout.readline()
        assert line, process.stderr.read()
        frame = json.loads(line)
        frames.append(frame)
        if frame.get("end"):
            return frames


def _collect_requests(process, request_ids, timeout=10):
    frames = {request_id: [] for request_id in request_ids}
    terminal = set()
    deadline = time.monotonic() + timeout
    while terminal != set(request_ids):
        assert time.monotonic() < deadline, frames
        line = process.stdout.readline()
        assert line, process.stderr.read()
        frame = json.loads(line)
        request_id = frame.get("request_id")
        if request_id in frames:
            frames[request_id].append(frame)
            if frame.get("end"):
                terminal.add(request_id)
    return frames


def test_real_vendored_hermes_serves_minimal_jsonl_vertical(tmp_path: Path):
    server = ThreadingHTTPServer(("127.0.0.1", 0), _ProviderHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    root = Path(__file__).resolve().parents[1]
    env = os.environ.copy()
    env.update({
        "PYTHONPATH": str(root / "src"),
        "OPENAI_API_KEY": "local-test-key",
        "OPENAI_BASE_URL": f"http://127.0.0.1:{server.server_port}/v1",
        "OPENAI_MODEL": "gpt-5.5",
        "NETWORKCLAW_HARNESS_ALLOWED_MODELS": "gpt-5.5",
        "NETWORKCLAW_HARNESS_ALLOWED_EGRESS_HOSTS": "127.0.0.1",
        "NETWORKCLAW_HARNESS_PROVIDER_STREAM": "true",
    })
    process = subprocess.Popen(
        [sys.executable, "-m", "networkclaw_harness.host", "--runtime-mode", "hermes_adapter",
         "--provider-mode", "live"],
        cwd=root, env=env, text=True,
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    assert process.stdin is not None and process.stdout is not None and process.stderr is not None
    try:
        negotiated = _exchange(process, {
            "protocol_version": "1.0", "type": "protocol.negotiate", "request_id": "negotiate",
            "payload": {"supported_protocol_versions": ["1.0"]},
        })
        assert any(frame["type"] == "protocol.negotiated" for frame in negotiated)
        issued = datetime.now(timezone.utc)
        timestamp = lambda value: value.isoformat().replace("+00:00", "Z")
        lease = {
            "session_id": "session", "owner_id": "owner", "lease_id": "lease",
            "lease_version": 1, "execution_epoch": 1,
            "issued_at": timestamp(issued), "renew_by": timestamp(issued + timedelta(seconds=30)),
            "expires_at": timestamp(issued + timedelta(seconds=60)),
            "grace_expires_at": timestamp(issued + timedelta(seconds=70)),
            "ttl_ms": 60000, "renew_interval_ms": 30000, "grace_ms": 10000,
        }
        opened = _exchange(process, {
            "protocol_version": "1.0", "type": "session.open", "request_id": "open",
            "tenant_id": "tenant", "user_id": "user", "session_id": "session",
            "payload": {"workspace_root": str(tmp_path / "workspace"), "owner_id": "owner",
                        "execution_epoch": 1, "lease": lease},
        })
        assert any(frame["type"] == "session.opened" for frame in opened), opened
        turn = _exchange(process, {
            "protocol_version": "1.0", "type": "user.input", "request_id": "input",
            "tenant_id": "tenant", "user_id": "user", "session_id": "session",
            "turn_id": "turn", "run_id": "run",
            "payload": {"text": "reply briefly", "provider_selection": {
                "provider_id": "openai", "model_id": "gpt-5.5", "config_ref": "env:openai",
            }},
        })
        failures = [frame["payload"] for frame in turn if frame["type"] == "turn.failed"]
        if failures:
            _exchange(process, {
                "protocol_version": "1.0", "type": "shutdown", "request_id": "failed-shutdown", "payload": {},
            })
            process.wait(timeout=10)
            raise AssertionError(f"{failures}; stderr={process.stderr.read()}")
        assert any(frame["type"] == "assistant.delta" and
                   frame["payload"].get("content") == "vertical-ok" for frame in turn), [
                       (frame["type"], frame.get("payload")) for frame in turn
                   ]
        assert any(frame["type"] == "turn.completed" for frame in turn)
        assert not any(frame["type"] in {"error", "turn.failed"} for frame in turn)
        _exchange(process, {
            "protocol_version": "1.0", "type": "shutdown", "request_id": "shutdown", "payload": {},
        })
        assert process.wait(timeout=10) == 0
    finally:
        server.shutdown()
        server.server_close()
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)


def test_real_vendored_hermes_executes_networkclaw_workspace_tool(tmp_path: Path):
    _ToolProviderHandler.calls = 0
    _ToolProviderHandler.tool_calls = 0
    _ToolProviderHandler.observations = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), _ToolProviderHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    root = Path(__file__).resolve().parents[1]
    env = os.environ.copy()
    env.update({
        "PYTHONPATH": str(root / "src"),
        "OPENAI_API_KEY": "local-test-key",
        "OPENAI_BASE_URL": f"http://127.0.0.1:{server.server_port}/v1",
        "OPENAI_MODEL": "gpt-5.5",
        "NETWORKCLAW_HARNESS_ALLOWED_MODELS": "gpt-5.5",
        "NETWORKCLAW_HARNESS_ALLOWED_EGRESS_HOSTS": "127.0.0.1",
        "NETWORKCLAW_HARNESS_PROVIDER_STREAM": "true",
    })
    process = subprocess.Popen(
        [sys.executable, "-m", "networkclaw_harness.host", "--runtime-mode", "hermes_adapter",
         "--provider-mode", "live"],
        cwd=root, env=env, text=True,
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    assert process.stdin is not None and process.stdout is not None and process.stderr is not None
    try:
        _exchange(process, {
            "protocol_version": "1.0", "type": "protocol.negotiate", "request_id": "negotiate",
            "payload": {"supported_protocol_versions": ["1.0"]},
        })
        issued = datetime.now(timezone.utc)
        timestamp = lambda value: value.isoformat().replace("+00:00", "Z")
        lease = {
            "session_id": "session", "owner_id": "owner", "lease_id": "lease",
            "lease_version": 1, "execution_epoch": 1,
            "issued_at": timestamp(issued), "renew_by": timestamp(issued + timedelta(seconds=30)),
            "expires_at": timestamp(issued + timedelta(seconds=60)),
            "grace_expires_at": timestamp(issued + timedelta(seconds=70)),
            "ttl_ms": 60000, "renew_interval_ms": 30000, "grace_ms": 10000,
        }
        workspace = tmp_path / "workspace"
        opened = _exchange(process, {
            "protocol_version": "1.0", "type": "session.open", "request_id": "open",
            "tenant_id": "tenant", "user_id": "user", "session_id": "session",
            "payload": {"workspace_root": str(workspace), "owner_id": "owner",
                        "execution_epoch": 1, "lease": lease},
        })
        assert any(frame["type"] == "session.opened" for frame in opened), opened
        (workspace / "input.txt").write_text("workspace-tool-ok", encoding="utf-8")
        turn = _exchange(process, {
            "protocol_version": "1.0", "type": "user.input", "request_id": "input",
            "tenant_id": "tenant", "user_id": "user", "session_id": "session",
            "turn_id": "turn", "run_id": "run",
            "payload": {"text": "read input.txt", "provider_selection": {
                "provider_id": "openai", "model_id": "gpt-5.5", "config_ref": "env:openai",
            }},
        })
        _exchange(process, {
            "protocol_version": "1.0", "type": "shutdown", "request_id": "shutdown", "payload": {},
        })
        assert process.wait(timeout=10) == 0
        stderr = process.stderr.read()
        assert _ToolProviderHandler.tool_calls == 2, (_ToolProviderHandler.calls, turn, stderr)
        observation = json.loads(_ToolProviderHandler.observations[-1])
        assert observation.get("content") == "workspace-tool-ok", (observation, stderr)
        assert any(frame["type"] == "tool.started" and
                   frame["payload"].get("invocation_id") for frame in turn), turn
        assert any(frame["type"] == "tool.completed" and
                   frame["payload"].get("status") == "succeeded" for frame in turn), turn
        streamed = "".join(
            frame["payload"].get("content", "")
            for frame in turn if frame["type"] == "assistant.delta"
        )
        assert "tool-vertical-ok" in streamed, (streamed, turn)
        assert any(frame["type"] == "turn.completed" for frame in turn), turn
        durable_bytes = (workspace / "session-state" / "durable.db").read_bytes()
        assert b"workspace-tool-ok" not in durable_bytes
    finally:
        server.shutdown()
        server.server_close()
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)


def test_real_vendored_hermes_cancel_preempts_active_provider_stream(tmp_path: Path):
    _SlowProviderHandler.started = threading.Event()
    server = ThreadingHTTPServer(("127.0.0.1", 0), _SlowProviderHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    root = Path(__file__).resolve().parents[1]
    env = os.environ.copy()
    env.update({
        "PYTHONPATH": str(root / "src"), "OPENAI_API_KEY": "local-test-key",
        "OPENAI_BASE_URL": f"http://127.0.0.1:{server.server_port}/v1",
        "OPENAI_MODEL": "gpt-5.5", "NETWORKCLAW_HARNESS_ALLOWED_MODELS": "gpt-5.5",
        "NETWORKCLAW_HARNESS_ALLOWED_EGRESS_HOSTS": "127.0.0.1",
        "NETWORKCLAW_HARNESS_PROVIDER_STREAM": "true",
    })
    process = subprocess.Popen(
        [sys.executable, "-m", "networkclaw_harness.host", "--runtime-mode", "hermes_adapter",
         "--provider-mode", "live"], cwd=root, env=env, text=True,
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    assert process.stdin is not None and process.stdout is not None and process.stderr is not None
    try:
        _exchange(process, {"protocol_version": "1.0", "type": "protocol.negotiate",
                            "request_id": "negotiate", "payload": {"supported_protocol_versions": ["1.0"]}})
        issued = datetime.now(timezone.utc)
        timestamp = lambda value: value.isoformat().replace("+00:00", "Z")
        lease = {"session_id": "session", "owner_id": "owner", "lease_id": "lease",
                 "lease_version": 1, "execution_epoch": 1, "issued_at": timestamp(issued),
                 "renew_by": timestamp(issued + timedelta(seconds=30)),
                 "expires_at": timestamp(issued + timedelta(seconds=60)),
                 "grace_expires_at": timestamp(issued + timedelta(seconds=70)),
                 "ttl_ms": 60000, "renew_interval_ms": 30000, "grace_ms": 10000}
        _exchange(process, {"protocol_version": "1.0", "type": "session.open", "request_id": "open",
                            "tenant_id": "tenant", "user_id": "user", "session_id": "session",
                            "payload": {"workspace_root": str(tmp_path / "workspace"), "owner_id": "owner",
                                        "execution_epoch": 1, "lease": lease}})
        turn = {"protocol_version": "1.0", "type": "user.input", "request_id": "turn",
                "tenant_id": "tenant", "user_id": "user", "session_id": "session",
                "turn_id": "turn", "run_id": "run", "payload": {"text": "stream slowly",
                    "provider_selection": {"provider_id": "openai", "model_id": "gpt-5.5",
                                           "config_ref": "env:openai"}}}
        process.stdin.write(json.dumps(turn) + "\n")
        process.stdin.flush()
        streams = {"turn": [], "cancel": []}
        terminals = set()
        turn_started = threading.Event()

        def read_streams():
            while terminals != {"turn", "cancel"}:
                line = process.stdout.readline()
                if not line:
                    return
                frame = json.loads(line)
                request_id = frame.get("request_id")
                if request_id in streams:
                    streams[request_id].append(frame)
                    if request_id == "turn" and frame.get("type") == "turn.started":
                        turn_started.set()
                    if frame.get("end"):
                        terminals.add(request_id)

        reader = threading.Thread(target=read_streams, daemon=True)
        reader.start()
        assert turn_started.wait(15), streams
        assert _SlowProviderHandler.started.wait(15)
        cancel = {"protocol_version": "1.0", "type": "turn.cancel", "request_id": "cancel",
                  "tenant_id": "tenant", "user_id": "user", "session_id": "session",
                  "turn_id": "turn", "run_id": "run", "payload": {}}
        process.stdin.write(json.dumps(cancel) + "\n")
        process.stdin.flush()
        reader.join(10)
        assert not reader.is_alive(), streams
        controlled = next(frame for frame in streams["cancel"] if frame["type"] == "turn.controlled")
        assert controlled["payload"].get("state") == "cancel_requested"
        assert any(frame["type"] == "turn.cancelled" for frame in streams["turn"]), streams["turn"]
        assert sum(frame["type"] == "assistant.delta" for frame in streams["turn"]) < 50
        _exchange(process, {"protocol_version": "1.0", "type": "shutdown",
                            "request_id": "shutdown", "payload": {}})
        assert process.wait(timeout=10) == 0
    finally:
        server.shutdown()
        server.server_close()
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)


def test_real_vendored_hermes_steer_redirects_active_generation(tmp_path: Path):
    _SteerProviderHandler.started = threading.Event()
    _SteerProviderHandler.calls = 0
    _SteerProviderHandler.requests = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), _SteerProviderHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    root = Path(__file__).resolve().parents[1]
    env = os.environ.copy()
    env.update({"PYTHONPATH": str(root / "src"), "OPENAI_API_KEY": "local-test-key",
                "OPENAI_BASE_URL": f"http://127.0.0.1:{server.server_port}/v1",
                "OPENAI_MODEL": "gpt-5.5", "NETWORKCLAW_HARNESS_ALLOWED_MODELS": "gpt-5.5",
                "NETWORKCLAW_HARNESS_ALLOWED_EGRESS_HOSTS": "127.0.0.1",
                "NETWORKCLAW_HARNESS_PROVIDER_STREAM": "true"})
    process = subprocess.Popen(
        [sys.executable, "-m", "networkclaw_harness.host", "--runtime-mode", "hermes_adapter",
         "--provider-mode", "live"], cwd=root, env=env, text=True,
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    assert process.stdin is not None and process.stdout is not None and process.stderr is not None
    issued = datetime.now(timezone.utc)
    timestamp = lambda value: value.isoformat().replace("+00:00", "Z")
    lease = {"session_id": "session", "owner_id": "owner", "lease_id": "lease",
             "lease_version": 1, "execution_epoch": 1, "issued_at": timestamp(issued),
             "renew_by": timestamp(issued + timedelta(seconds=30)),
             "expires_at": timestamp(issued + timedelta(seconds=60)),
             "grace_expires_at": timestamp(issued + timedelta(seconds=70)),
             "ttl_ms": 60000, "renew_interval_ms": 30000, "grace_ms": 10000}
    try:
        _exchange(process, {"protocol_version": "1.0", "type": "protocol.negotiate",
                            "request_id": "negotiate", "payload": {"supported_protocol_versions": ["1.0"]}})
        _exchange(process, {"protocol_version": "1.0", "type": "session.open", "request_id": "open",
                            "tenant_id": "tenant", "user_id": "user", "session_id": "session",
                            "payload": {"workspace_root": str(tmp_path / "workspace"), "owner_id": "owner",
                                        "execution_epoch": 1, "lease": lease}})
        process.stdin.write(json.dumps({
            "protocol_version": "1.0", "type": "user.input", "request_id": "turn",
            "tenant_id": "tenant", "user_id": "user", "session_id": "session",
            "turn_id": "turn", "run_id": "run", "payload": {"text": "start long answer",
                "provider_selection": {"provider_id": "openai", "model_id": "gpt-5.5",
                                       "config_ref": "env:openai"}},
        }) + "\n")
        process.stdin.flush()
        frames = {"turn": [], "steer": []}
        terminal = set()
        generation_active = threading.Event()

        def reader():
            while terminal != {"turn", "steer"}:
                frame = json.loads(process.stdout.readline())
                request_id = frame.get("request_id")
                if request_id in frames:
                    frames[request_id].append(frame)
                    if request_id == "turn" and frame.get("type") == "assistant.delta":
                        generation_active.set()
                    if frame.get("end"):
                        terminal.add(request_id)

        reading = threading.Thread(target=reader, daemon=True)
        reading.start()
        assert _SteerProviderHandler.started.wait(15)
        assert generation_active.wait(15), frames
        process.stdin.write(json.dumps({
            "protocol_version": "1.0", "type": "turn.steer", "request_id": "steer",
            "tenant_id": "tenant", "user_id": "user", "session_id": "session",
            "turn_id": "turn", "run_id": "run", "payload": {"text": "answer only with steered-ok"},
        }) + "\n")
        process.stdin.flush()
        reading.join(15)
        assert not reading.is_alive(), frames
        controlled = next(frame for frame in frames["steer"] if frame["type"] == "turn.controlled")
        assert controlled["payload"]["state"] == "accepted"
        assert _SteerProviderHandler.calls >= 2
        assert any(
            "answer only with steered-ok" in json.dumps(request.get("messages", []))
            for request in _SteerProviderHandler.requests[1:]
        )
        assert "steered-ok" in "".join(
            frame["payload"].get("content", "") for frame in frames["turn"]
            if frame["type"] == "assistant.delta"
        )
        assert any(frame["type"] == "turn.completed" for frame in frames["turn"])
        _exchange(process, {"protocol_version": "1.0", "type": "shutdown",
                            "request_id": "shutdown", "payload": {}})
        assert process.wait(timeout=10) == 0
    finally:
        server.shutdown()
        server.server_close()
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)
