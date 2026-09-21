"""Hermes transcript adapter boundary and message invariants."""

from __future__ import annotations

import copy
import hashlib
import json
import threading
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Mapping, Protocol, Sequence


class HistoryError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class TranscriptMessage:
    role: str
    content: str | None
    phase: str
    tool_calls: tuple[str, ...] = ()
    tool_call_id: str | None = None
    tool_name: str | None = None
    round_id: str | None = None
    segment_id: str | None = None
    invocation_id: str | None = None
    pending: bool = False

    def to_hermes(self) -> dict[str, Any]:
        value: dict[str, Any] = {"role": self.role, "content": self.content}
        if self.tool_calls:
            value["tool_calls"] = [
                {"id": call_id, "type": "function", "function": {"name": "networkclaw_tool", "arguments": "{}"}}
                for call_id in self.tool_calls
            ]
        if self.tool_call_id is not None:
            value["tool_call_id"] = self.tool_call_id
        if self.tool_name is not None:
            value["tool_name"] = self.tool_name
        for key, item in (("round_id", self.round_id), ("segment_id", self.segment_id),
                          ("invocation_id", self.invocation_id)):
            if item is not None:
                value[key] = item
        if self.pending:
            value["pending"] = True
        value["display_kind"] = self.phase
        return value


def repair_transcript_tail(messages: Sequence[TranscriptMessage]) -> tuple[TranscriptMessage, ...]:
    """Return a provider-replayable transcript without inventing tool success.

    Provider streams can end after an assistant tool proposal or after an empty
    assistant placeholder.  Those transient tails are not durable conversation
    turns: drop the incomplete tail, while retaining all completed turns.  A
    partial assistant stream is retained as a normal model step so already
    delivered text is never silently replaced.
    """
    repaired = list(messages)
    # A proposal remains marked pending on disk until its result rows arrive.
    # Clear that marker during reload once every call has a matching result.
    completed_calls: set[str] = set()
    for item in repaired:
        if item.role == "tool" and item.tool_call_id:
            completed_calls.add(item.tool_call_id)
    repaired = [
        replace_message(item, pending=False)
        if item.role == "assistant" and item.pending and set(item.tool_calls).issubset(completed_calls)
        else item
        for item in repaired
    ]
    # Final delivery is a singleton terminal row.  Crash/retry can duplicate it;
    # keep the first durable delivery and discard later synthetic duplicates.
    finals = [index for index, item in enumerate(repaired)
              if item.role == "assistant" and item.phase == "final_delivery"]
    if finals:
        repaired = repaired[:finals[0] + 1]
    while repaired:
        try:
            validate_transcript(repaired)
            return tuple(repaired)
        except HistoryError as error:
            if error.code == "missing_tool_result":
                # Remove the assistant proposal and its unmatched tool tail.
                pending: set[str] = set()
                cut = len(repaired)
                for index, message in enumerate(repaired):
                    if message.role == "assistant":
                        pending.update(message.tool_calls)
                    elif message.role == "tool" and message.tool_call_id in pending:
                        pending.remove(message.tool_call_id)
                    if pending:
                        cut = index
                        break
                repaired = repaired[:cut]
                continue
            if error.code == "invalid_history" and repaired[-1].role == "assistant":
                repaired.pop()
                continue
            raise
    return ()


class SessionHistoryPort(Protocol):
    def open(self, session_id: str) -> None: ...
    def resume(self, session_id: str) -> None: ...
    def append(self, session_id: str, messages: Sequence[TranscriptMessage]) -> None: ...
    def load(self, session_id: str) -> tuple[TranscriptMessage, ...]: ...
    def close(self, session_id: str, reason: str) -> None: ...
    def append_tool_proposal(self, session_id: str, message: TranscriptMessage) -> None: ...


class HermesSessionDB(Protocol):
    def create_session(self, session_id: str, source: str, **kwargs: Any) -> str: ...
    def reopen_session(self, session_id: str) -> None: ...
    def end_session(self, session_id: str, end_reason: str) -> None: ...
    def append_messages_batch(self, session_id: str, messages: list[dict[str, Any]], **kwargs: Any) -> int: ...
    def get_messages(self, session_id: str, **kwargs: Any) -> list[dict[str, Any]]: ...


class HermesSessionPersistence:
    """Narrow adapter around vendored Hermes SessionDB; no Hermes globals leak in."""

    def __init__(self, database: HermesSessionDB, *, source: str = "networkclaw-harness") -> None:
        self._database = database
        self._source = source

    def open(self, session_id: str) -> None:
        self._database.create_session(session_id, self._source)
        self._database.reopen_session(session_id)

    def resume(self, session_id: str) -> None:
        self._database.reopen_session(session_id)

    def append(self, session_id: str, messages: Sequence[TranscriptMessage]) -> None:
        current = tuple(_from_hermes(item) for item in self._database.get_messages(session_id))
        validate_transcript((*current, *messages))
        inserted = self._database.append_messages_batch(session_id, [message.to_hermes() for message in messages])
        if inserted != len(messages):
            raise HistoryError("history_write_incomplete", "Hermes did not persist the complete transcript batch")

    def append_tool_proposal(self, session_id: str, message: TranscriptMessage) -> None:
        if message.role != "assistant" or not message.tool_calls:
            raise HistoryError("invalid_tool_proposal", "tool proposal must be an assistant message with calls")
        # A pending assistant row is intentionally durable before any effect.  Hermes
        # reload accepts it as a tail which repair_transcript_tail can remove if the
        # process dies before tool results are appended.
        current = tuple(_from_hermes(item) for item in self._database.get_messages(session_id))
        if current and current[-1].pending:
            raise HistoryError("pending_tool_proposal", "a tool proposal is already pending")
        inserted = self._database.append_messages_batch(session_id, [message.to_hermes()])
        if inserted != 1:
            raise HistoryError("history_write_incomplete", "Hermes did not persist the tool proposal")

    def load(self, session_id: str) -> tuple[TranscriptMessage, ...]:
        result = tuple(_from_hermes(item) for item in self._database.get_messages(session_id))
        return repair_transcript_tail(result)

    def close(self, session_id: str, reason: str) -> None:
        self._database.end_session(session_id, reason)

    def shutdown(self) -> None:
        close = getattr(self._database, "close", None)
        if close is not None:
            close()


def open_workspace_hermes_history(workspace: Any) -> HermesSessionPersistence:
    """Open the pinned Hermes SessionDB inside the assigned session workspace."""
    from .hermes_bootstrap import open_hermes_runtime
    return HermesSessionPersistence(open_hermes_runtime(workspace).session_db)


class ReferenceSessionHistory:
    def __init__(self) -> None:
        self._messages: dict[str, list[TranscriptMessage]] = {}
        self._closed: set[str] = set()
        self._lock = threading.RLock()

    def open(self, session_id: str) -> None:
        with self._lock:
            self._messages.setdefault(session_id, [])
            self._closed.discard(session_id)

    def resume(self, session_id: str) -> None:
        self.open(session_id)

    def append(self, session_id: str, messages: Sequence[TranscriptMessage]) -> None:
        with self._lock:
            if session_id in self._closed:
                raise HistoryError("session_closed", "cannot append history to a closed session")
            combined = (*self._messages.get(session_id, ()), *messages)
            validate_transcript(combined)
            self._messages.setdefault(session_id, []).extend(copy.deepcopy(tuple(messages)))

    def append_tool_proposal(self, session_id: str, message: TranscriptMessage) -> None:
        if message.role != "assistant" or not message.tool_calls:
            raise HistoryError("invalid_tool_proposal", "tool proposal must be an assistant message with calls")
        with self._lock:
            if session_id in self._closed:
                raise HistoryError("session_closed", "cannot append history to a closed session")
            current = self._messages.setdefault(session_id, [])
            if current and current[-1].pending:
                raise HistoryError("pending_tool_proposal", "a tool proposal is already pending")
            current.append(replace_message(message, pending=True))

    def load(self, session_id: str) -> tuple[TranscriptMessage, ...]:
        with self._lock:
            return repair_transcript_tail(tuple(copy.deepcopy(self._messages.get(session_id, ()))))

    def close(self, session_id: str, reason: str) -> None:
        del reason
        with self._lock:
            self._closed.add(session_id)


def validate_transcript(messages: Sequence[TranscriptMessage]) -> None:
    pending: list[str] = []
    expected_role = "user"
    phases = {"user_interaction", "model_step", "tool_round", "final_delivery"}
    for index, message in enumerate(messages):
        if message.phase not in phases or message.role not in {"user", "assistant", "tool"}:
            raise HistoryError("invalid_history", f"message {index} has an invalid role or phase")
        if message.role == "tool":
            if message.phase != "tool_round" or not message.tool_call_id or message.tool_call_id not in pending:
                raise HistoryError("tool_result_mismatch", f"message {index} has no matching assistant tool call")
            pending.remove(message.tool_call_id)
            if not pending:
                expected_role = "assistant"
            continue
        if pending:
            raise HistoryError("missing_tool_result", "all assistant tool calls require a matching tool result")
        if message.role != expected_role:
            raise HistoryError("role_alternation", "user and assistant dialogue roles must alternate")
        if message.role == "user" and message.phase != "user_interaction":
            raise HistoryError("invalid_history", "user messages must be user interactions")
        if message.role == "assistant" and message.phase not in {"model_step", "final_delivery"}:
            raise HistoryError("invalid_history", "assistant messages must be model steps or final deliveries")
        if message.tool_call_id is not None or message.role != "assistant" and message.tool_calls:
            raise HistoryError("invalid_history", "tool call fields do not match the message role")
        if len(set(message.tool_calls)) != len(message.tool_calls):
            raise HistoryError("duplicate_tool_call", "tool call ids must be unique")
        pending.extend(message.tool_calls)
        if message.role == "user":
            expected_role = "assistant"
        elif not pending:
            expected_role = "user"
    if pending:
        raise HistoryError("missing_tool_result", "transcript ends before all tool calls have results")


def history_hash(messages: Sequence[TranscriptMessage]) -> str:
    payload = [asdict(message) for message in messages]
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _from_hermes(value: Mapping[str, Any]) -> TranscriptMessage:
    calls = value.get("tool_calls") or ()
    return TranscriptMessage(
        role=str(value.get("role", "")), content=value.get("content"),
        phase=str(value.get("display_kind") or ("tool_round" if value.get("role") == "tool" else "model_step")),
        tool_calls=tuple(str(item.get("id")) for item in calls if isinstance(item, Mapping) and item.get("id")),
        tool_call_id=value.get("tool_call_id"), tool_name=value.get("tool_name"),
        round_id=value.get("round_id"), segment_id=value.get("segment_id"),
        invocation_id=value.get("invocation_id"), pending=bool(value.get("pending", False)),
    )


def replace_message(message: TranscriptMessage, **changes: Any) -> TranscriptMessage:
    values = {
        "role": message.role, "content": message.content, "phase": message.phase,
        "tool_calls": message.tool_calls, "tool_call_id": message.tool_call_id,
        "tool_name": message.tool_name, "round_id": message.round_id,
        "segment_id": message.segment_id, "invocation_id": message.invocation_id,
        "pending": message.pending,
    }
    values.update(changes)
    return TranscriptMessage(**values)
