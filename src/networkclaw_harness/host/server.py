"""The bounded, versioned stdin/stdout JSONL Host Protocol v1 endpoint."""

from __future__ import annotations

import json
import hashlib
import logging
import os
import threading
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping, Protocol, Sequence, TextIO

from networkclaw_harness import __version__
from networkclaw_harness.observability import (
    HARNESS_METRICS, DiagnosticService, MetricsRegistry, OperationTimer,
)
from networkclaw_harness.protocol import InputFrame, ProtocolError, event_frame
from networkclaw_harness.protocol.catalog import (
    ALL_EVENTS, COMMANDS, INCOMPATIBLE_PROTOCOL_EXIT, MAX_FRAME_BYTES,
    MAX_PENDING_FRAMES, PRESENTATION_PREFIX, PROCESS_COMMANDS, WRITE_TIMEOUT_MS,
)
from networkclaw_harness.protocol.version import SUPPORTED_PROTOCOL_VERSIONS
from networkclaw_harness.workspace import SessionWorkspace, WorkspaceError
from .scheduler import AdmissionScheduler, ScheduledCommand

LOGGER = logging.getLogger(__name__)
MAX_INPUT_LINE_BYTES = 1024 * 1024


@dataclass(slots=True)
class OpenSession:
    workspace: SessionWorkspace
    tenant_id: str
    user_id: str
    owner_id: str
    execution_epoch: int
    lease: Mapping[str, Any]
    execution_state: str = "active"
    host_grant: Mapping[str, Any] | None = None
    agent_id: str = ""
    profile_id: str = ""


@dataclass(slots=True)
class RequestRecord:
    request_hash: str
    frames: tuple[dict[str, Any], ...]


class HostRuntimePort(Protocol):
    def open(self, *, session_id: str, tenant_id: str, user_id: str,
             workspace: SessionWorkspace,
             lease: Mapping[str, Any],
             host_grant: Mapping[str, Any] | None = None) -> None: ...
    def close(self, session_id: str) -> None: ...
    def handle(self, frame: InputFrame) -> Sequence[tuple[str, Mapping[str, Any]]]: ...
    def host_disconnected(self, session_ids: Sequence[str]) -> None: ...
    def fence(self, session_id: str, reason: str = "lease_lost") -> None: ...


class JsonlHost:
    """Drive the frozen v1 protocol without exposing runtime internals on stdout."""

    def __init__(self, input_stream: TextIO, output_stream: TextIO,
                 runtime: HostRuntimePort | None = None,
                 metrics: MetricsRegistry | None = None) -> None:
        self._input = input_stream
        self._output = output_stream
        self._sessions: dict[str, OpenSession] = {}
        self._requests: dict[str, RequestRecord] = {}
        self._stopping = False
        self._output_closed = False
        self._exit_code = 0
        self._scheduler = AdmissionScheduler()
        self._scheduler_lock = threading.RLock()
        self._write_lock = threading.RLock()
        self._request_lock = threading.RLock()
        self._active_requests: dict[str, str] = {}
        self._turn_workers: dict[str, threading.Thread] = {}
        self._worker_lock = threading.RLock()
        self._runtime = runtime
        self._metrics = metrics or MetricsRegistry(allowed_names=HARNESS_METRICS)
        self._diagnostics = DiagnosticService(self._metrics)
        self._metrics.gauge("readiness", 0)
        self._metrics.gauge("active_sessions", 0)
        self._metrics.gauge("queue_depth", 0)

    @property
    def stopping(self) -> bool:
        """Whether the protocol requested process shutdown or hit a fatal output error."""
        return self._stopping

    def serve(self) -> int:
        for line in self._input:
            if self._stopping or self._output_closed:
                break
            if not line.strip():
                continue
            if len(line.encode("utf-8")) > MAX_INPUT_LINE_BYTES:
                self._emit_error(None, "resource_exhausted", "input frame exceeds maximum size")
                continue
            self._handle_line(line)
            if self._stopping or self._output_closed:
                break
        if not self._stopping and self._sessions:
            # A finite fixture/input stream may close immediately after enqueueing its
            # final turn. Give admitted work a short bounded drain before treating EOF
            # as a host failure; a genuinely wedged turn is still fenced below.
            self._wait_for_workers(timeout=1.0)
            if self._runtime is not None:
                self._runtime.host_disconnected(tuple(self._sessions))
            for session in self._sessions.values():
                session.execution_state = "fenced"
            LOGGER.error("host input pipe closed; fenced %d sessions", len(self._sessions))
            return 75
        return self._exit_code

    def _handle_line(self, line: str) -> None:
        raw: Any = None
        frame: InputFrame | None = None
        canonical: str | None = None
        try:
            raw = json.loads(line)
            if not isinstance(raw, dict):
                raise ProtocolError("invalid_frame", "input frame must be a JSON object")
            frame = InputFrame.from_mapping(raw)
            canonical = hashlib.sha256(json.dumps(
                frame.canonical_mapping(), sort_keys=True, separators=(",", ":"),
            ).encode("utf-8")).hexdigest()
            with self._request_lock:
                known = self._requests.get(frame.request_id)
                active_canonical = self._active_requests.get(frame.request_id)
            if known is not None:
                if known.request_hash != canonical:
                    self._emit_error(frame.request_id, "request_id_conflict", "request_id was already used with a different command", frame)
                else:
                    self._write_many(known.frames)
                return
            if active_canonical is not None:
                if active_canonical != canonical:
                    self._emit_error(frame.request_id, "request_id_conflict", "request_id was already used with a different command", frame)
                else:
                    self._emit_error(frame.request_id, "request_in_progress", "request is still running", frame)
                return
            if frame.type == "user.input" and getattr(self._runtime, "handle_stream", None) is not None:
                self._start_streaming(frame, canonical)
                return
            frames = self._dispatch(frame)
            with self._request_lock:
                self._requests[frame.request_id] = RequestRecord(canonical, tuple(frames))
            self._write_many(frames)
        except json.JSONDecodeError:
            self._emit_error(None, "invalid_json", "input is not a complete JSON object")
        except ProtocolError as error:
            self._metrics.inc("protocol_errors_total")
            request_id = raw.get("request_id") if isinstance(raw, dict) and isinstance(raw.get("request_id"), str) else None
            error_frame = self._error_frame(request_id, error.code, str(error), frame)
            if frame is not None and canonical is not None:
                with self._request_lock:
                    self._requests[frame.request_id] = RequestRecord(canonical, (error_frame,))
            self._write_many((error_frame,))
        except (WorkspaceError, OSError) as error:
            request_id = raw.get("request_id") if isinstance(raw, dict) and isinstance(raw.get("request_id"), str) else None
            LOGGER.warning("host request rejected: %s", error)
            self._emit_error(request_id, "invalid_request", "assigned workspace could not be opened")

    def _dispatch(self, frame: InputFrame) -> list[dict[str, Any]]:
        timer = OperationTimer(self._metrics, "protocol_latency_ms")
        try:
            return self._dispatch_timed(frame)
        finally:
            timer.finish()

    def _dispatch_timed(self, frame: InputFrame) -> list[dict[str, Any]]:
        if frame.type not in COMMANDS:
            if frame.type.startswith(PRESENTATION_PREFIX):
                return self._response(frame, [("warning", {"code": "presentation_degraded", "message": "presentation extension is not supported"})])
            code = "unknown_control" if frame.type.startswith(("control.", "authorization.")) else "unsupported_command"
            raise ProtocolError(code, f"unsupported command: {frame.type}")
        self._validate_identity(frame)
        handlers = {
            "protocol.negotiate": self._negotiate,
            "health.query": self._health,
            "capabilities.query": self._capabilities,
            "metrics.query": self._metrics_query,
            "session.open": self._open_session,
            "session.resume": self._open_session,
            "session.close": self._close_session,
            "session.drain": self._drain_session,
            "session.lease.update": self._lease_update,
            "user.input": self._user_input,
            "turn.steer": self._control,
            "turn.cancel": self._control,
            "run.query": self._run_query,
            "delegation.resolve": self._control,
            "clarification.answer": self._control,
            "approval.resolve": self._control,
            "shutdown": self._shutdown,
        }
        return handlers[frame.type](frame)

    def _dispatch_streaming(self, frame: InputFrame) -> list[dict[str, Any]]:
        """Write provider deltas as they arrive while retaining replayable frames."""
        self._validate_identity(frame)
        self._require_open_session(frame)
        text = frame.payload.get("text")
        if not isinstance(text, str) or not text.strip():
            raise ProtocolError("invalid_request", "payload.text must be a non-empty string")
        assert frame.session_id is not None and self._runtime is not None
        with self._scheduler_lock:
            self._scheduler.submit(ScheduledCommand(frame.session_id, frame.request_id, frame))
            admitted = self._scheduler.next()
        self._metrics.inc("turns_started_total")
        assert admitted is not None and admitted.request_id == frame.request_id
        frames = [self._frame("request.accepted", frame, {"command": frame.type}, 1)]
        self._write_many(frames)

        def emit(event_type: str, payload: Mapping[str, Any]) -> None:
            if len(frames) >= MAX_PENDING_FRAMES:
                raise ProtocolError("backpressure", "stream response buffer is full")
            self._record_event_metrics(event_type, payload)
            event = self._frame(event_type, frame, payload, len(frames) + 1)
            frames.append(event)
            self._write_many((event,))

        events = self._runtime.handle_stream(frame, emit)  # type: ignore[attr-defined]
        for event_type, payload in events:
            if event_type in {"turn.started", "plan.updated"}:
                continue
            emit(event_type, payload)
        if not any(item["type"] in {"turn.completed", "turn.failed", "turn.cancelled"} for item in frames):
            end = self._frame("end", frame, {"status": "completed"}, len(frames) + 1, end=True)
            frames.append(end)
            self._write_many((end,))
        return frames

    def _start_streaming(self, frame: InputFrame, canonical: str) -> None:
        self._validate_identity(frame)
        self._require_open_session(frame)
        assert frame.session_id is not None
        with self._worker_lock:
            active = self._turn_workers.get(frame.session_id)
            if active is not None and active.is_alive():
                frames = self._error_response(frame, "turn_already_active", "session already has an active turn")
                with self._request_lock:
                    self._requests[frame.request_id] = RequestRecord(canonical, tuple(frames))
                self._write_many(frames)
                return
            with self._request_lock:
                self._active_requests[frame.request_id] = canonical
            worker = threading.Thread(
                target=self._run_streaming_request,
                args=(frame, canonical),
                name=f"harness-turn-{frame.session_id}",
                daemon=True,
            )
            self._turn_workers[frame.session_id] = worker
            worker.start()

    def _run_streaming_request(self, frame: InputFrame, canonical: str) -> None:
        try:
            frames = self._dispatch_streaming(frame)
        except ProtocolError as error:
            frames = [self._error_frame(frame.request_id, error.code, str(error), frame)]
            self._write_many(frames)
        except Exception as error:
            LOGGER.exception("streaming turn failed: %s", type(error).__name__)
            frames = [self._error_frame(
                frame.request_id, "runtime_unavailable", f"runtime turn failed: {type(error).__name__}", frame,
            )]
            self._write_many(frames)
        finally:
            with self._worker_lock:
                if frame.session_id is not None and self._turn_workers.get(frame.session_id) is threading.current_thread():
                    self._turn_workers.pop(frame.session_id, None)
        with self._request_lock:
            self._active_requests.pop(frame.request_id, None)
            self._requests[frame.request_id] = RequestRecord(canonical, tuple(frames))

    def _validate_identity(self, frame: InputFrame) -> None:
        identity = ("tenant_id", "user_id", "session_id", "turn_id", "run_id", "interaction_id", "invocation_id", "artifact_id", "cursor")
        if frame.type in PROCESS_COMMANDS:
            carried = [name for name in identity if getattr(frame, name) is not None]
            if carried:
                raise ProtocolError("invalid_identity", f"process command cannot carry {', '.join(carried)}")
            return
        for name in ("tenant_id", "user_id", "session_id"):
            if getattr(frame, name) is None:
                raise ProtocolError("invalid_identity", f"{name} is required for session commands")
        if frame.type in {"user.input", "turn.steer", "turn.cancel"} and frame.turn_id is None:
            raise ProtocolError("invalid_identity", "turn_id is required for turn commands")
        if frame.type in {"clarification.answer", "approval.resolve", "delegation.resolve"} and frame.interaction_id is None:
            raise ProtocolError("invalid_identity", "interaction_id is required for interaction commands")

    def _negotiate(self, frame: InputFrame) -> list[dict[str, Any]]:
        offered = frame.payload.get("supported_protocol_versions")
        if not isinstance(offered, list) or not all(isinstance(item, str) for item in offered):
            raise ProtocolError("invalid_request", "payload.supported_protocol_versions must be an array of strings")
        shared = [version for version in SUPPORTED_PROTOCOL_VERSIONS if version in offered]
        if not shared:
            self._stopping = True
            self._exit_code = INCOMPATIBLE_PROTOCOL_EXIT
            return self._error_response(frame, "unsupported_protocol_version", "no compatible protocol version")
        return self._response(frame, [("protocol.negotiated", {"protocol_version": shared[0]})])

    def _health(self, frame: InputFrame) -> list[dict[str, Any]]:
        self._metrics.inc("health_queries_total")
        live = os.environ.get("NETWORKCLAW_HARNESS_PROVIDER_MODE", "").strip().lower() == "live"
        snapshot = self._diagnostics.snapshot(health="ready" if live else "degraded", readiness=live)
        return self._response(frame, [("health.status", {
            "status": "ready" if live else "degraded", "harness_version": __version__,
            "hermes_runtime": "native_adapter", "diagnostics": snapshot,
        })])

    def _capabilities(self, frame: InputFrame) -> list[dict[str, Any]]:
        return self._response(frame, [("capabilities.report", {
            "commands": sorted(COMMANDS), "events": sorted(ALL_EVENTS),
            "supported_protocol_versions": list(SUPPORTED_PROTOCOL_VERSIONS),
            "hermes_runtime_ready": True, "self_evolution": False,
            "limits": {"max_frame_bytes": MAX_FRAME_BYTES, "max_pending_frames": MAX_PENDING_FRAMES, "write_timeout_ms": WRITE_TIMEOUT_MS},
        })])

    def _metrics_query(self, frame: InputFrame) -> list[dict[str, Any]]:
        return self._response(frame, [("metrics.snapshot", {
            "format": "prometheus_text",
            "prometheus": self._metrics.prometheus(),
        })])

    def _open_session(self, frame: InputFrame) -> list[dict[str, Any]]:
        workspace_root = frame.payload.get("workspace_root")
        owner_id = frame.payload.get("owner_id")
        epoch = frame.payload.get("execution_epoch")
        lease = frame.payload.get("lease")
        if not isinstance(workspace_root, str) or not workspace_root:
            raise ProtocolError("invalid_request", "payload.workspace_root is required")
        if not isinstance(owner_id, str) or not owner_id.isascii() or not owner_id:
            raise ProtocolError("invalid_request", "payload.owner_id is required")
        if not isinstance(epoch, int) or isinstance(epoch, bool) or epoch < 1:
            raise ProtocolError("invalid_request", "payload.execution_epoch must be a positive integer")
        self._validate_lease(lease)
        host_grant = self._validate_host_grant(frame.payload, frame.session_id, epoch, workspace_root)
        agent_id = frame.payload.get("agent_id", "")
        profile_id = frame.payload.get("profile_id", "")
        if any(not isinstance(value, str) or len(value) > 256 for value in (agent_id, profile_id)):
            raise ProtocolError("invalid_request", "agent_id and profile_id must be bounded strings")
        assert frame.session_id is not None and frame.tenant_id is not None and frame.user_id is not None
        if lease["session_id"] != frame.session_id or lease["owner_id"] != owner_id or lease["execution_epoch"] != epoch:
            raise ProtocolError("invalid_request", "payload.lease identity must match session, owner, and execution epoch")
        existing = self._sessions.get(frame.session_id)
        if existing and (existing.tenant_id != frame.tenant_id or existing.user_id != frame.user_id):
            raise ProtocolError("session_identity_mismatch", "session is already associated with another tenant or user")
        if existing is not None:
            if existing.workspace.root != Path(workspace_root).resolve():
                raise ProtocolError("session_identity_conflict", "session workspace does not match its existing binding")
            if epoch < existing.execution_epoch:
                raise ProtocolError("stale_epoch", "session open carries an older execution epoch")
            if epoch == existing.execution_epoch:
                if owner_id != existing.owner_id or lease["lease_id"] != existing.lease["lease_id"]:
                    raise ProtocolError("session_owned", "session is already owned by another execution lease")
                if existing.execution_state != "active":
                    raise ProtocolError("session_fenced", "session is not active")
                if lease != existing.lease:
                    raise ProtocolError("lease_update_required", "renew the lease using session.lease.update")
                if "agent_id" not in frame.payload:
                    agent_id = existing.agent_id
                if "profile_id" not in frame.payload:
                    profile_id = existing.profile_id
                if "host_grant" not in frame.payload:
                    host_grant = existing.host_grant
                self._configure_session(frame.session_id, agent_id, profile_id, host_grant)
                existing.agent_id = agent_id
                existing.profile_id = profile_id
                existing.host_grant = host_grant
                return self._response(frame, [("session.opened", {
                    "workspace_root": str(existing.workspace.root),
                    "owner_id": existing.owner_id,
                    "execution_epoch": existing.execution_epoch,
                })])
            # A strictly newer epoch is an explicit platform takeover. Fence and
            # release the old runtime before admitting the replacement binding.
            fencer = getattr(self._runtime, "fence", None) if self._runtime is not None else None
            if fencer is not None:
                fencer(frame.session_id, "execution_epoch_takeover")
            closer = getattr(self._runtime, "close", None) if self._runtime is not None else None
            if closer is not None:
                closer(frame.session_id)
            self._sessions.pop(frame.session_id, None)
        workspace = SessionWorkspace.open(Path(workspace_root), tenant_id=frame.tenant_id, session_id=frame.session_id)
        opened = OpenSession(workspace, frame.tenant_id, frame.user_id, owner_id, epoch, lease,
                             host_grant=host_grant, agent_id=agent_id, profile_id=profile_id)
        opener = getattr(self._runtime, "open", None) if self._runtime is not None else None
        if opener is not None:
            try:
                opener(session_id=frame.session_id, tenant_id=frame.tenant_id, user_id=frame.user_id,
                       workspace=workspace, lease=lease, host_grant=host_grant)
                self._configure_session(frame.session_id, agent_id, profile_id, host_grant)
            except Exception as error:
                LOGGER.error("runtime session open failed: %s", type(error).__name__)
                raise ProtocolError("runtime_open_failed", "runtime could not open the assigned session") from error
        self._sessions[frame.session_id] = opened
        self._metrics.gauge("active_sessions", len(self._sessions))
        return self._response(frame, [("session.opened", {"workspace_root": str(workspace.root), "owner_id": owner_id, "execution_epoch": epoch})])

    def _configure_session(self, session_id: str, agent_id: str, profile_id: str,
                           host_grant: Mapping[str, Any] | None) -> None:
        configure = getattr(self._runtime, "configure_session", None)
        if configure is not None:
            try:
                configure(session_id, agent_id=agent_id, profile_id=profile_id, host_grant=host_grant)
            except Exception as error:
                code = getattr(error, "code", "runtime_configuration_failed")
                raise ProtocolError(code, "runtime could not update the session configuration") from error

    def _close_session(self, frame: InputFrame) -> list[dict[str, Any]]:
        session = self._require_session_identity(frame)
        assert frame.session_id is not None
        # Close is the final lifecycle edge.  Fence/interrupt the native runtime
        # first, then give the corresponding streaming worker a bounded window to
        # publish its terminal outcome before the session binding is removed.  This
        # keeps ``drain -> close`` meaningful: close cannot silently orphan a late
        # native callback while still allowing a wedged provider to fail closed.
        session.execution_state = "draining"
        self._fence_runtime(frame.session_id, "session_closed")
        self._wait_for_session_worker(frame.session_id, timeout=10.0)
        self._sessions.pop(frame.session_id, None)
        closer = getattr(self._runtime, "close", None) if self._runtime is not None else None
        if closer is not None:
            closer(frame.session_id)
        self._metrics.gauge("active_sessions", len(self._sessions))
        return self._response(frame, [("session.closed", {})])

    def _drain_session(self, frame: InputFrame) -> list[dict[str, Any]]:
        session = self._require_session_identity(frame)
        assert frame.session_id is not None
        if session.execution_state == "active":
            session.execution_state = "draining"
        return self._response(frame, [("session.draining", {
            "execution_epoch": session.execution_epoch,
            "active_turn": self._has_worker(frame.session_id),
        })])

    def _lease_update(self, frame: InputFrame) -> list[dict[str, Any]]:
        session = self._require_open_session(frame)
        operation = frame.payload.get("operation", "renew")
        if not isinstance(operation, str) or operation not in {"renew", "replace", "revoke", "takeover"}:
            raise ProtocolError("lease_operation_invalid", "payload.operation must be renew, replace, revoke, or takeover")
        if operation == "revoke":
            reason_code = frame.payload.get("reason_code", "platform_lease_lost")
            if not isinstance(reason_code, str) or reason_code not in {
                "platform_lease_lost", "epoch_takeover", "session_closed", "harness_shutdown",
            }:
                raise ProtocolError("lease_operation_invalid", "revoke reason_code is not supported")
            session.execution_state = "fenced"
            self._fence_runtime(frame.session_id, reason_code)
            return self._response(frame, [("session.lease.revoked", {
                "reason_code": reason_code, "lease_id": session.lease["lease_id"],
                "lease_version": session.lease["lease_version"],
            })])
        if frame.payload.get("renewal_succeeded") is False:
            session.execution_state = "fenced"
            self._fence_runtime(frame.session_id, "platform_lease_lost")
            raise ProtocolError("lease_lost", "lease renewal failed; session is fenced")
        lease = frame.payload.get("lease")
        self._validate_lease(lease)
        assert frame.session_id is not None
        current = session.lease
        if operation in {"replace", "takeover"}:
            if frame.payload.get("replacement_admitted") is not True:
                raise ProtocolError("lease_replacement_not_admitted", "replacement requires explicit admission")
            if lease["execution_epoch"] <= current["execution_epoch"]:
                raise ProtocolError("stale_epoch", "replacement epoch must be newer than the current epoch")
            reason_code = "epoch_takeover" if operation == "takeover" else "platform_lease_lost"
            session.execution_state = "fenced"
            self._fence_runtime(frame.session_id, reason_code)
            session.owner_id = lease["owner_id"]
            session.execution_epoch = lease["execution_epoch"]
            session.lease = lease
            session.execution_state = "active"
            updater = getattr(self._runtime, "update_lease", None) if self._runtime is not None else None
            if updater is not None:
                updater(frame.session_id, lease)
            event_type = "session.lease.takeover" if operation == "takeover" else "session.lease.replaced"
            return self._response(frame, [(event_type, {
                "owner_id": lease["owner_id"], "execution_epoch": lease["execution_epoch"],
                "lease_id": lease["lease_id"], "lease_version": lease["lease_version"],
                "reason_code": reason_code,
            })])
        identity = ("session_id", "owner_id", "execution_epoch", "lease_id")
        if any(lease[name] != current[name] for name in identity):
            session.execution_state = "fenced"
            self._fence_runtime(frame.session_id, "platform_lease_lost")
            raise ProtocolError("lease_lost", "lease update changed session ownership identity")
        policy = ("ttl_ms", "renew_interval_ms", "grace_ms")
        if any(lease[name] != current[name] for name in policy):
            session.execution_state = "fenced"
            self._fence_runtime(frame.session_id, "platform_lease_lost")
            raise ProtocolError("lease_lost", "lease policy changed during renewal")
        if lease["lease_version"] != current["lease_version"] + 1:
            session.execution_state = "fenced"
            self._fence_runtime(frame.session_id, "platform_lease_lost")
            raise ProtocolError("lease_lost", "lease renewal version did not increase by exactly one")
        current_expires = datetime.fromisoformat(current["expires_at"].replace("Z", "+00:00"))
        renewed_expires = datetime.fromisoformat(lease["expires_at"].replace("Z", "+00:00"))
        if renewed_expires <= current_expires:
            session.execution_state = "fenced"
            self._fence_runtime(frame.session_id, "platform_lease_lost")
            raise ProtocolError("lease_lost", "lease renewal did not extend the expiry window")
        session.lease = lease
        updater = getattr(self._runtime, "update_lease", None) if self._runtime is not None else None
        if updater is not None:
            updater(frame.session_id, lease)
        return self._response(frame, [("session.lease.updated", {"lease_id": lease["lease_id"], "lease_version": lease["lease_version"]})])

    def _fence_runtime(self, session_id: str | None, reason_code: str) -> None:
        fencer = getattr(self._runtime, "fence", None) if self._runtime is not None else None
        if fencer is not None and session_id is not None:
            fencer(session_id, reason_code)

    def _user_input(self, frame: InputFrame) -> list[dict[str, Any]]:
        self._require_open_session(frame)
        text = frame.payload.get("text")
        if not isinstance(text, str) or not text.strip():
            raise ProtocolError("invalid_request", "payload.text must be a non-empty string")
        assert frame.session_id is not None
        position = self._scheduler.submit(ScheduledCommand(frame.session_id, frame.request_id, frame))
        self._metrics.inc("turns_started_total")
        self._metrics.gauge("queue_depth", self._scheduler.depth)
        admitted = self._scheduler.next()
        self._metrics.gauge("queue_depth", self._scheduler.depth)
        assert admitted is not None and admitted.request_id == frame.request_id
        if self._runtime is not None:
            stream_handler = getattr(self._runtime, "handle_stream", None)
            if stream_handler is not None:
                streamed: list[tuple[str, Mapping[str, Any]]] = []
                events = stream_handler(frame, lambda event_type, payload: streamed.append((event_type, payload)))
                return self._response(frame, [*streamed, *events])
            return self._response(frame, list(self._runtime.handle(frame)))
        return self._response(frame, [
            ("turn.queued", {"reason": "runtime_not_ready", "queue_position": position, "priority": "normal"}),
            ("turn.failed", {"code": "runtime_unavailable", "reason_code": "runtime_unavailable", "message": "Hermes runtime snapshot and adapter have not passed H0 validation", "retryable": False}),
        ])

    def _control(self, frame: InputFrame) -> list[dict[str, Any]]:
        session = self._require_control_session(frame)
        assert frame.session_id is not None
        expected_epoch = frame.payload.get("execution_epoch")
        if expected_epoch is not None and expected_epoch != session.execution_epoch:
            raise ProtocolError("stale_epoch", "control frame execution_epoch is stale")
        if frame.type in {"turn.cancel", "turn.steer"}:
            target_turn_id = frame.payload.get("target_turn_id")
            if target_turn_id is not None and target_turn_id != frame.turn_id:
                raise ProtocolError("stale_request", "control target_turn_id does not match frame turn_id")
        with self._scheduler_lock:
            self._scheduler.submit(ScheduledCommand(frame.session_id, frame.request_id, frame), control=True)
            admitted = self._scheduler.next()
        assert admitted is not None and admitted.request_id == frame.request_id
        if self._runtime is not None:
            return self._response(frame, list(self._runtime.handle(frame)))
        return self._response(frame, [("turn.controlled", {"command": frame.type, "state": "runtime_unavailable", "priority": "control"})])

    def _run_query(self, frame: InputFrame) -> list[dict[str, Any]]:
        session = self._require_control_session(frame)
        assert frame.session_id is not None
        status = "running" if self._has_worker(frame.session_id) else "idle"
        return self._response(frame, [("run.snapshot", {
            "session_id": frame.session_id, "run_id": frame.run_id or "",
            "status": status, "execution_epoch": session.execution_epoch,
        })])

    def _shutdown(self, frame: InputFrame) -> list[dict[str, Any]]:
        # Shutdown is an explicit lifecycle fence. Active native turns must
        # receive the stable shutdown reason before workers are drained.
        for session_id, session in tuple(self._sessions.items()):
            if self._has_worker(session_id):
                session.execution_state = "fenced"
                self._fence_runtime(session_id, "harness_shutdown")
        self._wait_for_workers(timeout=10.0)
        self._stopping = True
        return self._response(frame, [("shutdown.completed", {})])

    def _wait_for_workers(self, *, timeout: float) -> None:
        deadline = datetime.now().timestamp() + timeout
        while True:
            with self._worker_lock:
                workers = tuple(self._turn_workers.values())
            if not workers:
                return
            remaining = deadline - datetime.now().timestamp()
            if remaining <= 0:
                return
            for worker in workers:
                worker.join(timeout=min(remaining, 0.1))

    def _wait_for_session_worker(self, session_id: str, *, timeout: float) -> None:
        """Wait only for one session's stream worker, without blocking other sessions."""
        deadline = datetime.now().timestamp() + timeout
        while True:
            with self._worker_lock:
                worker = self._turn_workers.get(session_id)
            if worker is None or not worker.is_alive():
                return
            remaining = deadline - datetime.now().timestamp()
            if remaining <= 0:
                LOGGER.warning("session close timed out waiting for active turn: %s", session_id)
                return
            worker.join(timeout=min(remaining, 0.1))

    def _has_workers(self) -> bool:
        with self._worker_lock:
            return any(worker.is_alive() for worker in self._turn_workers.values())

    def _has_worker(self, session_id: str | None) -> bool:
        if session_id is None:
            return False
        with self._worker_lock:
            worker = self._turn_workers.get(session_id)
            return worker is not None and worker.is_alive()

    def _require_open_session(self, frame: InputFrame) -> OpenSession:
        session = self._require_session_identity(frame)
        if session.execution_state != "active":
            raise ProtocolError("lease_lost", "session is fenced and cannot accept new actions")
        return session

    def _require_control_session(self, frame: InputFrame) -> OpenSession:
        session = self._require_session_identity(frame)
        if session.execution_state == "fenced":
            raise ProtocolError("lease_lost", "session is fenced and cannot accept control")
        return session

    def _require_session_identity(self, frame: InputFrame) -> OpenSession:
        assert frame.session_id is not None and frame.tenant_id is not None and frame.user_id is not None
        session = self._sessions.get(frame.session_id)
        if session is None:
            raise ProtocolError("session_not_open", "session must be opened before this command")
        if session.tenant_id != frame.tenant_id or session.user_id != frame.user_id:
            raise ProtocolError("session_identity_mismatch", "session tenant or user does not match")
        return session

    @staticmethod
    def _validate_lease(lease: Any) -> None:
        if not isinstance(lease, Mapping):
            raise ProtocolError("invalid_request", "payload.lease must be an object")
        required_strings = ("session_id", "owner_id", "lease_id", "issued_at", "expires_at", "renew_by", "grace_expires_at")
        if any(not isinstance(lease.get(name), str) or not lease[name] for name in required_strings):
            raise ProtocolError("invalid_request", "payload.lease is missing a required field")
        for name in ("lease_version", "execution_epoch", "ttl_ms", "renew_interval_ms"):
            if not isinstance(lease.get(name), int) or isinstance(lease[name], bool) or lease[name] < 1:
                raise ProtocolError("invalid_request", f"payload.lease.{name} must be a positive integer")
        if not isinstance(lease.get("grace_ms"), int) or isinstance(lease["grace_ms"], bool) or lease["grace_ms"] < 0:
            raise ProtocolError("invalid_request", "payload.lease.grace_ms must be a non-negative integer")
        if lease["renew_interval_ms"] >= lease["ttl_ms"] or lease["grace_ms"] >= lease["ttl_ms"]:
            raise ProtocolError("invalid_request", "lease renewal and grace intervals must be less than ttl_ms")
        try:
            issued_at, renew_by, expires_at, grace_expires_at = (
                datetime.fromisoformat(lease[name].replace("Z", "+00:00"))
                for name in ("issued_at", "renew_by", "expires_at", "grace_expires_at")
            )
        except ValueError as error:
            raise ProtocolError("invalid_request", "lease timestamps must be ISO-8601 values") from error
        if not issued_at < renew_by < expires_at <= grace_expires_at:
            raise ProtocolError("invalid_request", "lease timestamps are not ordered")
        if any(value.tzinfo is None for value in (issued_at, renew_by, expires_at, grace_expires_at)):
            raise ProtocolError("invalid_request", "lease timestamps must include a timezone")
        # datetime.total_seconds() is a float; flooring it can turn an exact
        # millisecond policy into 59,999ms when RFC3339 carries nanoseconds.
        milliseconds = lambda delta: round(delta.total_seconds() * 1000)
        if (milliseconds(renew_by - issued_at) != lease["renew_interval_ms"] or
                milliseconds(expires_at - issued_at) != lease["ttl_ms"] or
                milliseconds(grace_expires_at - expires_at) != lease["grace_ms"]):
            raise ProtocolError("invalid_request", "lease timestamps do not match its immutable policy")

    @staticmethod
    def _validate_host_grant(payload: Mapping[str, Any], session_id: str,
                             execution_epoch: int, workspace_root: str) -> Mapping[str, Any] | None:
        """Validate platform-owned resource grants without scheduling or placing sessions."""
        grant = payload.get("host_grant")
        if grant is None:
            return None
        if not isinstance(grant, Mapping):
            raise ProtocolError("invalid_request", "payload.host_grant must be an object")
        if grant.get("session_id") != session_id or grant.get("execution_epoch") != execution_epoch:
            raise ProtocolError("invalid_request", "host_grant identity does not match session")
        if grant.get("workspace") not in {workspace_root, str(Path(workspace_root).resolve())}:
            raise ProtocolError("invalid_request", "host_grant workspace does not match assigned workspace")
        budget = grant.get("turn_budget")
        if not isinstance(budget, Mapping):
            raise ProtocolError("invalid_request", "host_grant.turn_budget is required")
        for field in ("max_steps", "max_retries", "max_actions", "timeout_seconds"):
            value = budget.get(field)
            if not isinstance(value, int) or isinstance(value, bool) or value <= 0 or value > 1_000_000:
                raise ProtocolError("invalid_request", f"host_grant.turn_budget.{field} is invalid")
        profile = grant.get("resource_profile")
        if not isinstance(profile, (str, Mapping)):
            raise ProtocolError("invalid_request", "host_grant.resource_profile is invalid")
        allowed = grant.get("allowed_tools")
        if not isinstance(allowed, list) or len(allowed) > 256 or any(
                not isinstance(item, str) or not item or not item.isascii() or len(item) > 128
                for item in allowed):
            raise ProtocolError("invalid_request", "host_grant.allowed_tools is invalid")
        event_capabilities = grant.get("event_capabilities", ())
        if not isinstance(event_capabilities, (list, tuple)) or len(event_capabilities) > 64 or any(
                not isinstance(item, str) or not item or not item.isascii() or len(item) > 128
                for item in event_capabilities):
            raise ProtocolError("invalid_request", "host_grant.event_capabilities is invalid")
        return {
            "session_id": session_id, "execution_epoch": execution_epoch,
            "workspace": str(Path(workspace_root).resolve()),
            "turn_budget": dict(budget), "resource_profile": profile,
            "allowed_tools": tuple(allowed),
            "event_capabilities": tuple(event_capabilities),
        }

    def _response(self, frame: InputFrame, events: list[tuple[str, Mapping[str, Any]]]) -> list[dict[str, Any]]:
        for event_type, payload in events:
            self._record_event_metrics(event_type, payload)
        result = [self._frame("request.accepted", frame, {"command": frame.type}, 1)]
        result.extend(self._frame(event_type, frame, payload, index + 2) for index, (event_type, payload) in enumerate(events))
        if not any(item["type"] in {"turn.completed", "turn.failed", "turn.cancelled"} for item in result):
            result.append(self._frame("end", frame, {"status": "completed"}, len(result) + 1, end=True))
        return result

    def _record_event_metrics(self, event_type: str, payload: Mapping[str, Any]) -> None:
        if event_type in {"turn.completed", "turn.failed"}:
            self._metrics.inc("turns_terminal_total")
        elif event_type == "tool.started":
            self._metrics.inc("tools_started_total")
        elif event_type == "tool.completed":
            self._metrics.inc("tools_terminal_total")
        metadata = payload.get("provider_metadata")
        if isinstance(metadata, Mapping):
            self._metrics.inc("provider_requests_total")
            latency = metadata.get("latency_ms")
            if isinstance(latency, (int, float)) and not isinstance(latency, bool) and latency >= 0:
                self._metrics.observe("provider_latency_ms", float(latency))
            usage = metadata.get("usage")
            if isinstance(usage, Mapping):
                for field, metric in (("prompt_tokens", "provider_input_tokens_total"), ("completion_tokens", "provider_output_tokens_total")):
                    value = usage.get(field)
                    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                        self._metrics.inc(metric, value)

    def _error_response(self, frame: InputFrame, code: str, message: str) -> list[dict[str, Any]]:
        self._metrics.inc("protocol_errors_total")
        return [self._frame("error", frame, {"code": code, "message": message}, 1, end=True)]

    def _emit_error(self, request_id: str | None, code: str, message: str, source: InputFrame | None = None) -> None:
        self._metrics.inc("protocol_errors_total")
        self._write_many((self._error_frame(request_id, code, message, source),))

    def diagnostic_snapshot(self) -> Mapping[str, Any]:
        return self._diagnostics.snapshot(
            health="stopping" if self._stopping else "degraded", readiness=False,
        )

    @staticmethod
    def _error_frame(request_id: str | None, code: str, message: str, source: InputFrame | None = None) -> dict[str, Any]:
        return event_frame(
            "error", sequence=1, request_id=request_id,
            tenant_id=source.tenant_id if source else None,
            user_id=source.user_id if source else None,
            session_id=source.session_id if source else None,
            turn_id=source.turn_id if source else None,
            trace_id=source.trace_id if source else None,
            interaction_id=source.interaction_id if source else None,
            payload={"code": code, "message": message}, end=True,
        )

    def _frame(self, event_type: str, source: InputFrame, payload: Mapping[str, Any], sequence: int, *, end: bool = False) -> dict[str, Any]:
        normalized = dict(payload)
        if event_type in {"turn.completed", "turn.failed", "turn.cancelled"}:
            outcome = {
                "turn.completed": "completed",
                "turn.failed": "failed",
                "turn.cancelled": "cancelled",
            }[event_type]
            normalized.setdefault("outcome", outcome)
            normalized.setdefault("end", True)
            if event_type == "turn.completed":
                normalized.setdefault("reason_code", "")
            elif event_type == "turn.cancelled":
                normalized.setdefault("reason_code", "user_cancel")
            else:
                normalized.setdefault("reason_code", normalized.get("code", "provider_interrupted"))
            session = self._sessions.get(source.session_id) if source.session_id else None
            if session is not None:
                normalized.setdefault("execution_epoch", session.execution_epoch)
            normalized.setdefault("generation", int(normalized.get("generation", 0) or 0))
        terminal = event_type in {"turn.completed", "turn.failed", "turn.cancelled"}
        return event_frame(event_type, sequence=sequence, request_id=source.request_id,
                           tenant_id=source.tenant_id, user_id=source.user_id,
                           session_id=source.session_id, turn_id=source.turn_id,
                           trace_id=source.trace_id, run_id=source.run_id,
                           event_id=None, invocation_id=source.invocation_id,
                           parent_item_id=source.parent_item_id, interaction_id=source.interaction_id,
                           artifact_id=source.artifact_id, cursor=source.cursor,
                           metadata=source.metadata, payload=normalized, end=end or terminal)

    def _write_many(self, frames: tuple[dict[str, Any], ...] | list[dict[str, Any]]) -> None:
        with self._write_lock:
            if self._output_closed:
                return
            if len(frames) > MAX_PENDING_FRAMES:
                self._output_closed = True
                return
            try:
                for frame in frames:
                    encoded = json.dumps(frame, ensure_ascii=True, separators=(",", ":"))
                    if len(encoded.encode("utf-8")) > MAX_FRAME_BYTES:
                        raise ValueError("protocol frame exceeds maximum size")
                    line = encoded + "\n"
                    written = self._output.write(line)
                    if written is not None and written != len(line):
                        raise BrokenPipeError("short protocol write")
                self._output.flush()
            except (BrokenPipeError, OSError, ValueError):
                LOGGER.error("host protocol output stream closed")
                self._output_closed = True
                self._stopping = True
                self._exit_code = 74
