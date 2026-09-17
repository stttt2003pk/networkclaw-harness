"""JSONL protocol envelopes shared by the host and tests."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Mapping

from .version import PROTOCOL_VERSION, SUPPORTED_PROTOCOL_VERSIONS


class ProtocolError(ValueError):
    """A malformed or incompatible input frame."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class InputFrame:
    type: str
    request_id: str
    protocol_version: str = PROTOCOL_VERSION
    session_id: str | None = None
    turn_id: str | None = None
    payload: Mapping[str, Any] = field(default_factory=dict)

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "InputFrame":
        version = value.get("protocol_version")
        if version not in SUPPORTED_PROTOCOL_VERSIONS:
            raise ProtocolError(
                "unsupported_protocol_version",
                f"supported versions: {', '.join(SUPPORTED_PROTOCOL_VERSIONS)}",
            )

        frame_type = _required_string(value, "type")
        request_id = _required_string(value, "request_id")
        payload = value.get("payload", {})
        if not isinstance(payload, Mapping):
            raise ProtocolError("invalid_payload", "payload must be a JSON object")

        return cls(
            type=frame_type,
            request_id=request_id,
            protocol_version=version,
            session_id=_optional_string(value, "session_id"),
            turn_id=_optional_string(value, "turn_id"),
            payload=payload,
        )


def event_frame(
    event_type: str,
    *,
    sequence: int,
    request_id: str | None,
    session_id: str | None = None,
    turn_id: str | None = None,
    payload: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build one host-to-chatsvc event envelope."""

    return {
        "protocol_version": PROTOCOL_VERSION,
        "type": event_type,
        "request_id": request_id,
        "session_id": session_id,
        "turn_id": turn_id,
        "sequence": sequence,
        "occurred_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "payload": dict(payload or {}),
    }


def _required_string(value: Mapping[str, Any], key: str) -> str:
    result = value.get(key)
    if not isinstance(result, str) or not result.strip():
        raise ProtocolError("invalid_frame", f"{key} must be a non-empty string")
    return result


def _optional_string(value: Mapping[str, Any], key: str) -> str | None:
    result = value.get(key)
    if result is None:
        return None
    if not isinstance(result, str) or not result.strip():
        raise ProtocolError("invalid_frame", f"{key} must be a non-empty string when present")
    return result

