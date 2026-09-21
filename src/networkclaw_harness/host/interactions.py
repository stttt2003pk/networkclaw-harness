"""Host Protocol adapter for the durable interaction control runtime."""

from __future__ import annotations

from typing import Any, Mapping, Sequence

from networkclaw_harness.protocol import InputFrame, ProtocolError
from networkclaw_harness.runtime import InteractionController, InteractionError


class InteractionHostAdapter:
    def __init__(self, controller: InteractionController) -> None:
        self._controller = controller
        self._active_runs: dict[str, str] = {}

    def bind_run(self, *, tenant_id: str, user_id: str, session_id: str,
                 run_id: str, version: int = 1,
                 runnable_items: Sequence[str] = ()):
        cancellation = self._controller.register_run(
            tenant_id=tenant_id, user_id=user_id, session_id=session_id,
            run_id=run_id, version=version, runnable_items=runnable_items,
        )
        self._active_runs[session_id] = run_id
        return cancellation

    def attach_run(self, *, session_id: str, run_id: str) -> None:
        self._controller.waiting(session_id=session_id, run_id=run_id)
        self._active_runs[session_id] = run_id

    def handle(self, frame: InputFrame) -> Sequence[tuple[str, Mapping[str, Any]]]:
        assert frame.session_id is not None
        active_run = self._active_runs.get(frame.session_id)
        if frame.type == "user.input":
            try:
                kind = self._controller.classify(frame.type, active_run=active_run is not None)
                expected = _positive_int(frame.payload, "run_version") if active_run is not None else None
                result = self._controller.record_user_input(
                    session_id=frame.session_id,
                    interaction_id=frame.interaction_id or frame.request_id,
                    item_id=frame.turn_id or frame.request_id,
                    text=_nonempty_string(frame.payload, "text"),
                    run_id=active_run, expected_run_version=expected,
                )
            except InteractionError as error:
                raise ProtocolError(error.code, str(error)) from error
            return (("turn.queued", {
                "input_kind": kind, "priority": "normal",
                "run_id": active_run, "run_version": result.run_version,
                "interaction_id": result.interaction_id,
                "deduplicated": result.deduplicated, "queue_position": 1,
            }),)
        run_id = frame.run_id or active_run
        if run_id is None:
            raise ProtocolError("invalid_identity", "run_id is required when no active run is bound")
        try:
            expected = _positive_int(frame.payload, "run_version")
            if frame.type == "turn.steer":
                result = self._controller.steer(
                    session_id=frame.session_id, run_id=run_id,
                    interaction_id=frame.interaction_id or frame.request_id,
                    expected_run_version=expected,
                    text=_nonempty_string(frame.payload, "text"),
                )
                return (("turn.controlled", _result_payload(frame.type, result)),)
            if frame.type == "turn.cancel":
                result = self._controller.request_cancel(
                    session_id=frame.session_id, run_id=run_id,
                    interaction_id=frame.interaction_id or frame.request_id,
                    expected_run_version=expected,
                )
                return (("turn.controlled", _result_payload(frame.type, result)),)
            interaction_id = frame.interaction_id
            assert interaction_id is not None
            interaction_version = _positive_int(frame.payload, "interaction_version")
            resolution_id = str(frame.payload.get("resolution_id") or frame.request_id)
            if frame.type == "clarification.answer":
                result = self._controller.answer_clarification(
                    session_id=frame.session_id, run_id=run_id,
                    interaction_id=interaction_id,
                    interaction_version=interaction_version,
                    expected_run_version=expected, resolution_id=resolution_id,
                    answer=_nonempty_string(frame.payload, "answer"),
                )
                return (("clarification.resolved", _result_payload(frame.type, result)),)
            if frame.type == "approval.resolve":
                result = self._controller.resolve_approval(
                    session_id=frame.session_id, run_id=run_id,
                    interaction_id=interaction_id,
                    interaction_version=interaction_version,
                    expected_run_version=expected, resolution_id=resolution_id,
                    decision=_nonempty_string(frame.payload, "decision"),
                )
                return (("approval.resolved", _result_payload(frame.type, result)),)
        except InteractionError as error:
            raise ProtocolError(error.code, str(error)) from error
        except ValueError as error:
            raise ProtocolError("invalid_request", str(error)) from error
        raise ProtocolError("unsupported_command", f"unsupported runtime command: {frame.type}")

    def host_disconnected(self, session_ids: Sequence[str]) -> None:
        for session_id in session_ids:
            run_id = self._active_runs.get(session_id)
            if run_id is None:
                continue
            try:
                view = self._controller.waiting(session_id=session_id, run_id=run_id)
                self._controller.handle_disconnect(
                    session_id=session_id, run_id=run_id, source="host",
                    interaction_id=f"host-disconnect-v{view.run_version}",
                    expected_run_version=view.run_version,
                )
            except InteractionError:
                continue


def _positive_int(payload: Mapping[str, Any], name: str) -> int:
    value = payload.get(name)
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise ProtocolError("invalid_request", f"payload.{name} must be a positive integer")
    return value


def _nonempty_string(payload: Mapping[str, Any], name: str) -> str:
    value = payload.get(name)
    if not isinstance(value, str) or not value.strip():
        raise ProtocolError("invalid_request", f"payload.{name} must be a non-empty string")
    return value


def _result_payload(command: str, result) -> dict[str, Any]:
    return {
        "command": command, "code": result.code,
        "run_version": result.run_version, "state": result.run_status,
        "interaction_version": result.interaction_version,
        "deduplicated": result.deduplicated,
        "resumed_items": list(result.resumed_items),
        "invocation_states": dict(result.invocation_states),
        "priority": "control",
    }
