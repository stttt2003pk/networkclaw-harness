"""Bounded stream assembly and transport-neutral response recovery."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from enum import StrEnum
import json
import sys
import threading
from pathlib import Path
from types import MappingProxyType
from typing import Any, Callable, Iterable, Mapping, Sequence

from .provider import ProviderChunk


class StreamOutcome(StrEnum):
    COMPLETED = "completed"
    PARTIAL = "partial"
    EMPTY = "empty"
    REFUSAL = "refusal"
    CONTENT_FILTER = "content_filter"
    INVALID_SHAPE = "invalid_shape"
    REPETITION = "repetition"
    TRUNCATED = "truncated"
    SAFE_SENTINEL = "safe_sentinel"


class RecoveryAction(StrEnum):
    INITIAL = "initial"
    EMPTY_RETRY = "empty_retry"
    POST_TOOL_NUDGE = "post_tool_nudge"
    CONTINUE_TEXT = "continue_text"
    RETRY_TOOL_CALL = "retry_tool_call"
    FALLBACK = "fallback"


@dataclass(frozen=True, slots=True)
class PartialDeliveryReceipt:
    sequence: int
    delivered_text: str
    text_bytes: int


@dataclass(frozen=True, slots=True)
class AssembledStream:
    chunks: tuple[ProviderChunk, ...]
    text: str
    reasoning: str
    tool_calls: tuple[Mapping[str, Any], ...]
    usage: Mapping[str, int]
    finish_reason: str | None
    refusal: str
    last_sequence: int
    receipts: tuple[PartialDeliveryReceipt, ...]
    invalid_reason: str | None = None

    @property
    def empty(self) -> bool:
        return not (self.text.strip() or self.reasoning.strip() or self.tool_calls or self.refusal.strip())


class StreamAssemblyError(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class BoundedStreamAssembler:
    """Single-writer assembler with one monotonic sequence for every stream event."""

    def __init__(self, *, max_events: int = 4096, max_text_bytes: int = 1_048_576,
                 max_tool_bytes: int = 262_144, start_sequence: int = 0,
                 receipt_sink: Callable[[PartialDeliveryReceipt], None] | None = None) -> None:
        if min(max_events, max_text_bytes, max_tool_bytes) <= 0:
            raise ValueError("stream bounds must be positive")
        self._max_events, self._max_text_bytes, self._max_tool_bytes = max_events, max_text_bytes, max_tool_bytes
        if start_sequence < 0:
            raise ValueError("stream start sequence must be non-negative")
        self._receipt_sink = receipt_sink
        self._start_sequence = start_sequence
        self._owner: int | None = None
        self._chunks: list[ProviderChunk] = []
        self._text: list[str] = []
        self._reasoning: list[str] = []
        self._usage: dict[str, int] = {}
        self._calls: dict[int, dict[str, Any]] = {}
        self._receipts: list[PartialDeliveryReceipt] = []
        self._finish: str | None = None
        self._refusal = ""
        self._text_bytes = 0
        self._tool_bytes = 0
        self._invalid: str | None = None

    def accept(self, chunk: ProviderChunk) -> ProviderChunk:
        owner = threading.get_ident()
        if self._owner is None:
            self._owner = owner
        elif owner != self._owner:
            raise StreamAssemblyError("stream_multiple_writers")
        if len(self._chunks) >= self._max_events:
            raise StreamAssemblyError("stream_event_limit")
        expected = self._start_sequence + len(self._chunks) + 1
        if chunk.sequence is not None and chunk.sequence != expected:
            self._invalid = "stream_sequence_invalid"
            raise StreamAssemblyError(self._invalid)
        chunk = replace(chunk, sequence=expected)
        self._text_bytes += len(chunk.delta.encode()) + len(chunk.reasoning_delta.encode())
        if self._text_bytes > self._max_text_bytes:
            raise StreamAssemblyError("stream_text_limit")
        self._chunks.append(chunk)
        self._text.append(chunk.delta)
        self._reasoning.append(chunk.reasoning_delta)
        self._refusal += chunk.refusal
        if chunk.finish_reason:
            if self._finish is not None and self._finish != chunk.finish_reason:
                self._invalid = "stream_finish_conflict"
            self._finish = chunk.finish_reason
        for name, amount in chunk.usage.items():
            self._usage[name] = self._usage.get(name, 0) + amount
        if chunk.tool_call is not None:
            self._accept_tool_call(chunk.tool_call)
        if chunk.delta:
            receipt = PartialDeliveryReceipt(expected, chunk.delta, len(chunk.delta.encode()))
            self._receipts.append(receipt)
            if self._receipt_sink is not None:
                self._receipt_sink(receipt)
        return chunk

    def _accept_tool_call(self, raw: Mapping[str, Any]) -> None:
        index = raw.get("index", 0)
        if not isinstance(index, int) or isinstance(index, bool) or index < 0:
            self._invalid = "stream_tool_index_invalid"
            return
        call = self._calls.setdefault(index, {"id": "", "type": "function", "function": {"name": "", "arguments": ""}})
        if isinstance(raw.get("id"), str) and raw["id"]:
            # Providers repeat the stable id on every fragment; retain one canonical id.
            call["id"] = raw["id"]
        function = raw.get("function")
        if isinstance(function, Mapping):
            for key in ("name", "arguments"):
                value = function.get(key)
                if isinstance(value, str):
                    call["function"][key] += value
                    self._tool_bytes += len(value.encode())
        if self._tool_bytes > self._max_tool_bytes:
            raise StreamAssemblyError("stream_tool_limit")

    def finish(self) -> AssembledStream:
        calls = tuple(MappingProxyType(value) for _, value in sorted(self._calls.items()))
        invalid = self._invalid
        if self._finish not in {None, "length", "stop", "tool_calls", "content_filter", "refusal"}:
            invalid = invalid or "stream_finish_invalid"
        if self._finish != "length" and calls and any(
            not call["id"] or not call["function"]["name"] or not _valid_json(call["function"]["arguments"])
            for call in calls
        ):
            invalid = invalid or "stream_tool_call_incomplete"
        return AssembledStream(tuple(self._chunks), "".join(self._text), "".join(self._reasoning), calls,
                               MappingProxyType(dict(self._usage)), self._finish, self._refusal,
                               self._start_sequence + len(self._chunks), tuple(self._receipts), invalid)


@dataclass(frozen=True, slots=True)
class RecoveryDirective:
    action: RecoveryAction
    attempt: int
    prior_text: str = ""
    increase_output_tokens: bool = False


@dataclass(frozen=True, slots=True)
class RecoveryPolicy:
    max_empty_retries: int = 2
    max_continuations: int = 4
    max_tool_call_retries: int = 2
    max_total_attempts: int = 8
    max_total_usage_tokens: int = 1_000_000
    min_context_headroom: int = 512
    safe_sentinel: str = "The model did not produce a safe, deliverable response."

    def __post_init__(self) -> None:
        if self.max_empty_retries < 0 or self.max_continuations < 0 or self.max_tool_call_retries < 0:
            raise ValueError("recovery retry ceilings must be non-negative")
        if self.max_total_attempts <= 0 or self.max_total_usage_tokens <= 0 or self.min_context_headroom < 0:
            raise ValueError("recovery attempt, usage, and headroom limits are invalid")
        if not self.safe_sentinel.strip():
            raise ValueError("recovery safe sentinel cannot be empty")


@dataclass(frozen=True, slots=True)
class RecoveredResponse:
    outcome: StreamOutcome
    text: str
    tool_calls: tuple[Mapping[str, Any], ...] = ()
    usage: Mapping[str, int] = field(default_factory=dict)
    finish_reason: str | None = None
    delivery_sequence: int = 0
    attempts: int = 0
    partial_delivered: bool = False
    reason: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "usage", MappingProxyType(dict(self.usage)))


class StreamRecoveryRuntime:
    """Bounded empty/truncation ladder shared by provider adapters and delivery."""

    def __init__(self, policy: RecoveryPolicy = RecoveryPolicy()) -> None:
        self.policy = policy

    def run(self, request: Callable[[RecoveryDirective], Iterable[ProviderChunk]], *,
            housekeeping_text: str = "", after_tool: bool = False,
            context_window: int | None = None, prompt_tokens: int | None = None,
            fallback: Callable[[RecoveryDirective], Iterable[ProviderChunk]] | None = None,
            receipt_sink: Callable[[PartialDeliveryReceipt], None] | None = None) -> RecoveredResponse:
        parts: list[str] = []
        usage: dict[str, int] = {}
        delivered = False
        sequence = 0
        empty_retries = continuations = tool_retries = attempts = 0
        directive = RecoveryDirective(RecoveryAction.INITIAL, 1)
        current = request
        while attempts < self.policy.max_total_attempts:
            attempts += 1
            assembler = BoundedStreamAssembler(start_sequence=sequence, receipt_sink=receipt_sink)
            interrupted = False
            try:
                for chunk in current(directive):
                    accepted = assembler.accept(chunk)
                    delivered = delivered or bool(accepted.delta)
            except Exception:
                interrupted = True
            result = assembler.finish()
            sequence = result.last_sequence
            for name, amount in result.usage.items():
                usage[name] = usage.get(name, 0) + amount
            if sum(usage.values()) > self.policy.max_total_usage_tokens:
                return RecoveredResponse(StreamOutcome.SAFE_SENTINEL, self.policy.safe_sentinel, (), usage, None,
                                         sequence, attempts, delivered, "usage_cost_guard")
            if result.invalid_reason:
                return self._terminal(StreamOutcome.INVALID_SHAPE, parts, result.text, usage, sequence, attempts, delivered, result.invalid_reason)
            if result.refusal or result.finish_reason == "refusal":
                return self._terminal(StreamOutcome.REFUSAL, parts, result.text, usage, sequence, attempts, delivered, "provider_refusal")
            if result.finish_reason == "content_filter":
                return self._terminal(StreamOutcome.CONTENT_FILTER, parts, result.text, usage, sequence, attempts, delivered, "content_filter")
            if interrupted:
                if result.text:
                    parts.append(result.text)
                    return self._terminal(StreamOutcome.PARTIAL, parts, "", usage, sequence, attempts, True, "stream_interrupted")
                empty_retries += 1
            elif result.finish_reason == "length":
                fragment = result.text
                if _repetition_dominated("".join(parts) + fragment):
                    return self._terminal(StreamOutcome.REPETITION, [], "", usage, sequence, attempts, delivered, "repetition_dominated")
                if context_window and prompt_tokens and context_window - prompt_tokens < self.policy.min_context_headroom:
                    return self._terminal(StreamOutcome.TRUNCATED, parts, fragment, usage, sequence, attempts, delivered, "context_headroom_exhausted")
                if result.tool_calls:
                    if tool_retries >= self.policy.max_tool_call_retries:
                        return self._terminal(StreamOutcome.TRUNCATED, parts, "", usage, sequence, attempts, delivered, "tool_call_retry_ceiling")
                    tool_retries += 1
                    directive = RecoveryDirective(RecoveryAction.RETRY_TOOL_CALL, attempts + 1, increase_output_tokens=True)
                    continue
                if not fragment.strip() and result.reasoning.strip():
                    return self._terminal(StreamOutcome.TRUNCATED, parts, "", usage, sequence, attempts, delivered, "reasoning_only")
                if continuations >= self.policy.max_continuations:
                    return self._terminal(StreamOutcome.TRUNCATED, [], "", usage, sequence, attempts, delivered, "continuation_ceiling_rollback")
                parts.append(fragment)
                continuations += 1
                directive = RecoveryDirective(RecoveryAction.CONTINUE_TEXT, attempts + 1, "".join(parts))
                continue
            elif result.text.strip() or result.tool_calls:
                parts.append(result.text)
                text = "".join(parts)
                if _repetition_dominated(text):
                    return self._terminal(StreamOutcome.REPETITION, [], "", usage, sequence, attempts, delivered, "repetition_dominated")
                return RecoveredResponse(StreamOutcome.COMPLETED, text, result.tool_calls, usage,
                                         result.finish_reason, sequence, attempts, delivered)
            else:
                empty_retries += 1

            if housekeeping_text.strip():
                return RecoveredResponse(StreamOutcome.COMPLETED, housekeeping_text.strip(), (), usage, "stop",
                                         sequence, attempts, delivered, "housekeeping_reuse")
            if empty_retries <= self.policy.max_empty_retries:
                action = RecoveryAction.POST_TOOL_NUDGE if after_tool and empty_retries == 1 else RecoveryAction.EMPTY_RETRY
                directive = RecoveryDirective(action, attempts + 1)
                continue
            if fallback is not None and current is not fallback:
                current = fallback
                directive = RecoveryDirective(RecoveryAction.FALLBACK, attempts + 1)
                continue
            break
        return RecoveredResponse(StreamOutcome.SAFE_SENTINEL, self.policy.safe_sentinel, (), usage, None,
                                 sequence, attempts, delivered, "recovery_exhausted")

    def _terminal(self, outcome: StreamOutcome, parts: Sequence[str], tail: str, usage: Mapping[str, int],
                  sequence: int, attempts: int, delivered: bool, reason: str) -> RecoveredResponse:
        text = "".join((*parts, tail))
        if not text and outcome is not StreamOutcome.REPETITION:
            text = self.policy.safe_sentinel
        return RecoveredResponse(outcome, text, (), usage, None, sequence, attempts, delivered, reason)


def adapt_openai_event(event: Mapping[str, Any]) -> tuple[ProviderChunk, ...]:
    """Map one Chat Completions/Responses-shaped event into canonical chunks."""
    usage = event.get("usage") if isinstance(event.get("usage"), Mapping) else {}
    event_type = event.get("type")
    if event_type == "response.output_text.delta":
        return (ProviderChunk(str(event.get("delta") or "")),)
    if event_type == "response.output_item.added":
        item = event.get("item") if isinstance(event.get("item"), Mapping) else {}
        if item.get("type") == "function_call":
            return (ProviderChunk(tool_call={"index": event.get("output_index", 0),
                                             "id": str(item.get("call_id") or item.get("id") or ""),
                                             "function": {"name": str(item.get("name") or ""),
                                                          "arguments": str(item.get("arguments") or "")}}),)
    if event_type == "response.function_call_arguments.delta":
        return (ProviderChunk(tool_call={"index": event.get("output_index", 0),
                                         "function": {"arguments": str(event.get("delta") or "")}}),)
    if event_type in {"response.completed", "response.incomplete", "response.failed"}:
        response = event.get("response") if isinstance(event.get("response"), Mapping) else {}
        response_usage = response.get("usage") if isinstance(response.get("usage"), Mapping) else usage
        reason = "stop" if event_type == "response.completed" else (
            "length" if event_type == "response.incomplete" else "refusal"
        )
        return (ProviderChunk(usage=response_usage, finish_reason=reason),)
    chunks: list[ProviderChunk] = []
    choices = event.get("choices")
    if isinstance(choices, list):
        for choice in choices:
            if not isinstance(choice, Mapping):
                continue
            delta = choice.get("delta") if isinstance(choice.get("delta"), Mapping) else {}
            calls = delta.get("tool_calls") if isinstance(delta.get("tool_calls"), list) else ()
            if not calls:
                chunks.append(ProviderChunk(str(delta.get("content") or ""), usage,
                                            choice.get("finish_reason") if isinstance(choice.get("finish_reason"), str) else None,
                                            reasoning_delta=str(delta.get("reasoning") or ""),
                                            refusal=str(delta.get("refusal") or "")))
            for call in calls:
                if isinstance(call, Mapping):
                    chunks.append(ProviderChunk(tool_call=call, usage=usage,
                                                finish_reason=choice.get("finish_reason") if isinstance(choice.get("finish_reason"), str) else None))
    elif usage:
        chunks.append(ProviderChunk(usage=usage))
    return tuple(chunks)


def adapt_anthropic_event(event: Mapping[str, Any]) -> tuple[ProviderChunk, ...]:
    """Map one Anthropic Messages event into canonical chunks."""
    kind = event.get("type")
    delta = event.get("delta") if isinstance(event.get("delta"), Mapping) else {}
    usage = event.get("usage") if isinstance(event.get("usage"), Mapping) else {}
    if kind == "content_block_start":
        block = event.get("content_block") if isinstance(event.get("content_block"), Mapping) else {}
        if block.get("type") == "tool_use":
            return (ProviderChunk(tool_call={"index": event.get("index", 0), "id": str(block.get("id") or ""),
                                             "function": {"name": str(block.get("name") or ""), "arguments": ""}}),)
    if kind == "content_block_delta":
        if delta.get("type") == "input_json_delta":
            index = event.get("index", 0)
            return (ProviderChunk(tool_call={"index": index, "function": {"arguments": str(delta.get("partial_json") or "")}}),)
        return (ProviderChunk(str(delta.get("text") or "")),)
    if kind == "message_delta":
        reason = delta.get("stop_reason")
        mapped = "length" if reason == "max_tokens" else ("tool_calls" if reason == "tool_use" else "stop")
        return (ProviderChunk(usage=usage, finish_reason=mapped),)
    return (ProviderChunk(usage=usage),) if usage else ()


def _valid_json(value: str) -> bool:
    try:
        parsed = json.loads(value)
        return isinstance(parsed, Mapping)
    except (TypeError, json.JSONDecodeError):
        return False


def _repetition_dominated(text: str) -> bool:
    vendor = Path(__file__).resolve().parents[3] / "vendor" / "hermes"
    vendor_text = str(vendor)
    if vendor_text not in sys.path:
        sys.path.insert(0, vendor_text)
    from agent.repetition_guard import is_repetition_dominated  # type: ignore
    return bool(is_repetition_dominated(text))
