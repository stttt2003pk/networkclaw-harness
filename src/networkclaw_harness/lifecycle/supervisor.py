"""A chatsvc-side reference supervisor for one dedicated Harness child."""

from __future__ import annotations

import enum
import fcntl
import hashlib
import json
import os
import selectors
import signal
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, BinaryIO, Callable, Mapping


class LifecycleError(RuntimeError):
    """The owned Harness could not satisfy its lifecycle contract."""


class StartMode(enum.StrEnum):
    EAGER = "eager"
    LAZY = "lazy"


class SupervisorState(enum.StrEnum):
    STOPPED = "stopped"
    STARTING = "starting"
    READY = "ready"
    DEGRADED = "degraded"
    DRAINING = "draining"
    BACKOFF = "backoff"
    CIRCUIT_OPEN = "circuit_open"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class RestartPolicy:
    max_restarts: int = 3
    window_seconds: float = 60.0
    base_backoff_seconds: float = 0.1
    max_backoff_seconds: float = 5.0


@dataclass(frozen=True, slots=True)
class SupervisorConfig:
    owner_id: str
    runtime_dir: Path
    working_directory: Path
    log_path: Path
    start_mode: StartMode = StartMode.LAZY
    command: tuple[str, ...] = field(default_factory=lambda: (sys.executable, "-m", "networkclaw_harness.host"))
    environment: Mapping[str, str] = field(default_factory=dict)
    handshake_timeout_seconds: float = 5.0
    shutdown_timeout_seconds: float = 5.0
    terminate_timeout_seconds: float = 1.0
    restart: RestartPolicy = field(default_factory=RestartPolicy)

    def __post_init__(self) -> None:
        if not self.owner_id or not self.owner_id.isascii():
            raise ValueError("owner_id must be a non-empty ASCII string")
        if not self.working_directory.is_absolute() or not self.runtime_dir.is_absolute() or not self.log_path.is_absolute():
            raise ValueError("runtime, working, and log paths must be absolute")
        if not self.command:
            raise ValueError("command cannot be empty")


_ENV_ALLOWLIST = frozenset({"PATH", "LANG", "LC_ALL", "TZ", "PYTHONPATH", "PYTHONUNBUFFERED"})


class HarnessSupervisor:
    """Own exactly one Harness process and its private protocol pipes."""

    def __init__(self, config: SupervisorConfig, *, sleep: Callable[[float], None] = time.sleep,
                 monotonic: Callable[[], float] = time.monotonic) -> None:
        self.config = config
        self.state = SupervisorState.STOPPED
        self.agent_available = False
        self.last_heartbeat_at: float | None = None
        self.last_exit_code: int | None = None
        self.restart_count = 0
        self._process: subprocess.Popen[bytes] | None = None
        self._log: BinaryIO | None = None
        self._lock: BinaryIO | None = None
        self._restart_times: list[float] = []
        self._request_sequence = 0
        self._sleep = sleep
        self._monotonic = monotonic

    @property
    def pid(self) -> int | None:
        return self._process.pid if self._process and self._process.poll() is None else None

    def __enter__(self) -> "HarnessSupervisor":
        self.start_if_eager()
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.shutdown()

    def start_if_eager(self) -> None:
        if self.config.start_mode is StartMode.EAGER:
            self.start()

    def start(self) -> None:
        if self.pid is not None:
            return
        if self.state is SupervisorState.CIRCUIT_OPEN:
            raise LifecycleError("Harness restart circuit is open")
        self._acquire_owner_lock()
        self.state = SupervisorState.STARTING
        self.config.log_path.parent.mkdir(parents=True, exist_ok=True)
        self._log = self.config.log_path.open("ab", buffering=0)
        command = (*self.config.command, "--parent-pid", str(os.getpid()))
        try:
            self._process = subprocess.Popen(
                command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=self._log,
                cwd=self.config.working_directory, env=self._child_environment(),
                start_new_session=True, bufsize=0,
            )
            self._handshake()
        except Exception as error:
            self._reap_failed_start()
            self.state = SupervisorState.FAILED
            if isinstance(error, LifecycleError):
                raise
            raise LifecycleError(f"Harness startup failed: {error}") from error

    def ensure_started(self) -> None:
        if self.pid is None:
            self.start()

    def open_session(self, *, tenant_id: str, user_id: str, session_id: str,
                     workspace_root: Path, execution_epoch: int, lease: Mapping[str, Any],
                     resume: bool = False) -> list[dict[str, Any]]:
        self.ensure_started()
        return self.send({
            "protocol_version": "1.0", "type": "session.resume" if resume else "session.open",
            "request_id": self._next_request_id("session"), "tenant_id": tenant_id,
            "user_id": user_id, "session_id": session_id,
            "payload": {"workspace_root": str(workspace_root), "owner_id": self.config.owner_id,
                        "execution_epoch": execution_epoch, "lease": dict(lease)},
        })

    def send(self, frame: Mapping[str, Any], *, timeout: float | None = None) -> list[dict[str, Any]]:
        if self.state is SupervisorState.DRAINING and frame.get("type") == "user.input":
            raise LifecycleError("chatsvc is draining and cannot start a new turn")
        if frame.get("type") == "user.input" and not self.agent_available:
            raise LifecycleError("Harness health has not opened agent capability")
        process = self._require_process()
        assert process.stdin is not None
        try:
            process.stdin.write(json.dumps(frame, separators=(",", ":")).encode("ascii") + b"\n")
            process.stdin.flush()
        except (BrokenPipeError, OSError) as error:
            self._abort_child()
            raise LifecycleError("Harness stdin pipe is closed") from error
        return self._read_response(str(frame.get("request_id")), timeout or self.config.handshake_timeout_seconds)

    def probe(self) -> dict[str, Any]:
        events = self.send({"protocol_version": "1.0", "type": "health.query", "request_id": self._next_request_id("heartbeat")})
        status = next(event for event in events if event["type"] == "health.status")
        self.last_heartbeat_at = self._monotonic()
        self.agent_available = status["payload"].get("status") == "ready"
        self.state = SupervisorState.READY if self.agent_available else SupervisorState.DEGRADED
        return status

    def monitor(self) -> dict[str, Any]:
        return_code = self._process.poll() if self._process else None
        if return_code is not None:
            self._record_exit()
        return {
            "owner_id": self.config.owner_id, "pid": self.pid, "state": self.state.value,
            "stdin_open": bool(self._process and self._process.stdin and not self._process.stdin.closed),
            "stdout_open": bool(self._process and self._process.stdout and not self._process.stdout.closed),
            "last_heartbeat_at": self.last_heartbeat_at, "last_exit_code": self.last_exit_code,
            "restart_count": self.restart_count, "resources": self._resource_snapshot(),
        }

    def recover(self) -> None:
        if self.pid is not None:
            return
        now = self._monotonic()
        policy = self.config.restart
        self._restart_times = [value for value in self._restart_times if now - value <= policy.window_seconds]
        if len(self._restart_times) >= policy.max_restarts:
            self.state = SupervisorState.CIRCUIT_OPEN
            raise LifecycleError("Harness restart limit exceeded")
        delay = min(policy.base_backoff_seconds * (2 ** len(self._restart_times)), policy.max_backoff_seconds)
        self.state = SupervisorState.BACKOFF
        self._sleep(delay)
        self._restart_times.append(self._monotonic())
        self.restart_count += 1
        self.start()

    def drain(self) -> None:
        if self.pid is None:
            self.state = SupervisorState.STOPPED
            self._release_owner_lock()
            return
        self.state = SupervisorState.DRAINING

    def shutdown(self) -> None:
        process = self._process
        if process is None:
            self.state = SupervisorState.STOPPED
            self._release_owner_lock()
            return
        self.drain()
        try:
            self.send({"protocol_version": "1.0", "type": "shutdown", "request_id": self._next_request_id("shutdown")}, timeout=self.config.shutdown_timeout_seconds)
            process.wait(timeout=self.config.shutdown_timeout_seconds)
        except (LifecycleError, subprocess.TimeoutExpired):
            self._signal_process_group(signal.SIGTERM)
            try:
                process.wait(timeout=self.config.terminate_timeout_seconds)
            except subprocess.TimeoutExpired:
                self._signal_process_group(signal.SIGKILL)
                process.wait(timeout=self.config.terminate_timeout_seconds)
        finally:
            self._record_exit()
            self.state = SupervisorState.STOPPED
            self._release_owner_lock()

    def close_host_pipe(self) -> None:
        process = self._require_process()
        assert process.stdin is not None
        process.stdin.close()

    def _handshake(self) -> None:
        negotiated = self.send({"protocol_version": "1.0", "type": "protocol.negotiate", "request_id": self._next_request_id("protocol"), "payload": {"supported_protocol_versions": ["1.0"]}})
        if not any(event["type"] == "protocol.negotiated" for event in negotiated):
            raise LifecycleError("Harness protocol negotiation failed")
        capabilities = self.send({"protocol_version": "1.0", "type": "capabilities.query", "request_id": self._next_request_id("capabilities")})
        report = next((event for event in capabilities if event["type"] == "capabilities.report"), None)
        if report is None or "session.open" not in report["payload"].get("commands", []):
            raise LifecycleError("Harness capability handshake failed")
        self.probe()

    def _read_response(self, request_id: str, timeout: float) -> list[dict[str, Any]]:
        process = self._require_process()
        assert process.stdout is not None
        selector = selectors.DefaultSelector()
        selector.register(process.stdout, selectors.EVENT_READ)
        deadline = self._monotonic() + timeout
        events: list[dict[str, Any]] = []
        expected_sequence = 1
        try:
            while self._monotonic() < deadline:
                remaining = max(0.0, deadline - self._monotonic())
                if not selector.select(remaining):
                    break
                line = process.stdout.readline()
                if not line:
                    self._abort_child()
                    raise LifecycleError("Harness stdout pipe closed before terminal frame")
                try:
                    event = json.loads(line)
                except (UnicodeDecodeError, json.JSONDecodeError) as error:
                    self._abort_child()
                    raise LifecycleError("Harness emitted invalid JSONL") from error
                if (event.get("protocol_version") != "1.0" or
                        event.get("request_id") != request_id or
                        event.get("sequence") != expected_sequence):
                    self._abort_child()
                    raise LifecycleError("Harness response violated protocol correlation or sequence")
                events.append(event)
                expected_sequence += 1
                if event.get("end") is True and event.get("type") in {"end", "error"}:
                    if event["type"] == "error":
                        raise LifecycleError(f"Harness rejected request: {event.get('payload', {}).get('code')}")
                    return events
        finally:
            selector.close()
        self._abort_child()
        raise LifecycleError("Harness handshake or response timed out")

    def _child_environment(self) -> dict[str, str]:
        environment = {key: value for key, value in os.environ.items() if key in _ENV_ALLOWLIST}
        for key, value in self.config.environment.items():
            if key not in _ENV_ALLOWLIST and not key.startswith("NETWORKCLAW_"):
                raise LifecycleError(f"environment variable is not allowlisted: {key}")
            environment[key] = value
        environment["PYTHONUNBUFFERED"] = "1"
        return environment

    def _acquire_owner_lock(self) -> None:
        if self._lock is not None:
            return
        self.config.runtime_dir.mkdir(parents=True, exist_ok=True)
        digest = hashlib.sha256(self.config.owner_id.encode("ascii")).hexdigest()
        lock = (self.config.runtime_dir / f"harness-{digest}.lock").open("a+b")
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            lock.close()
            raise LifecycleError("this chatsvc owner already has a Harness") from error
        self._lock = lock

    def _release_owner_lock(self) -> None:
        if self._lock is not None:
            fcntl.flock(self._lock.fileno(), fcntl.LOCK_UN)
            self._lock.close()
            self._lock = None

    def _require_process(self) -> subprocess.Popen[bytes]:
        if self._process is None or self._process.poll() is not None:
            self._record_exit()
            raise LifecycleError("Harness process is not running")
        return self._process

    def _record_exit(self) -> None:
        if self._process is not None:
            self.last_exit_code = self._process.poll()
            for stream in (self._process.stdin, self._process.stdout):
                if stream and not stream.closed:
                    stream.close()
            self._process = None
        if self._log is not None:
            self._log.close()
            self._log = None
        self.agent_available = False
        if self.state not in {SupervisorState.DRAINING, SupervisorState.STOPPED}:
            self.state = SupervisorState.DEGRADED

    def _reap_failed_start(self) -> None:
        if self._process and self._process.poll() is None:
            self._signal_process_group(signal.SIGKILL)
            self._process.wait(timeout=1)
        self._record_exit()
        self._release_owner_lock()

    def _abort_child(self) -> None:
        if self._process and self._process.poll() is None:
            self._signal_process_group(signal.SIGTERM)
            try:
                self._process.wait(timeout=self.config.terminate_timeout_seconds)
            except subprocess.TimeoutExpired:
                self._signal_process_group(signal.SIGKILL)
                self._process.wait(timeout=self.config.terminate_timeout_seconds)
        self._record_exit()

    def _signal_process_group(self, requested_signal: signal.Signals) -> None:
        if self._process and self._process.poll() is None:
            try:
                os.killpg(self._process.pid, requested_signal)
            except ProcessLookupError:
                pass

    def _next_request_id(self, prefix: str) -> str:
        self._request_sequence += 1
        return f"supervisor-{prefix}-{self._request_sequence}"

    def _resource_snapshot(self) -> dict[str, int | None]:
        pid = self.pid
        if pid is None:
            return {"rss_bytes": None, "cpu_ticks": None}
        status_path = Path(f"/proc/{pid}/status")
        stat_path = Path(f"/proc/{pid}/stat")
        if not status_path.exists() or not stat_path.exists():
            return {"rss_bytes": None, "cpu_ticks": None}
        try:
            rss_kib = next((int(line.split()[1]) for line in status_path.read_text().splitlines() if line.startswith("VmRSS:")), 0)
            stat = stat_path.read_text().split()
            return {"rss_bytes": rss_kib * 1024, "cpu_ticks": int(stat[13]) + int(stat[14])}
        except (OSError, IndexError, ValueError):
            return {"rss_bytes": None, "cpu_ticks": None}
