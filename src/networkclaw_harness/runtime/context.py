"""Stable prompt prefixes, budgeted context assembly, and compaction ports."""

from __future__ import annotations

import hashlib
import threading
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
from dataclasses import dataclass, field
from typing import Callable, Mapping, Protocol, Sequence, Any

from .durable import DurableSessionPort
from .history import TranscriptMessage, history_hash, validate_transcript
from .models import FactKind, SemanticFact


class ContextError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class ContextPressure:
    input_tokens: int
    output_headroom: int
    context_window: int
    provider_input_tokens: int | None = None
    provider_output_tokens: int = 0
    system_tokens: int = 0
    tool_tokens: int = 0
    history_tokens: int = 0
    plan_tokens: int = 0

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_headroom

    @property
    def over_limit(self) -> bool:
        return self.total_tokens > self.context_window

    @property
    def headroom(self) -> int:
        return max(0, self.context_window - self.total_tokens)


class ContextPreflight:
    """Provider-aware compression gate with cooldown and late-result fencing."""

    def __init__(self, *, context_window: int, output_reserve: int,
                 threshold: float = .9, cooldown_turns: int = 2) -> None:
        if context_window <= 0 or output_reserve < 0 or not 0 < threshold <= 1 or cooldown_turns < 0:
            raise ValueError("invalid context preflight configuration")
        self.context_window = context_window
        self.output_reserve = output_reserve
        self.threshold = threshold
        self.cooldown_turns = cooldown_turns
        self._last_compacted_turn = -10**9

    def estimate(self, items: Sequence[ContextItem], *, system_tokens: int = 0,
                 tool_tokens: int = 0, plan_tokens: int = 0,
                 provider_usage: Mapping[str, int] | None = None,
                 context_window: int | None = None) -> ContextPressure:
        # Token counts are supplied by the provider adapter; this is deliberately
        # not a fixed character heuristic.
        usage = provider_usage or {}
        input_tokens = sum(i.tokens for i in items)
        provider_input = usage.get("input_tokens")
        if isinstance(provider_input, int) and provider_input >= 0:
            # Provider usage is authoritative when available; local counts are
            # still retained as a diagnostic component breakdown.
            input_tokens = provider_input
        provider_output = usage.get("output_tokens", 0)
        if not isinstance(provider_output, int) or provider_output < 0:
            provider_output = 0
        return ContextPressure(
            system_tokens + tool_tokens + plan_tokens + input_tokens,
            self.output_reserve, context_window or self.context_window,
            provider_input, provider_output, system_tokens, tool_tokens,
            sum(i.tokens for i in items), plan_tokens,
        )

    def should_compact(self, pressure: ContextPressure, *, turn: int) -> bool:
        if turn - self._last_compacted_turn < self.cooldown_turns:
            return False
        return pressure.total_tokens >= int(self.context_window * self.threshold)

    def mark_compacted(self, turn: int) -> None:
        self._last_compacted_turn = turn


@dataclass(frozen=True, slots=True)
class CompactionFence:
    session_id: str
    input_hash: str
    turn: int

    def accepts(self, *, session_id: str, input_hash: str, current_turn: int) -> bool:
        return self.session_id == session_id and self.input_hash == input_hash and current_turn == self.turn


@dataclass(frozen=True, slots=True)
class PromptConfiguration:
    system_prompt: bytes
    skill_ids: tuple[str, ...] = ()
    tool_schema_hash: str = ""

    def __post_init__(self) -> None:
        if not self.system_prompt or not self.tool_schema_hash:
            raise ValueError("system prompt and tool schema hash are required")
        if any(not item or not item.isascii() for item in self.skill_ids):
            raise ValueError("skill ids must be non-empty ASCII strings")

    @property
    def fingerprint(self) -> str:
        digest = hashlib.sha256()
        digest.update(self.system_prompt)
        digest.update(b"\0")
        digest.update(self.tool_schema_hash.encode("ascii"))
        for skill_id in self.skill_ids:
            digest.update(b"\0")
            digest.update(skill_id.encode("ascii"))
        return digest.hexdigest()


class PromptCacheRegistry:
    """Binds system prompt, skill set, and tool schema to one session lifecycle."""

    def __init__(self) -> None:
        self._bindings: dict[str, PromptConfiguration] = {}

    def bind(self, session_id: str, configuration: PromptConfiguration) -> str:
        current = self._bindings.get(session_id)
        if current is not None and current != configuration:
            raise ContextError(
                "prompt_prefix_changed",
                "skills, tools, and system prompt are frozen until a new session",
            )
        self._bindings[session_id] = configuration
        return configuration.fingerprint

    def get(self, session_id: str) -> PromptConfiguration | None:
        return self._bindings.get(session_id)

    def close(self, session_id: str) -> None:
        self._bindings.pop(session_id, None)


@dataclass(frozen=True, slots=True)
class ContextBudget:
    model_tokens: int
    output_tokens: int
    system_tokens: int
    history_tokens: int
    tool_schema_tokens: int
    plan_tokens: int

    @property
    def available_input_tokens(self) -> int:
        return self.model_tokens - self.output_tokens - self.system_tokens - self.history_tokens - self.tool_schema_tokens - self.plan_tokens


@dataclass(frozen=True, slots=True)
class ContextItem:
    item_id: str
    kind: str
    text: str
    tokens: int
    source: Mapping[str, str]
    critical: bool = False
    summary: str | None = None
    summary_tokens: int | None = None

    def __post_init__(self) -> None:
        if not self.item_id or not self.item_id.isascii() or self.tokens < 0:
            raise ValueError("context item requires an ASCII id and non-negative tokens")
        if self.summary is not None and (self.summary_tokens is None or self.summary_tokens < 0):
            raise ValueError("context item summary requires a non-negative token count")


@dataclass(frozen=True, slots=True)
class AssembledPrompt:
    system_prompt: bytes
    cache_key: str
    items: tuple[ContextItem, ...]
    input_tokens: int
    output_tokens: int
    used_summaries: tuple[str, ...]
    omitted_items: tuple[str, ...]


class ContextAssembler:
    def assemble(self, *, session_id: str, configuration: PromptConfiguration,
                 registry: PromptCacheRegistry, budget: ContextBudget,
                 items: Sequence[ContextItem]) -> AssembledPrompt:
        cache_key = registry.bind(session_id, configuration)
        if budget.available_input_tokens < 0:
            raise ContextError("context_budget_invalid", "fixed prompt components exceed the model context window")
        selected: list[ContextItem] = []
        used_summaries: list[str] = []
        omitted: list[str] = []
        remaining = budget.available_input_tokens
        for item in items:
            if item.tokens <= remaining:
                selected.append(item)
                remaining -= item.tokens
                continue
            if item.summary is not None and item.summary_tokens is not None and item.summary_tokens <= remaining:
                selected.append(ContextItem(
                    item.item_id, item.kind, item.summary, item.summary_tokens,
                    item.source, item.critical, item.summary, item.summary_tokens,
                ))
                used_summaries.append(item.item_id)
                remaining -= item.summary_tokens
                continue
            if item.critical:
                raise ContextError(
                    "context_selection_required",
                    "critical context does not fit; compact it or request a user selection",
                )
            omitted.append(item.item_id)
        input_tokens = budget.model_tokens - budget.output_tokens - remaining
        return AssembledPrompt(configuration.system_prompt, cache_key, tuple(selected), input_tokens,
                               budget.output_tokens, tuple(used_summaries), tuple(omitted))


@dataclass(frozen=True, slots=True)
class CompactionRequest:
    session_id: str
    input_start: int
    input_end: int
    messages: tuple[TranscriptMessage, ...]
    unresolved_ids: tuple[str, ...]
    unknown_invocation_ids: tuple[str, ...]
    source_refs: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class CompactionResult:
    version: str
    summary: str
    summary_artifact_id: str
    retained_messages: tuple[TranscriptMessage, ...]
    unresolved_ids: tuple[str, ...]
    unknown_invocation_ids: tuple[str, ...]
    source_refs: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class CompactionPolicy:
    """Operational limits for production compaction, independent of Hermes."""

    timeout_seconds: float = 15.0
    max_attempts: int = 2
    cooldown_turns: int = 2
    max_output_chars: int = 16_384
    retain_latest_messages: int = 8

    def __post_init__(self) -> None:
        if self.timeout_seconds <= 0 or self.max_attempts <= 0 or self.cooldown_turns < 0:
            raise ValueError("invalid compaction policy")
        if self.max_output_chars <= 0 or self.retain_latest_messages < 0:
            raise ValueError("invalid compaction bounds")


@dataclass(frozen=True, slots=True)
class CompactionReceipt:
    """Charge/refund state for a compaction attempt.

    A receipt is refundable only while no summary provider call was emitted.
    """

    attempt: int
    request_emitted: bool = False
    token_emitted: bool = False
    refunded: bool = False

    @property
    def refundable(self) -> bool:
        return not self.request_emitted and not self.token_emitted and not self.refunded


@dataclass(frozen=True, slots=True)
class CompactionBinding:
    session_id: str
    input_hash: str
    summary_artifact_id: str
    summary_hash: str
    durable_cursor: str
    workspace_epoch: int
    checkpoint_hash: str
    resume_tail: tuple[TranscriptMessage, ...]

    def accepts(self, *, session_id: str, input_hash: str, workspace_epoch: int) -> bool:
        return (self.session_id == session_id and self.input_hash == input_hash
                and self.workspace_epoch == workspace_epoch)


class SessionRotationPort(Protocol):
    def rotate(self, session_id: str, *, result: CompactionResult,
               resume_tail: tuple[TranscriptMessage, ...], input_hash: str,
               workspace_epoch: int) -> str: ...


@dataclass(frozen=True, slots=True)
class CompactionOutcome:
    status: str
    result: CompactionResult | None = None
    reason: str | None = None
    receipt: CompactionReceipt | None = None
    binding: CompactionBinding | None = None


class CompactionCoordinator:
    """Coordinates bounded compression and rejects stale/late writers."""

    def __init__(self, service: "CompactionService", *, policy: CompactionPolicy = CompactionPolicy(),
                 rotation: SessionRotationPort | None = None) -> None:
        self._service = service
        self._policy = policy
        self._rotation = rotation
        self._lock = threading.Lock()
        self._last_turn: dict[str, int] = {}
        self._attempts: dict[str, int] = {}
        self._bindings: dict[str, CompactionBinding] = {}

    def compact(self, request: CompactionRequest, *, event_id: str, turn: int,
                workspace_epoch: int = 0, lock_held: bool = False,
                provider_available: bool = True) -> CompactionOutcome:
        if not provider_available:
            return CompactionOutcome("deferred", reason="provider_unavailable",
                                     receipt=CompactionReceipt(0, refunded=True))
        with self._lock:
            last = self._last_turn.get(request.session_id, -10**9)
            if turn - last < self._policy.cooldown_turns:
                return CompactionOutcome("deferred", reason="cooldown",
                                         receipt=CompactionReceipt(0, refunded=True))
            if lock_held:
                return CompactionOutcome("deferred", reason="lock_busy",
                                         receipt=CompactionReceipt(0, refunded=True))
            attempt = self._attempts.get(request.session_id, 0) + 1
            if attempt > self._policy.max_attempts:
                return CompactionOutcome("deferred", reason="attempt_cap",
                                         receipt=CompactionReceipt(attempt, refunded=True))
            self._attempts[request.session_id] = attempt
        receipt = CompactionReceipt(attempt)
        active = threading.Event()
        active.set()
        try:
            pool = ThreadPoolExecutor(max_workers=1)
            future = pool.submit(self._service.compact, request, event_id=event_id,
                                 commit_allowed=active.is_set)
            try:
                result = future.result(timeout=self._policy.timeout_seconds)
            finally:
                # A timed-out Hermes worker is fenced from publishing by the
                # caller's binding/epoch check; never block the coordinator on it.
                pool.shutdown(wait=False, cancel_futures=True)
        except FutureTimeoutError:
            active.clear()
            return CompactionOutcome("fallback", result=_fallback_result(request), reason="timeout", receipt=receipt)
        except Exception as error:
            active.clear()
            return CompactionOutcome("fallback", result=_fallback_result(request),
                                     reason=getattr(error, "code", "summary_failed"), receipt=receipt)
        input_hash = history_hash(request.messages)
        tail = result.retained_messages[-self._policy.retain_latest_messages:] if self._policy.retain_latest_messages else ()
        summary_hash = hashlib.sha256(result.summary.encode("utf-8")).hexdigest()
        cursor = ""
        if self._rotation is not None:
            try:
                cursor = self._rotation.rotate(request.session_id, result=result,
                                               resume_tail=tuple(tail), input_hash=input_hash,
                                               workspace_epoch=workspace_epoch)
            except Exception as error:
                return CompactionOutcome("fallback", reason=getattr(error, "code", "rotation_failed"), receipt=receipt)
        checkpoint_hash = hashlib.sha256((summary_hash + input_hash + cursor + str(workspace_epoch)).encode()).hexdigest()
        binding = CompactionBinding(request.session_id, input_hash, result.summary_artifact_id,
                                    summary_hash, cursor, workspace_epoch, checkpoint_hash, tuple(tail))
        with self._lock:
            self._last_turn[request.session_id] = turn
            self._bindings[request.session_id] = binding
        return CompactionOutcome("completed", result=result, receipt=CompactionReceipt(attempt, True, True), binding=binding)

    def accepts_late_commit(self, binding: CompactionBinding, *, current_input_hash: str,
                            workspace_epoch: int) -> bool:
        with self._lock:
            current = self._bindings.get(binding.session_id)
            return current == binding and binding.accepts(session_id=binding.session_id,
                                                          input_hash=current_input_hash,
                                                          workspace_epoch=workspace_epoch)


def _fallback_result(request: CompactionRequest) -> CompactionResult:
    """Deterministic, non-provider summary used when routing or timeout fails."""
    snippets = [message.content.strip() for message in request.messages
                if message.content and message.role in {"user", "assistant", "tool"}]
    text = " ".join(snippets)[-4096:]
    summary = "Fallback context summary: " + (text or "No durable transcript content was available.")
    artifact_id = "compaction-fallback-" + hashlib.sha256(
        f"{request.session_id}:{request.input_start}:{request.input_end}".encode()
    ).hexdigest()[:24]
    return CompactionResult(
        "fallback-v1", summary, artifact_id, request.messages[-8:],
        request.unresolved_ids, request.unknown_invocation_ids, request.source_refs,
    )


def bounded_context_items(items: Sequence[ContextItem], *, max_tokens: int,
                          protect_latest: int = 2) -> tuple[ContextItem, ...]:
    """Bound tool/history context while preserving newest and critical evidence."""
    if max_tokens < 0 or protect_latest < 0:
        raise ValueError("context bounds must be non-negative")
    newest = list(items[-protect_latest:]) if protect_latest else []
    selected: list[ContextItem] = []
    used = 0
    for item in (*[i for i in items if i.critical], *newest, *reversed(items)):
        if item in selected or used + item.tokens > max_tokens:
            continue
        selected.append(item)
        used += item.tokens
    order = {id(item): index for index, item in enumerate(items)}
    return tuple(sorted(selected, key=lambda item: order[id(item)]))


def bound_tool_output(text: str, *, max_chars: int = 8192) -> tuple[str, bool]:
    """Return a deterministic, UTF-8 safe excerpt before context assembly."""
    if max_chars <= 0:
        raise ValueError("max_chars must be positive")
    if len(text) <= max_chars:
        return text, False
    marker = "\n...[output bounded; full content must be referenced by artifact]"
    if len(marker) > max_chars:
        return marker[:max_chars], True
    keep = max(0, max_chars - len(marker))
    return text[:keep] + marker, True


class HermesCompactionPort(Protocol):
    def compact(self, request: CompactionRequest) -> CompactionResult: ...


class HermesCompactionRuntime(Protocol):
    """The small surface supplied by the host-created Hermes compression runtime."""

    def compact_session(self, request: CompactionRequest) -> CompactionResult: ...


class HermesCompactionAdapter:
    """Keeps vendored Hermes construction outside the Harness semantic runtime."""

    def __init__(self, runtime: HermesCompactionRuntime) -> None:
        self._runtime = runtime

    def compact(self, request: CompactionRequest) -> CompactionResult:
        return self._runtime.compact_session(request)


class CompactionService:
    def __init__(self, durable: DurableSessionPort, adapter: HermesCompactionPort,
                 artifact_commit: Callable[[CompactionRequest, CompactionResult], None],
                 commit_allowed: Callable[[], bool] | None = None) -> None:
        self._durable = durable
        self._adapter = adapter
        self._artifact_commit = artifact_commit
        self._commit_allowed = commit_allowed or (lambda: True)

    def compact(self, request: CompactionRequest, *, event_id: str,
                commit_allowed: Callable[[], bool] | None = None) -> CompactionResult:
        validate_transcript(request.messages)
        result = self._adapter.compact(request)
        validate_transcript(result.retained_messages)
        if set(request.unknown_invocation_ids) - set(result.unknown_invocation_ids):
            raise ContextError("compaction_unknown_lost", "compaction cannot convert an unknown invocation into success")
        if set(request.unresolved_ids) - set(result.unresolved_ids):
            raise ContextError("compaction_unresolved_lost", "compaction cannot discard unresolved interactions")
        if set(request.source_refs) - set(result.source_refs):
            raise ContextError("compaction_source_lost", "compaction must retain source references")
        allowed = commit_allowed or self._commit_allowed
        if not allowed():
            raise ContextError("compaction_fenced", "late compaction result was fenced before commit")
        self._artifact_commit(request, result)
        payload = {
            "version": result.version, "input_start": request.input_start, "input_end": request.input_end,
            "input_hash": history_hash(request.messages), "summary_artifact_id": result.summary_artifact_id,
            "unresolved_ids": list(result.unresolved_ids),
            "unknown_invocation_ids": list(result.unknown_invocation_ids), "source_refs": list(result.source_refs),
        }
        if not allowed():
            raise ContextError("compaction_fenced", "late compaction result was fenced before durable commit")
        self._durable.commit(
            session_id=request.session_id, event_id=event_id,
            facts=(SemanticFact(FactKind.COMPACTION, result.summary_artifact_id, "completed", payload),),
        )
        return result


class ReferenceHermesCompaction:
    """Adapter-shaped reference; production calls Hermes' atomic session rotation API."""

    def __init__(self, *, version: str = "hermes-reference-v1") -> None:
        self._version = version

    def compact(self, request: CompactionRequest) -> CompactionResult:
        # A concise user/assistant summary keeps the transcript state machine legal.
        summary = "Compacted history; preserve referenced artifacts and unresolved state."
        retained = (
            TranscriptMessage("user", summary, "user_interaction"),
            TranscriptMessage("assistant", "Context restored from compacted history.", "model_step"),
        )
        artifact_id = "compaction-" + hashlib.sha256(
            f"{request.session_id}:{request.input_start}:{request.input_end}".encode()
        ).hexdigest()[:24]
        return CompactionResult(
            self._version, summary, artifact_id, retained, request.unresolved_ids,
            request.unknown_invocation_ids, request.source_refs,
        )
