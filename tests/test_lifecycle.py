import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from networkclaw_harness.lifecycle import (
    HarnessSupervisor, LifecycleError, RestartPolicy, StartMode,
    SupervisorConfig, SupervisorState,
)


REPO_ROOT = Path(__file__).resolve().parents[1]


def config(tmp_path: Path, owner: str, *, mode=StartMode.LAZY, command=None, **values):
    environment = values.pop("environment", {"PYTHONPATH": str(REPO_ROOT / "src")})
    return SupervisorConfig(
        owner_id=owner, runtime_dir=tmp_path / "run", working_directory=REPO_ROOT,
        log_path=tmp_path / f"{owner}.log", start_mode=mode,
        command=tuple(command or (
            sys.executable, "-m", "networkclaw_harness.host",
        )),
        environment=environment, **values,
    )


def lease(session_id: str, owner: str):
    return {
        "session_id": session_id, "owner_id": owner, "lease_id": f"lease-{session_id}",
        "issued_at": "2026-09-17T00:00:00Z", "expires_at": "2026-09-17T00:01:00Z",
        "renew_by": "2026-09-17T00:00:30Z", "grace_expires_at": "2026-09-17T00:01:10Z",
        "lease_version": 1, "execution_epoch": 1,
        "ttl_ms": 60000, "renew_interval_ms": 30000, "grace_ms": 10000,
    }


def wait_for_exit(pid: int, timeout: float = 5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return
        status = Path(f"/proc/{pid}/status")
        if status.exists() and "State:\tZ" in status.read_text():
            return
        time.sleep(0.05)
    raise AssertionError(f"process {pid} did not exit")


def wait_for_supervisor_exit(supervisor: HarnessSupervisor, timeout: float = 5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        status = supervisor.monitor()
        if status["pid"] is None and status["last_exit_code"] is not None:
            return status
        time.sleep(0.05)
    raise AssertionError("supervised process did not exit")


def test_eager_and_lazy_start_have_the_same_handshake(tmp_path: Path):
    eager = HarnessSupervisor(config(tmp_path, "owner-eager", mode=StartMode.EAGER))
    lazy = HarnessSupervisor(config(tmp_path, "owner-lazy"))
    try:
        eager.start_if_eager()
        assert eager.pid is not None and eager.state is SupervisorState.DEGRADED
        assert lazy.pid is None
        lazy.ensure_started()
        assert lazy.pid is not None and lazy.state is SupervisorState.DEGRADED
        assert eager.pid != lazy.pid
        snapshot = eager.monitor()
        assert snapshot["last_heartbeat_at"] is not None
        assert set(snapshot["resources"]) == {"rss_bytes", "cpu_ticks"}
        json.dumps(snapshot)
        assert eager.agent_available is False
        with pytest.raises(LifecycleError, match="health has not opened"):
            eager.send({"protocol_version": "1.0", "type": "user.input", "request_id": "not-ready"})
    finally:
        eager.shutdown()
        lazy.shutdown()


def test_one_harness_reuses_multiple_sessions_and_private_pipes(tmp_path: Path):
    first = HarnessSupervisor(config(tmp_path, "owner-a"))
    second = HarnessSupervisor(config(tmp_path, "owner-b"))
    try:
        first.start()
        second.start()
        first_pid = first.pid
        first.open_session(tenant_id="tenant", user_id="user", session_id="s1", workspace_root=tmp_path / "s1", execution_epoch=1, lease=lease("s1", "owner-a"))
        first.open_session(tenant_id="tenant", user_id="user", session_id="s2", workspace_root=tmp_path / "s2", execution_epoch=1, lease=lease("s2", "owner-a"))
        second.open_session(tenant_id="tenant", user_id="user", session_id="s1", workspace_root=tmp_path / "other-s1", execution_epoch=1, lease=lease("s1", "owner-b"))
        assert first.pid == first_pid
        assert first.pid != second.pid
    finally:
        first.shutdown()
        second.shutdown()


def test_owner_lock_prevents_two_harnesses_for_one_chatsvc(tmp_path: Path):
    first = HarnessSupervisor(config(tmp_path, "same-owner"))
    second = HarnessSupervisor(config(tmp_path, "same-owner"))
    try:
        first.start()
        with pytest.raises(LifecycleError, match="already has"):
            second.start()
    finally:
        first.shutdown()
        second.shutdown()


def test_crash_is_isolated_and_restart_circuit_opens(tmp_path: Path):
    delays = []
    policy = RestartPolicy(max_restarts=1, base_backoff_seconds=0.25, max_backoff_seconds=1)
    failed = HarnessSupervisor(config(tmp_path, "failed", restart=policy), sleep=delays.append)
    healthy = HarnessSupervisor(config(tmp_path, "healthy"))
    try:
        failed.start()
        healthy.start()
        old_pid = failed.pid
        os.kill(old_pid, signal.SIGKILL)
        assert wait_for_supervisor_exit(failed)["last_exit_code"] == -signal.SIGKILL
        assert healthy.probe()["type"] == "health.status"
        failed.recover()
        assert delays == [0.25]
        replacement = failed.pid
        assert replacement is not None and replacement != old_pid
        os.kill(replacement, signal.SIGKILL)
        wait_for_supervisor_exit(failed)
        with pytest.raises(LifecycleError, match="restart limit"):
            failed.recover()
        assert failed.state is SupervisorState.CIRCUIT_OPEN
    finally:
        failed.shutdown()
        healthy.shutdown()


def test_drain_rejects_new_turn_and_normal_shutdown_reaps_child(tmp_path: Path):
    supervisor = HarnessSupervisor(config(tmp_path, "drain"))
    supervisor.start()
    pid = supervisor.pid
    supervisor.drain()
    with pytest.raises(LifecycleError, match="draining"):
        supervisor.send({"protocol_version": "1.0", "type": "user.input", "request_id": "turn"})
    supervisor.shutdown()
    assert supervisor.pid is None and supervisor.state is SupervisorState.STOPPED
    wait_for_exit(pid)


def test_shutdown_timeout_escalates_to_kill(tmp_path: Path):
    command = (sys.executable, str(REPO_ROOT / "tests/fixtures/lifecycle/stubborn_harness.py"))
    supervisor = HarnessSupervisor(config(
        tmp_path, "stubborn", command=command,
        shutdown_timeout_seconds=0.05, terminate_timeout_seconds=0.05,
    ))
    supervisor.start()
    pid = supervisor.pid
    supervisor.shutdown()
    assert supervisor.last_exit_code == -signal.SIGKILL
    wait_for_exit(pid)


def test_startup_and_handshake_failures_are_bounded(tmp_path: Path):
    failed = HarnessSupervisor(config(tmp_path, "start-fail", command=("/usr/bin/false",), handshake_timeout_seconds=0.1))
    with pytest.raises(LifecycleError):
        failed.start()
    invalid = HarnessSupervisor(config(
        tmp_path, "bad-handshake",
        command=(sys.executable, "-c", "import sys; print('not-json', flush=True)"),
        handshake_timeout_seconds=0.2,
    ))
    with pytest.raises(LifecycleError, match="invalid JSONL"):
        invalid.start()


def test_environment_is_allowlisted(tmp_path: Path):
    supervisor = HarnessSupervisor(config(tmp_path, "env", environment={"SECRET_TOKEN": "must-not-pass"}))
    with pytest.raises(LifecycleError, match="not allowlisted"):
        supervisor.start()


def test_host_pipe_disconnect_fences_sessions_and_exits(tmp_path: Path):
    supervisor = HarnessSupervisor(config(tmp_path, "disconnect"))
    supervisor.start()
    supervisor.open_session(tenant_id="tenant", user_id="user", session_id="s1", workspace_root=tmp_path / "s1", execution_epoch=1, lease=lease("s1", "disconnect"))
    pid = supervisor.pid
    supervisor.close_host_pipe()
    status = wait_for_supervisor_exit(supervisor)
    assert status["last_exit_code"] == 75


def test_parent_sigkill_does_not_leave_harness_orphan(tmp_path: Path):
    pid_file = tmp_path / "harness.pid"
    owner = subprocess.Popen(
        [sys.executable, str(REPO_ROOT / "tests/fixtures/lifecycle/orphan_owner.py"), str(pid_file)],
        cwd=REPO_ROOT, env={**os.environ, "PYTHONPATH": str(REPO_ROOT / "src")},
    )
    deadline = time.monotonic() + 5
    while not pid_file.exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    assert pid_file.exists()
    harness_pid = int(pid_file.read_text())
    os.kill(owner.pid, signal.SIGKILL)
    owner.wait(timeout=2)
    wait_for_exit(harness_pid)
