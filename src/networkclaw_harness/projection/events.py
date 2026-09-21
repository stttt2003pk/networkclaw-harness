"""Safe, deterministic projection from durable facts to client events."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import Any, Mapping, Sequence

from networkclaw_harness.runtime import DurableSessionPort, FactKind, RunStatus, StoredFact


class ProjectionError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class VisibilityMode(StrEnum):
    VISIBLE = "visible"
    SUMMARY = "summary"
    ARTIFACT_ONLY = "artifact_only"
    HIDDEN = "hidden"


@dataclass(frozen=True, slots=True)
class VisibilityRule:
    event_type: str
    mode: VisibilityMode
    tool_name: str | None = None
    tenant_id: str | None = None
    profile: str | None = None


@dataclass(frozen=True, slots=True)
class ProjectionContext:
    tenant_id: str
    user_id: str
    session_id: str
    profile: str
    secret_values: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ProjectedEvent:
    event_id: str
    type: str
    session_id: str
    cursor: str
    payload: Mapping[str, Any]
    run_id: str | None = None
    item_id: str | None = None
    invocation_id: str | None = None
    interaction_id: str | None = None
    artifact_id: str | None = None
    terminal: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "payload", MappingProxyType(dict(self.payload)))


class EventVisibilityPolicy:
    """Most-specific matching rule wins; hidden data never reaches serialization."""

    def __init__(self, rules: Sequence[VisibilityRule] = ()) -> None:
        self._rules = tuple(rules)

    def decide(self, *, event_type: str, tenant_id: str, profile: str,
               tool_name: str | None = None) -> VisibilityMode:
        candidates: list[tuple[int, int, VisibilityMode]] = []
        for index, rule in enumerate(self._rules):
            if rule.event_type not in {event_type, "*"}:
                continue
            if rule.tool_name is not None and rule.tool_name != tool_name:
                continue
            if rule.tenant_id is not None and rule.tenant_id != tenant_id:
                continue
            if rule.profile is not None and rule.profile != profile:
                continue
            specificity = sum(value is not None for value in (
                rule.tool_name, rule.tenant_id, rule.profile,
            )) + (rule.event_type != "*")
            candidates.append((specificity, index, rule.mode))
        return max(candidates, key=lambda item: (item[0], item[1]))[2] if candidates else VisibilityMode.VISIBLE


class EventProjector:
    def __init__(self, durable: DurableSessionPort,
                 policy: EventVisibilityPolicy | None = None,
                 *, max_events: int = 512, max_payload_bytes: int = 32 * 1024) -> None:
        if max_events <= 0 or max_payload_bytes <= 0:
            raise ValueError("projection limits must be positive")
        self._durable = durable
        self._policy = policy or EventVisibilityPolicy()
        self._max_events = max_events
        self._max_payload_bytes = max_payload_bytes

    def project(self, context: ProjectionContext, *, after_cursor: int = 0) -> tuple[ProjectedEvent, ...]:
        events: list[ProjectedEvent] = []
        terminal_runs: set[str] = set()
        active_run: str | None = None
        for stored in self._durable.load(context.session_id):
            fact = stored.fact
            # A callback arriving after a terminal RUN must not become the
            # implicit run context for subsequent facts that omit run_id.
            # Fence the active pointer before projecting the late fact.
            if (fact.kind is FactKind.RUN and fact.state == RunStatus.RUNNING
                    and fact.entity_id not in terminal_runs):
                active_run = fact.entity_id
            elif fact.kind is FactKind.RUN and fact.state in _TERMINAL_RUN_STATES:
                if active_run == fact.entity_id:
                    active_run = None
            run_id = _run_id(fact) or active_run
            projected = self._project_fact(
                stored, context, late=bool(run_id and run_id in terminal_runs),
                fallback_run_id=run_id,
            )
            if stored.cursor > after_cursor:
                events.extend(projected)
                if len(events) > self._max_events:
                    raise ProjectionError("backpressure", "projected event batch exceeds its configured limit")
            if fact.kind is FactKind.RUN and fact.state in _TERMINAL_RUN_STATES:
                terminal_runs.add(fact.entity_id)
        return tuple(events)

    def _project_fact(self, stored: StoredFact, context: ProjectionContext,
                      *, late: bool, fallback_run_id: str | None) -> tuple[ProjectedEvent, ...]:
        fact = stored.fact
        run_id = _run_id(fact) or fallback_run_id
        if late and _would_reactivate(fact):
            return (self._event(
                stored, context, "warning", {
                    "code": "late_fact_ignored",
                    "summary": "A late fact was recorded after the run reached a terminal state.",
                    "fact_kind": fact.kind, "fact_state": fact.state,
                }, run_id=run_id, item_id=fact.entity_id,
            ),)
        result: list[ProjectedEvent] = []
        for event_type, payload, identities, terminal in _event_specs(fact):
            tool_name = str(fact.payload.get("tool_name", "")) or None
            mode = self._policy.decide(
                event_type=event_type, tenant_id=context.tenant_id,
                profile=context.profile, tool_name=tool_name,
            )
            if mode is VisibilityMode.HIDDEN:
                continue
            safe = project_mapping(
                _safe_event_payload(event_type, payload),
                secret_values=context.secret_values,
            )
            safe = _bound_payload(_apply_visibility(safe, mode), self._max_payload_bytes)
            result.append(self._event(
                stored, context, event_type, safe, terminal=terminal,
                run_id=identities.get("run_id") or run_id,
                item_id=identities.get("item_id"),
                invocation_id=identities.get("invocation_id"),
                interaction_id=identities.get("interaction_id"),
                artifact_id=identities.get("artifact_id"), index=len(result),
            ))
        return tuple(result)

    @staticmethod
    def _event(stored: StoredFact, context: ProjectionContext, event_type: str,
               payload: Mapping[str, Any], *, run_id: str | None = None,
               item_id: str | None = None, invocation_id: str | None = None,
               interaction_id: str | None = None, artifact_id: str | None = None,
               terminal: bool = False, index: int = 0) -> ProjectedEvent:
        identity = f"{stored.session_id}:{stored.cursor}:{index}:{event_type}"
        event_id = "projection-" + hashlib.sha256(identity.encode()).hexdigest()[:24]
        return ProjectedEvent(
            event_id, event_type, context.session_id, str(stored.cursor), payload,
            run_id=run_id, item_id=item_id, invocation_id=invocation_id,
            interaction_id=interaction_id, artifact_id=artifact_id, terminal=terminal,
        )


@dataclass(frozen=True, slots=True)
class DeltaBatch:
    content: str
    first_sequence: int
    last_sequence: int
    gaps: tuple[int, ...]
    final: bool


class DeltaCoalescer:
    """Coalesces transient model chunks without making them the final source of truth."""

    def __init__(self, *, max_chars: int = 2048, max_chunks: int = 32) -> None:
        if max_chars <= 0 or max_chunks <= 0:
            raise ValueError("delta limits must be positive")
        self._max_chars = max_chars
        self._max_chunks = max_chunks
        self._parts: list[tuple[int, str]] = []
        self._next_sequence = 1
        self._gaps: list[int] = []
        self._all_gaps: list[int] = []

    def add(self, sequence: int, delta: str) -> DeltaBatch | None:
        if sequence < self._next_sequence:
            raise ProjectionError("delta_out_of_order", "delta sequence moved backwards")
        if sequence > self._next_sequence:
            missing = list(range(self._next_sequence, sequence))
            self._gaps.extend(missing)
            self._all_gaps.extend(missing)
        self._next_sequence = sequence + 1
        self._parts.append((sequence, delta))
        if sum(len(value) for _, value in self._parts) >= self._max_chars or len(self._parts) >= self._max_chunks:
            return self._flush(final=False)
        return None

    def finish(self, final_content: str) -> tuple[DeltaBatch, ...]:
        batches: list[DeltaBatch] = []
        if self._parts:
            batches.append(self._flush(final=False))
        last = self._next_sequence - 1
        batches.append(DeltaBatch(final_content, last, last, tuple(self._all_gaps), True))
        self._gaps.clear()
        self._all_gaps.clear()
        return tuple(batches)

    def _flush(self, *, final: bool) -> DeltaBatch:
        first, last = self._parts[0][0], self._parts[-1][0]
        batch = DeltaBatch("".join(value for _, value in self._parts), first, last, tuple(self._gaps), final)
        self._parts.clear()
        self._gaps.clear()
        return batch


_SENSITIVE_EXACT = frozenset({
    "api_key", "authorization", "chain_of_thought", "cookie", "internal_config",
    "internal_prompt", "password", "raw_reasoning", "reasoning_content", "scratchpad",
    "secret", "system_prompt", "token",
})


def project_mapping(value: Mapping[str, Any], *,
                    secret_values: Sequence[str] = ()) -> dict[str, Any]:
    """Recursively remove backend-only fields and redact configured secret values."""
    return {
        str(key): "[redacted]" if _sensitive_key(str(key)) else _project_value(item, secret_values)
        for key, item in value.items()
    }


def _project_value(value: Any, secrets: Sequence[str]) -> Any:
    if isinstance(value, Mapping):
        return project_mapping(value, secret_values=secrets)
    if isinstance(value, (list, tuple)):
        return [_project_value(item, secrets) for item in value]
    if isinstance(value, bytes):
        return "[binary omitted]"
    if isinstance(value, str):
        result = value
        for secret in secrets:
            if secret:
                result = result.replace(secret, "[redacted]")
        return result
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return str(value)


def _sensitive_key(key: str) -> bool:
    name = key.casefold().replace("-", "_")
    if name.endswith("_hash") or name in {"token_count", "token_usage"}:
        return False
    if name in _SENSITIVE_EXACT:
        return True
    parts = set(name.split("_"))
    return bool(parts & {"password", "secret", "token", "cookie"}) or "chain_of_thought" in name


_TERMINAL_RUN_STATES = frozenset({
    RunStatus.COMPLETED, RunStatus.FAILED, RunStatus.CANCELLED,
    RunStatus.INTERRUPTED, RunStatus.BLOCKED, RunStatus.EXPIRED,
})


def _event_specs(fact) -> tuple[tuple[str, Mapping[str, Any], dict[str, str], bool], ...]:
    payload = dict(fact.payload)
    # New semantic facts remain consumable by the legacy event stream.  The
    # explicit version lets reconnecting clients reject unknown shapes safely.
    semantic_version = str(payload.pop("schema_version", "networkclaw.semantic.v1"))
    payload["schema_version"] = semantic_version
    if fact.kind is FactKind.PLAN:
        return (("plan.updated", {"state": fact.state, **payload}, {"item_id": fact.entity_id}, False),)
    if fact.kind is FactKind.TOOL_ROUND:
        return ((f"tool.round.{fact.state}", {"status": fact.state, **payload},
                 {"item_id": fact.entity_id}, False),)
    if fact.kind is FactKind.COMPACTION:
        phase = "started" if fact.state in {"started", "compacting"} else "continued"
        return ((f"context.{phase}", {
            "status": fact.state,
            "summary": "Compacting conversation context." if phase == "started" else "Continued with compacted context.",
            "version": payload.get("version"),
        }, {"item_id": fact.entity_id}, False),)
    if fact.kind is FactKind.CHECKPOINT:
        return (("checkpoint.updated", {"status": fact.state, **payload},
                 {"item_id": fact.entity_id}, False),)
    if fact.kind is FactKind.PROVIDER_ATTEMPT:
        retry = fact.state in {"retry", "retrying", "fallback"} or payload.get("attempt", 1) != 1
        return (("provider.retry" if retry else "provider.attempt",
                 {"status": fact.state, **payload}, {"item_id": fact.entity_id}, False),)
    if fact.kind is FactKind.GRACE_SUMMARY:
        return (("grace.summary", {"status": fact.state, **payload},
                 {"item_id": fact.entity_id}, False),)
    if fact.kind is FactKind.FINALIZER:
        return (("finalizer.fallback", {"status": fact.state, **payload},
                 {"item_id": fact.entity_id}, fact.state in _TERMINAL_RUN_STATES),)
    if fact.kind is FactKind.DELIVERY:
        content = str(payload.pop("content", ""))
        return (("assistant.delta", {"content": content, "final": True, **payload},
                 {"item_id": fact.entity_id}, False),)
    if fact.kind is FactKind.INVOCATION:
        kind = "subagent" if payload.get("action_type") == "subagent" else "tool"
        event_type = f"{kind}.started" if fact.state in {"queued", "running"} else f"{kind}.completed"
        return ((event_type, {"status": fact.state, **payload},
                 {"invocation_id": fact.entity_id, "item_id": fact.entity_id}, False),)
    if fact.kind is FactKind.OUTCOME and payload.get("action_type") in {"tool", "subagent"}:
        if payload.get("audit_projection") is True and payload.get("action_type") == "tool":
            return ()
        kind = str(payload["action_type"])
        return ((f"{kind}.completed", {"status": fact.state, **payload},
                 {"invocation_id": fact.entity_id, "item_id": fact.entity_id}, False),)
    if fact.kind is FactKind.INTERACTION:
        kind = str(payload.get("kind", ""))
        prefix = "approval" if kind == "approval" else "clarification"
        suffix = "requested" if fact.state == "pending" else "resolved"
        return ((f"{prefix}.{suffix}", {"status": fact.state, **payload},
                 {"interaction_id": fact.entity_id,
                  "item_id": str(payload.get("item_id", fact.entity_id))}, False),)
    if fact.kind is FactKind.ARTIFACT_REF:
        return (("artifact.created", {"status": fact.state, **payload},
                 {"artifact_id": fact.entity_id, "item_id": fact.entity_id}, False),)
    if fact.kind is FactKind.UNRESOLVED_ITEM:
        return (("warning", {"code": "unresolved_item", "status": fact.state, **payload},
                 {"item_id": fact.entity_id}, False),)
    if fact.kind is FactKind.OBSERVATION and fact.state in {"warning", "error"}:
        dimension = payload.get("dimension")
        if fact.state == "warning" and dimension in {"iterations", "run_time", "tool_safety"}:
            labels = {
                "iterations": "Approaching the model iteration limit.",
                "run_time": "Approaching the run time limit; finishing from existing evidence.",
                "tool_safety": "Approaching the tool safety limit.",
            }
            payload = {**payload, "code": f"budget_{dimension}", "summary": labels[str(dimension)]}
        return ((fact.state, payload, {"item_id": fact.entity_id}, fact.state == "error"),)
    if fact.kind is FactKind.RUN:
        if fact.state == RunStatus.RUNNING:
            event_type, terminal = "turn.started", False
        elif fact.state == RunStatus.COMPLETED:
            event_type, terminal = "turn.completed", True
        elif fact.state in _TERMINAL_RUN_STATES:
            event_type, terminal = "turn.failed", True
        else:
            return ()
        return ((event_type, {"status": fact.state, **payload},
                 {"run_id": fact.entity_id, "item_id": fact.entity_id}, terminal),)
    return ()


def _run_id(fact) -> str | None:
    if fact.kind in {FactKind.RUN, FactKind.DELIVERY}:
        return fact.entity_id
    value = fact.payload.get("run_id")
    return str(value) if value else None


def _would_reactivate(fact) -> bool:
    if fact.kind is FactKind.RUN:
        return fact.state in {RunStatus.RUNNING, RunStatus.WAITING, "cancel_requested"}
    # Invocation callbacks (including their outcomes) and duplicate delivery
    # callbacks must not create a second visible execution after termination.
    return fact.kind in {
        FactKind.PLAN, FactKind.INVOCATION, FactKind.OUTCOME,
        FactKind.INTERACTION, FactKind.DELIVERY,
    }


def _apply_visibility(payload: Mapping[str, Any], mode: VisibilityMode) -> dict[str, Any]:
    if mode is VisibilityMode.VISIBLE:
        return dict(payload)
    summary = payload.get("summary") or payload.get("message") or payload.get("status") or "Available"
    if mode is VisibilityMode.SUMMARY:
        result = {"summary": str(summary)}
        for name in ("status", "code", "artifact_id", "evidence_refs", "unresolved"):
            if name in payload:
                result[name] = payload[name]
        return result
    return {"summary": str(summary), "artifact_id": payload.get("artifact_id"), "content_hidden": True}


_EXECUTION_EVENT_FIELDS = frozenset({
    "action_type", "artifact_id", "business_success", "code", "error_code",
    "display_arguments", "duration_ms", "ended_at", "evidence_refs", "progress",
    "replay_allowed", "result", "started_at", "status", "summary", "tool_name", "unresolved",
})

# Backend-only finalizer/diagnostic material must never be forwarded to a
# client projection, even when it is attached to an otherwise safe fact such
# as DELIVERY or RUN.  Secrets are handled separately by ``project_mapping``;
# these keys are intentionally dropped rather than redacted because they do
# not have a useful client-facing representation.
_UNSAFE_EVENT_FIELDS = frozenset({
    "internal_finalizer_prompt", "finalizer_prompt", "reasoning",
    "raw_reasoning", "reasoning_content", "chain_of_thought", "scratchpad",
    "raw_diagnostics", "debug_diagnostics", "debug_trace", "traceback",
})


def _safe_event_payload(event_type: str, payload: Mapping[str, Any]) -> dict[str, Any]:
    """Keep raw invocation arguments and executor results behind artifact access."""
    payload = _drop_unsafe_fields(payload)
    if event_type.startswith(("tool.", "subagent.")):
        safe = {key: value for key, value in payload.items() if key in _EXECUTION_EVENT_FIELDS}
        if "result" in safe:
            safe["result"] = _bounded_result(safe["result"])
        return safe
    return dict(payload)


def _drop_unsafe_fields(value: Any) -> Any:
    """Remove backend-only diagnostic fields recursively for client events."""
    if isinstance(value, Mapping):
        return {
            key: _drop_unsafe_fields(item)
            for key, item in value.items()
            if str(key).casefold() not in _UNSAFE_EVENT_FIELDS
        }
    if isinstance(value, (list, tuple)):
        return [_drop_unsafe_fields(item) for item in value]
    return value


def _bounded_result(value: Any) -> Any:
    """Keep a presentation result small and omit fields commonly carrying raw output."""
    if isinstance(value, Mapping):
        return {
            str(key): _bounded_result(item)
            for key, item in value.items()
            if str(key).casefold() not in {"raw", "raw_output", "debug", "traceback"}
        }
    if isinstance(value, (list, tuple)):
        return [_bounded_result(item) for item in value[:64]]
    if isinstance(value, str):
        return value[:4096] + ("...[truncated]" if len(value) > 4096 else "")
    return value


def _bound_payload(payload: Mapping[str, Any], limit: int) -> dict[str, Any]:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
    if len(encoded) <= limit:
        return dict(payload)
    summary = str(payload.get("summary") or payload.get("message") or "Output available as an artifact")
    return {
        "summary": summary[:512], "content_truncated": True,
        "artifact_id": payload.get("artifact_id"),
        "unresolved": payload.get("unresolved", []),
    }
