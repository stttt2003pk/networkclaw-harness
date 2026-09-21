"""JSONL protocol envelopes shared by the host and protocol simulators."""

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


_OPTIONAL_STRING_FIELDS = (
    "tenant_id", "user_id", "session_id", "turn_id", "trace_id", "deadline",
    "run_id", "event_id", "invocation_id", "parent_item_id", "interaction_id",
    "artifact_id", "cursor",
)
_INPUT_FIELDS = frozenset({"protocol_version", "type", "request_id", "metadata", "payload", *_OPTIONAL_STRING_FIELDS})


@dataclass(frozen=True, slots=True)
class InputFrame:
    type: str
    request_id: str
    protocol_version: str = PROTOCOL_VERSION
    tenant_id: str | None = None
    user_id: str | None = None
    session_id: str | None = None
    turn_id: str | None = None
    trace_id: str | None = None
    deadline: str | None = None
    run_id: str | None = None
    event_id: str | None = None
    invocation_id: str | None = None
    parent_item_id: str | None = None
    interaction_id: str | None = None
    artifact_id: str | None = None
    cursor: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)
    payload: Mapping[str, Any] = field(default_factory=dict)

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "InputFrame":
        unknown = sorted(set(value) - _INPUT_FIELDS)
        if unknown:
            raise ProtocolError("invalid_frame", f"unknown input fields: {', '.join(unknown)}")
        version = value.get("protocol_version")
        if version not in SUPPORTED_PROTOCOL_VERSIONS:
            raise ProtocolError("unsupported_protocol_version", f"supported versions: {', '.join(SUPPORTED_PROTOCOL_VERSIONS)}")
        optional = {name: _optional_string(value, name) for name in _OPTIONAL_STRING_FIELDS}
        return cls(
            type=_required_string(value, "type"), request_id=_required_string(value, "request_id"),
            protocol_version=version, metadata=_mapping(value, "metadata"),
            payload=_mapping(value, "payload"), **optional,
        )

    def canonical_mapping(self) -> dict[str, Any]:
        """Complete command body used for request-id conflict detection."""
        result: dict[str, Any] = {
            "protocol_version": self.protocol_version, "type": self.type,
            "request_id": self.request_id, "metadata": dict(self.metadata),
            "payload": dict(self.payload),
        }
        result.update({name: getattr(self, name) for name in _OPTIONAL_STRING_FIELDS if getattr(self, name) is not None})
        return result


def event_frame(event_type: str, *, sequence: int, request_id: str | None,
                tenant_id: str | None = None, user_id: str | None = None,
                session_id: str | None = None, turn_id: str | None = None,
                trace_id: str | None = None, run_id: str | None = None,
                event_id: str | None = None, invocation_id: str | None = None,
                parent_item_id: str | None = None, interaction_id: str | None = None,
                artifact_id: str | None = None, cursor: str | None = None,
                metadata: Mapping[str, Any] | None = None,
                payload: Mapping[str, Any] | None = None, end: bool = False) -> dict[str, Any]:
    """Build one host-to-chatsvc event envelope."""
    frame: dict[str, Any] = {
        "protocol_version": PROTOCOL_VERSION, "type": event_type, "request_id": request_id,
        "sequence": sequence,
        "occurred_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "payload": dict(payload or {}), "end": end,
    }
    optional = {
        "tenant_id": tenant_id, "user_id": user_id, "session_id": session_id,
        "turn_id": turn_id, "trace_id": trace_id, "run_id": run_id, "event_id": event_id,
        "invocation_id": invocation_id, "parent_item_id": parent_item_id,
        "interaction_id": interaction_id, "artifact_id": artifact_id, "cursor": cursor,
    }
    frame.update({key: value for key, value in optional.items() if value is not None})
    if metadata:
        frame["metadata"] = dict(metadata)
    return frame


def _required_string(value: Mapping[str, Any], key: str) -> str:
    result = value.get(key)
    if not isinstance(result, str) or not result.strip() or not result.isascii():
        raise ProtocolError("invalid_frame", f"{key} must be a non-empty ASCII string")
    return result


def _optional_string(value: Mapping[str, Any], key: str) -> str | None:
    result = value.get(key)
    if result is None:
        return None
    if not isinstance(result, str) or not result.strip() or not result.isascii():
        raise ProtocolError("invalid_frame", f"{key} must be a non-empty ASCII string when present")
    return result


def _mapping(value: Mapping[str, Any], key: str) -> Mapping[str, Any]:
    result = value.get(key, {})
    if not isinstance(result, Mapping):
        raise ProtocolError("invalid_payload", f"{key} must be a JSON object")
    return result
