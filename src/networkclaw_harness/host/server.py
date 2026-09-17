"""Initial stdin/stdout JSONL host boundary."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TextIO

from networkclaw_harness import __version__
from networkclaw_harness.protocol import InputFrame, ProtocolError, event_frame
from networkclaw_harness.workspace import SessionWorkspace, WorkspaceError

LOGGER = logging.getLogger(__name__)


@dataclass(slots=True)
class OpenSession:
    workspace: SessionWorkspace
    execution_epoch: str


class JsonlHost:
    """Process protocol frames sequentially and emit monotonic events."""

    def __init__(self, input_stream: TextIO, output_stream: TextIO) -> None:
        self._input = input_stream
        self._output = output_stream
        self._sequence = 0
        self._sessions: dict[str, OpenSession] = {}
        self._stopping = False

    def serve(self) -> int:
        for line in self._input:
            if self._stopping:
                break
            if not line.strip():
                continue
            self._handle_line(line)
        return 0

    def _handle_line(self, line: str) -> None:
        try:
            raw = json.loads(line)
            if not isinstance(raw, dict):
                raise ProtocolError("invalid_frame", "input frame must be a JSON object")
            frame = InputFrame.from_mapping(raw)
            self._dispatch(frame)
        except json.JSONDecodeError as error:
            self._emit_error(None, "invalid_json", str(error))
        except ProtocolError as error:
            request_id = raw.get("request_id") if isinstance(raw, dict) else None
            self._emit_error(request_id, error.code, str(error))
        except (KeyError, WorkspaceError) as error:
            request_id = raw.get("request_id") if isinstance(raw, dict) else None
            self._emit_error(request_id, "invalid_request", str(error))

    def _dispatch(self, frame: InputFrame) -> None:
        handlers = {
            "health.query": self._health,
            "capabilities.query": self._capabilities,
            "session.open": self._open_session,
            "session.resume": self._open_session,
            "session.close": self._close_session,
            "user.input": self._user_input,
            "shutdown": self._shutdown,
        }
        handler = handlers.get(frame.type)
        if handler is None:
            raise ProtocolError("unsupported_command", f"unsupported command: {frame.type}")
        handler(frame)

    def _health(self, frame: InputFrame) -> None:
        self._accepted(frame)
        self._emit(
            "health.status",
            frame,
            {
                "status": "degraded",
                "harness_version": __version__,
                "hermes_runtime": "not_initialized",
            },
        )

    def _capabilities(self, frame: InputFrame) -> None:
        self._accepted(frame)
        self._emit(
            "capabilities.report",
            frame,
            {
                "commands": [
                    "health.query",
                    "capabilities.query",
                    "session.open",
                    "session.resume",
                    "session.close",
                    "user.input",
                    "shutdown",
                ],
                "hermes_runtime_ready": False,
                "self_evolution": False,
            },
        )

    def _open_session(self, frame: InputFrame) -> None:
        session_id = self._require_session_id(frame)
        workspace_root = frame.payload.get("workspace_root")
        execution_epoch = frame.payload.get("execution_epoch")
        if not isinstance(workspace_root, str) or not workspace_root:
            raise ProtocolError("invalid_request", "payload.workspace_root is required")
        if not isinstance(execution_epoch, str) or not execution_epoch:
            raise ProtocolError("invalid_request", "payload.execution_epoch is required")

        workspace = SessionWorkspace.open(Path(workspace_root))
        self._sessions[session_id] = OpenSession(workspace, execution_epoch)
        self._accepted(frame)
        self._emit(
            "session.opened",
            frame,
            {"workspace_root": str(workspace.root), "execution_epoch": execution_epoch},
        )

    def _close_session(self, frame: InputFrame) -> None:
        session_id = self._require_session_id(frame)
        self._sessions.pop(session_id, None)
        self._accepted(frame)
        self._emit("session.closed", frame, {})

    def _user_input(self, frame: InputFrame) -> None:
        session_id = self._require_session_id(frame)
        if session_id not in self._sessions:
            raise ProtocolError("session_not_open", "session must be opened before user.input")
        self._accepted(frame)
        self._emit(
            "turn.failed",
            frame,
            {
                "code": "runtime_unavailable",
                "message": "Hermes runtime snapshot and adapter have not passed H0 validation",
                "retryable": False,
            },
        )

    def _shutdown(self, frame: InputFrame) -> None:
        self._accepted(frame)
        self._emit("shutdown.completed", frame, {})
        self._stopping = True

    def _accepted(self, frame: InputFrame) -> None:
        self._emit("request.accepted", frame, {"command": frame.type})

    def _emit(self, event_type: str, frame: InputFrame, payload: dict[str, Any]) -> None:
        self._sequence += 1
        self._write(
            event_frame(
                event_type,
                sequence=self._sequence,
                request_id=frame.request_id,
                session_id=frame.session_id,
                turn_id=frame.turn_id,
                payload=payload,
            )
        )

    def _emit_error(self, request_id: str | None, code: str, message: str) -> None:
        self._sequence += 1
        self._write(
            event_frame(
                "error",
                sequence=self._sequence,
                request_id=request_id,
                payload={"code": code, "message": message},
            )
        )

    def _write(self, frame: dict[str, Any]) -> None:
        self._output.write(json.dumps(frame, ensure_ascii=False, separators=(",", ":")) + "\n")
        self._output.flush()

    @staticmethod
    def _require_session_id(frame: InputFrame) -> str:
        if frame.session_id is None:
            raise ProtocolError("invalid_request", "session_id is required")
        return frame.session_id

