"""Versioned, redacted semantic facts and timeline projection.

The timeline is a view over durable facts, never a second source of truth.  Its
identity is deliberately made from the durable cursor/event id and the run
hierarchy so live, history and resume can merge the same records safely.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Iterable, Mapping

from networkclaw_harness.runtime import FactKind, RunStatus, SemanticFact, StoredFact

from .events import project_mapping

SEMANTIC_SCHEMA_VERSION = "networkclaw.semantic.v1"
TIMELINE_SCHEMA_VERSION = "networkclaw.timeline.v1"
_TERMINAL = frozenset({s.value for s in (RunStatus.COMPLETED, RunStatus.FAILED,
    RunStatus.CANCELLED, RunStatus.INTERRUPTED, RunStatus.BLOCKED, RunStatus.EXPIRED)})


@dataclass(frozen=True, slots=True)
class SemanticFactEnvelope:
    schema_version: str
    fact: SemanticFact
    redacted_payload: Mapping[str, Any]

    def __post_init__(self) -> None:
        object.__setattr__(self, "redacted_payload", MappingProxyType(dict(self.redacted_payload)))


def versioned_fact(fact: SemanticFact, *, secret_values: Iterable[str] = (),
                  max_payload_bytes: int = 32 * 1024) -> SemanticFactEnvelope:
    """Return a stable schema-versioned and client-safe fact envelope."""
    if max_payload_bytes <= 0:
        raise ValueError("max_payload_bytes must be positive")
    payload = _sanitize(project_mapping(dict(fact.payload), secret_values=tuple(secret_values)))
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    if len(encoded.encode()) > max_payload_bytes:
        payload = {"summary": str(payload.get("summary") or payload.get("message") or "Output available as an artifact")[:512],
                   "content_truncated": True, "artifact_id": payload.get("artifact_id")}
    return SemanticFactEnvelope(SEMANTIC_SCHEMA_VERSION, fact, payload)


@dataclass(frozen=True, slots=True)
class IdentityGraph:
    session_id: str
    cursor: int
    event_id: str
    run_id: str | None = None
    turn_id: str | None = None
    round_id: str | None = None
    segment_id: str | None = None
    invocation_id: str | None = None

    @property
    def identity(self) -> str:
        parts = (self.session_id, str(self.cursor), self.event_id, self.run_id,
                 self.turn_id, self.round_id, self.segment_id, self.invocation_id)
        return hashlib.sha256("|".join(str(p or "") for p in parts).encode()).hexdigest()[:32]


def identity_graph(stored: StoredFact) -> IdentityGraph:
    p = stored.fact.payload
    run_id = str(p.get("run_id") or (stored.fact.entity_id if stored.fact.kind is FactKind.RUN else "")) or None
    return IdentityGraph(stored.session_id, stored.cursor, stored.event_id, run_id,
                         _str(p, "turn_id") or (run_id if stored.fact.kind is FactKind.RUN else None),
                         _str(p, "round_id"), _str(p, "segment_id"),
                         _str(p, "invocation_id") or (stored.fact.entity_id if stored.fact.kind in {FactKind.INVOCATION, FactKind.OUTCOME} else None))


@dataclass(frozen=True, slots=True)
class TimelineEntry:
    schema_version: str
    cursor: int
    entry_id: str
    stage: str
    reason: str | None
    status: str
    identities: IdentityGraph
    payload: Mapping[str, Any]
    terminal: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "payload", MappingProxyType(dict(self.payload)))


class TimelineProjector:
    """Project facts into a bounded, idempotently mergeable safe timeline."""

    def __init__(self, *, max_payload_bytes: int = 32 * 1024) -> None:
        if max_payload_bytes <= 0:
            raise ValueError("max_payload_bytes must be positive")
        self.max_payload_bytes = max_payload_bytes

    def project(self, facts: Iterable[StoredFact], *, after_cursor: int = 0,
                secret_values: Iterable[str] = ()) -> tuple[TimelineEntry, ...]:
        terminal_runs: set[str] = set()
        entries: dict[str, TimelineEntry] = {}
        for stored in facts:
            fact = stored.fact
            graph = identity_graph(stored)
            run_id = graph.run_id
            late = bool(run_id and run_id in terminal_runs)
            if stored.cursor <= after_cursor:
                if fact.kind is FactKind.RUN and fact.state in _TERMINAL:
                    terminal_runs.add(fact.entity_id)
                continue
            if late and fact.kind not in {FactKind.OBSERVATION, FactKind.UNRESOLVED_ITEM}:
                payload = {"code": "late_fact_ignored", "summary": "A late fact was ignored after terminal state."}
                entry = self._entry(stored, graph, "terminal", "late_fact_ignored", "warning", payload)
            else:
                entry = self._entry_for_fact(stored, graph, secret_values)
            if entry is not None:
                entries[entry.entry_id] = entry
            if fact.kind is FactKind.RUN and fact.state in _TERMINAL:
                terminal_runs.add(fact.entity_id)
        return tuple(sorted(entries.values(), key=lambda item: (item.cursor, item.entry_id)))

    def merge(self, *batches: Iterable[TimelineEntry]) -> tuple[TimelineEntry, ...]:
        merged: dict[str, TimelineEntry] = {}
        for batch in batches:
            for entry in batch:
                merged.setdefault(entry.entry_id, entry)
        return tuple(sorted(merged.values(), key=lambda item: (item.cursor, item.entry_id)))

    def _entry_for_fact(self, stored, graph, secrets):
        f = stored.fact
        p = versioned_fact(f, secret_values=secrets, max_payload_bytes=self.max_payload_bytes).redacted_payload
        if f.kind is FactKind.COMPACTION:
            return self._entry(stored, graph, "compacting", "context_compaction", f.state, {"summary": "Compacting conversation context."})
        if f.kind is FactKind.PROVIDER_ATTEMPT:
            stage = "provider_retry" if f.state in {"retry", "retrying", "fallback"} or p.get("attempt", 1) != 1 else "provider_attempt"
            return self._entry(stored, graph, stage, stage, f.state, p)
        if f.kind is FactKind.GRACE_SUMMARY:
            return self._entry(stored, graph, "grace_summary", "grace_summary", f.state, p)
        if f.kind is FactKind.FINALIZER:
            return self._entry(stored, graph, "terminal", "finalizer_fallback", f.state, p, terminal=f.state in _TERMINAL)
        if f.kind is FactKind.CHECKPOINT:
            return self._entry(stored, graph, "checkpoint", "checkpoint", f.state, p)
        if f.kind is FactKind.OBSERVATION and f.state == "warning":
            dimension = str(p.get("dimension", ""))
            if dimension == "run_time":
                return self._entry(stored, graph, "run_budget_warning", "run_budget_warning", f.state, {"summary": "Approaching the run time limit."})
            if dimension == "tool_safety":
                return self._entry(stored, graph, "tool_safety_warning", "tool_safety_warning", f.state, {"summary": "Approaching the tool safety limit."})
        if f.kind is FactKind.RUN and f.state in _TERMINAL:
            return self._entry(stored, graph, "terminal", "terminal", f.state, p, terminal=True)
        if f.kind is FactKind.RUN and f.state == RunStatus.RUNNING:
            return self._entry(stored, graph, "run", "started", f.state, p)
        if f.kind is FactKind.TOOL_ROUND:
            return self._entry(stored, graph, "tool_round", "tool_round", f.state, p)
        return self._entry(stored, graph, "event", None, f.state, p)

    def _entry(self, stored, graph, stage, reason, status, payload, terminal=False):
        ident = f"{stored.session_id}:{stored.cursor}:{stored.event_id}:{stage}:{reason or ''}"
        entry_id = "timeline-" + hashlib.sha256(ident.encode()).hexdigest()[:24]
        return TimelineEntry(TIMELINE_SCHEMA_VERSION, stored.cursor, entry_id, stage, reason,
                             str(status), graph, project_mapping(payload), terminal)


def _str(payload: Mapping[str, Any], key: str) -> str | None:
    value = payload.get(key)
    return str(value) if value else None


_UNSAFE = frozenset({"raw_response", "raw_output", "raw", "prompt", "system_prompt",
                     "chain_of_thought", "reasoning", "debug_trace", "traceback"})


def _sanitize(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(k): _sanitize(v) for k, v in value.items()
                if str(k).casefold() not in _UNSAFE}
    if isinstance(value, (list, tuple)):
        return [_sanitize(item) for item in value]
    return value
