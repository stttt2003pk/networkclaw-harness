import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import socket
import stat
import threading
import time
from pathlib import Path

from networkclaw_harness.host import UnixJsonlGateway


def frame(kind: str, request_id: str, session_id: str | None = None, **extra):
    value = {"protocol_version": "1.0", "type": kind, "request_id": request_id}
    if session_id is not None:
        value.update({"tenant_id": "tenant", "user_id": "user", "session_id": session_id})
    value.update(extra)
    return value


def lease(session_id: str, *, version: int = 1):
    suffix = "00:00:00" if version == 1 else "00:00:20"
    return {
        "session_id": session_id,
        "owner_id": "owner",
        "lease_id": f"lease-{session_id}",
        "issued_at": f"2026-09-17T{suffix}Z",
        "expires_at": f"2026-09-17T00:01:{'00' if version == 1 else '20'}Z",
        "renew_by": f"2026-09-17T00:00:{'30' if version == 1 else '50'}Z",
        "grace_expires_at": f"2026-09-17T00:01:{'10' if version == 1 else '30'}Z",
        "lease_version": version,
        "execution_epoch": 1,
        "ttl_ms": 60000,
        "renew_interval_ms": 30000,
        "grace_ms": 10000,
    }


class Runtime:
    def __init__(self):
        self.fenced: list[tuple[str, str]] = []
        self.disconnected: list[tuple[str, ...]] = []

    def open(self, **kwargs):
        pass

    def close(self, session_id):
        pass

    def host_disconnected(self, session_ids):
        self.disconnected.append(tuple(session_ids))

    def fence(self, session_id, reason="lease_lost"):
        self.fenced.append((session_id, reason))

    def handle(self, input_frame):
        if input_frame.type == "user.input":
            return (("turn.completed", {"status": "completed"}),)
        return ()


def read_request(reader, request_id: str):
    events = []
    while True:
        line = reader.readline()
        assert line
        event = json.loads(line)
        if event["request_id"] == request_id:
            events.append(event)
            if event["end"]:
                return events


def wait_for_socket(path: Path):
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        if path.exists():
            return
        time.sleep(0.01)
    raise AssertionError("Gateway did not create its Unix socket")


def test_gateway_serves_jsonl_over_unix_socket_and_cleans_up(tmp_path: Path):
    socket_dir = Path(tempfile.mkdtemp(prefix="ncg-", dir="/tmp"))
    socket_path = socket_dir / "g.sock"
    runtime = Runtime()
    gateway = UnixJsonlGateway(socket_path, runtime=runtime)
    serving = threading.Thread(target=gateway.serve)
    serving.start()
    wait_for_socket(socket_path)
    assert stat.S_IMODE(socket_path.stat().st_mode) == 0o600
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.connect(str(socket_path))
    reader = client.makefile("r", encoding="utf-8")
    writer = client.makefile("w", encoding="utf-8")

    def send(value):
        writer.write(json.dumps(value) + "\n")
        writer.flush()

    try:
        send(frame("protocol.negotiate", "neg", payload={"supported_protocol_versions": ["1.0"]}))
        assert any(event["type"] == "protocol.negotiated" for event in read_request(reader, "neg"))
        send(frame("health.query", "health"))
        assert any(event["type"] == "health.status" for event in read_request(reader, "health"))
        for session_id in ("s1", "s2"):
            send(frame("session.open", f"open-{session_id}", session_id,
                       payload={"workspace_root": str(tmp_path / session_id), "owner_id": "owner",
                                "execution_epoch": 1, "lease": lease(session_id)}))
            assert any(event["type"] == "session.opened" for event in read_request(reader, f"open-{session_id}"))
        probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        probe.connect(str(socket_path))
        probe_reader = probe.makefile("r", encoding="utf-8")
        probe_writer = probe.makefile("w", encoding="utf-8")
        try:
            probe_writer.write(json.dumps(frame("health.query", "parallel-health")) + "\n")
            probe_writer.flush()
            assert any(event["type"] == "health.status" for event in read_request(probe_reader, "parallel-health"))
        finally:
            probe_writer.close()
            probe_reader.close()
            probe.close()
        send(frame("session.lease.update", "fence-s1", "s1",
                   payload={"operation": "revoke", "reason_code": "platform_lease_lost"}))
        assert any(event["type"] == "session.lease.revoked" for event in read_request(reader, "fence-s1"))
        send(frame("user.input", "turn-s2", "s2", turn_id="t2", payload={"text": "hello"}))
        assert any(event["type"] == "turn.completed" for event in read_request(reader, "turn-s2"))
        assert runtime.fenced == [("s1", "platform_lease_lost")]
        send(frame("shutdown", "stop"))
        assert any(event["type"] == "shutdown.completed" for event in read_request(reader, "stop"))
    finally:
        writer.close()
        reader.close()
        client.close()
        serving.join(2)
        gateway.close()
    assert not serving.is_alive()
    assert not socket_path.exists()
    socket_dir.rmdir()


def test_gateway_cli_process_owns_unix_socket_and_keeps_stdout_clean():
    socket_dir = Path(tempfile.mkdtemp(prefix="ncg-", dir="/tmp"))
    socket_path = socket_dir / "g.sock"
    environment = {**os.environ, "PYTHONPATH": str(Path(__file__).parents[1] / "src")}
    process = subprocess.Popen(
        [sys.executable, "-m", "networkclaw_harness.host", "--socket-path", str(socket_path),
         "--provider-mode", "live"],
        cwd=Path(__file__).parents[1], env=environment,
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True,
    )
    try:
        wait_for_socket(socket_path)
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        client.connect(str(socket_path))
        reader = client.makefile("r", encoding="utf-8")
        writer = client.makefile("w", encoding="utf-8")
        try:
            writer.write(json.dumps(frame("protocol.negotiate", "neg", payload={
                "supported_protocol_versions": ["1.0"],
            })) + "\n")
            writer.flush()
            assert any(event["type"] == "protocol.negotiated" for event in read_request(reader, "neg"))
            writer.write(json.dumps(frame("health.query", "ready")) + "\n")
            writer.flush()
            assert any(event["type"] == "health.status" and event["payload"]["status"] == "ready"
                       for event in read_request(reader, "ready"))
            writer.write(json.dumps(frame("shutdown", "stop")) + "\n")
            writer.flush()
            assert any(event["type"] == "shutdown.completed" for event in read_request(reader, "stop"))
        finally:
            writer.close()
            reader.close()
            client.close()
        assert process.wait(timeout=3) == 0
        assert process.stdout is not None
        assert process.stdout.read() == ""
    finally:
        if process.poll() is None:
            process.terminate()
            process.wait(timeout=3)
        assert not socket_path.exists()
        socket_dir.rmdir()


def test_gateway_sigterm_cleans_socket():
    socket_dir = Path(tempfile.mkdtemp(prefix="ncg-", dir="/tmp"))
    socket_path = socket_dir / "g.sock"
    environment = {**os.environ, "PYTHONPATH": str(Path(__file__).parents[1] / "src")}
    process = subprocess.Popen(
        [sys.executable, "-m", "networkclaw_harness.host", "--socket-path", str(socket_path)],
        cwd=Path(__file__).parents[1], env=environment,
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True,
    )
    try:
        wait_for_socket(socket_path)
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        client.connect(str(socket_path))
        reader = client.makefile("r", encoding="utf-8")
        writer = client.makefile("w", encoding="utf-8")
        writer.write(json.dumps(frame("health.query", "ready")) + "\n")
        writer.flush()
        assert any(event["type"] == "health.status" for event in read_request(reader, "ready"))
        process.terminate()
        assert process.wait(timeout=3) == 0
        assert not socket_path.exists()
        writer.close()
        reader.close()
        client.close()
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=3)
        socket_path.unlink(missing_ok=True)
        socket_dir.rmdir()


def test_gateway_client_disconnect_releases_its_sessions():
    socket_dir = Path(tempfile.mkdtemp(prefix="ncg-", dir="/tmp"))
    socket_path = socket_dir / "g.sock"
    runtime = Runtime()
    gateway = UnixJsonlGateway(socket_path, runtime=runtime)
    serving = threading.Thread(target=gateway.serve)
    serving.start()
    try:
        wait_for_socket(socket_path)
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        client.connect(str(socket_path))
        reader = client.makefile("r", encoding="utf-8")
        writer = client.makefile("w", encoding="utf-8")
        writer.write(json.dumps(frame("session.open", "open", "s1", payload={
            "workspace_root": str(socket_dir / "workspace"), "owner_id": "owner",
            "execution_epoch": 1, "lease": lease("s1"),
        })) + "\n")
        writer.flush()
        assert any(event["type"] == "session.opened" for event in read_request(reader, "open"))
        writer.close()
        reader.close()
        client.close()
        deadline = time.monotonic() + 2
        while not runtime.disconnected and time.monotonic() < deadline:
            time.sleep(0.01)
        assert runtime.disconnected == [("s1",)]
        probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        probe.connect(str(socket_path))
        probe_reader = probe.makefile("r", encoding="utf-8")
        probe_writer = probe.makefile("w", encoding="utf-8")
        probe_writer.write(json.dumps(frame("health.query", "probe")) + "\n")
        probe_writer.flush()
        assert any(event["type"] == "health.status" for event in read_request(probe_reader, "probe"))
        probe_writer.write(json.dumps(frame("shutdown", "stop")) + "\n")
        probe_writer.flush()
        assert any(event["type"] == "shutdown.completed" for event in read_request(probe_reader, "stop"))
        probe_writer.close()
        probe_reader.close()
        probe.close()
    finally:
        gateway.close()
        serving.join(2)
        shutil.rmtree(socket_dir)


def test_gateway_refuses_existing_socket_path(tmp_path: Path):
    socket_path = tmp_path / "gateway.sock"
    socket_path.write_text("owned by another process")
    gateway = UnixJsonlGateway(socket_path, runtime=Runtime())
    try:
        gateway.serve()
    except RuntimeError as error:
        assert "already exists" in str(error)
    else:
        raise AssertionError("Gateway overwrote an existing socket path")
    assert socket_path.read_text() == "owned by another process"


def test_gateway_parent_death_removes_socket():
    socket_dir = Path(tempfile.mkdtemp(prefix="ncg-", dir="/tmp"))
    socket_path = socket_dir / "g.sock"
    pid_file = socket_dir / "child.pid"
    root = Path(__file__).parents[1]
    environment = {**os.environ, "PYTHONPATH": str(root / "src")}
    owner = subprocess.Popen(
        [sys.executable, str(root / "tests/fixtures/lifecycle/orphan_owner.py"),
         str(pid_file), "--socket-path", str(socket_path)],
        cwd=root, env=environment,
    )
    try:
        wait_for_socket(socket_path)
        assert pid_file.exists()
        os.kill(owner.pid, signal.SIGKILL)
        owner.wait(timeout=2)
        deadline = time.monotonic() + 3
        while socket_path.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert not socket_path.exists()
    finally:
        if owner.poll() is None:
            owner.kill()
            owner.wait(timeout=2)
        if pid_file.exists():
            child_pid = int(pid_file.read_text())
            try:
                os.kill(child_pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        shutil.rmtree(socket_dir)
