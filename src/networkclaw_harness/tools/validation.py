"""Bounded validation of provider-originated tool calls.

The validator is deliberately independent of a provider SDK.  It normalizes the
small amount of untrusted structure that may cross the provider boundary and
returns synthetic tool errors for calls that must not reach an executor.
"""
from __future__ import annotations

import difflib
import json
from dataclasses import dataclass
from typing import Any, Mapping, Sequence


@dataclass(frozen=True, slots=True)
class ValidatedToolCall:
    call_id: str
    tool_name: str
    arguments: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class ToolCallError:
    call_id: str
    tool_name: str
    message: str
    retry: bool = False


@dataclass(frozen=True, slots=True)
class ToolValidationResult:
    valid: tuple[ValidatedToolCall, ...] = ()
    errors: tuple[ToolCallError, ...] = ()
    retry: bool = False
    partial: bool = False
    strikes: int = 0


class ToolValidationState:
    """Per-turn retry counters; state is public and bounded for durable replay."""

    def __init__(self, *, max_strikes: int = 3, max_json_retries: int = 2) -> None:
        self.max_strikes = max_strikes
        self.max_json_retries = max_json_retries
        self.invalid_name_strikes = 0
        self.invalid_json_retries = 0

    def reset_success(self) -> None:
        self.invalid_name_strikes = 0
        self.invalid_json_retries = 0

    def public_dict(self) -> dict[str, int]:
        return {"invalid_name_strikes": self.invalid_name_strikes,
                "invalid_json_retries": self.invalid_json_retries}


def _stable_id(value: Any, index: int, seen: set[str]) -> str:
    base = str(value or f"provider-call-{index}").strip()[:128] or f"provider-call-{index}"
    candidate = base
    suffix = 2
    while candidate in seen:
        candidate = f"{base}-{suffix}"
        suffix += 1
    seen.add(candidate)
    return candidate


def _repair_name(name: str, valid: Sequence[str]) -> str | None:
    normalized = name.strip()
    if normalized in valid:
        return normalized
    # Repair only an unambiguous close spelling. Never invent a new capability.
    matches = difflib.get_close_matches(normalized, list(valid), n=2, cutoff=0.86)
    return matches[0] if len(matches) == 1 else None


def validate_tool_calls(calls: Sequence[Mapping[str, Any]], valid_names: Sequence[str],
                        state: ToolValidationState | None = None) -> ToolValidationResult:
    state = state or ToolValidationState()
    valid_set = tuple(str(item) for item in valid_names)
    normalized: list[ValidatedToolCall] = []
    errors: list[ToolCallError] = []
    seen: set[str] = set()
    unknown = False
    for index, raw in enumerate(calls):
        function = raw.get("function") if isinstance(raw, Mapping) else None
        function = function if isinstance(function, Mapping) else {}
        call_id = _stable_id(raw.get("id") if isinstance(raw, Mapping) else None, index, seen)
        original_name = str(function.get("name") or "")
        name = _repair_name(original_name, valid_set)
        if name is None:
            unknown = True
            errors.append(ToolCallError(call_id, original_name[:80],
                                        f"Unknown tool '{original_name[:80]}'."))
            continue
        arguments = function.get("arguments")
        if isinstance(arguments, Mapping):
            parsed = dict(arguments)
        elif arguments is None or not str(arguments).strip():
            parsed = {}
        else:
            try:
                parsed = json.loads(str(arguments))
            except json.JSONDecodeError as exc:
                text = str(arguments).rstrip()
                truncated = not text.endswith(("}", "]"))
                if truncated:
                    return ToolValidationResult(errors=(ToolCallError(call_id, name, "Truncated JSON arguments; call was not executed."),), partial=True, strikes=state.invalid_name_strikes)
                errors.append(ToolCallError(call_id, name, f"Invalid JSON arguments: {exc.msg}.", retry=True))
                continue
        if not isinstance(parsed, dict):
            errors.append(ToolCallError(call_id, name, "Tool arguments must be a JSON object.", retry=True))
            continue
        normalized.append(ValidatedToolCall(call_id, name, parsed))
    if unknown and not normalized:
        state.invalid_name_strikes += 1
        if state.invalid_name_strikes >= state.max_strikes:
            return ToolValidationResult(errors=tuple(errors), partial=True, strikes=state.invalid_name_strikes)
    elif normalized:
        state.invalid_name_strikes = 0
    json_errors = tuple(item for item in errors if item.retry)
    if json_errors and not normalized:
        state.invalid_json_retries += 1
        if state.invalid_json_retries <= state.max_json_retries:
            return ToolValidationResult(errors=json_errors, retry=True, strikes=state.invalid_name_strikes)
        state.invalid_json_retries = 0
        return ToolValidationResult(errors=tuple(ToolCallError(item.call_id, item.tool_name, item.message) for item in errors), retry=False, strikes=state.invalid_name_strikes)
    if not json_errors:
        state.invalid_json_retries = 0
    return ToolValidationResult(tuple(normalized), tuple(errors), strikes=state.invalid_name_strikes)


def error_result(error: ToolCallError) -> dict[str, str]:
    """Provider-safe synthetic result; never includes raw arguments."""
    return {"status": "error", "error": error.message, "tool_call_id": error.call_id}

