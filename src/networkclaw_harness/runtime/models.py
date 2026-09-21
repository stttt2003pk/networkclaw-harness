"""Durable semantic records for multi-session Hermes execution."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from types import MappingProxyType
from typing import Any, Mapping


class RunStatus(StrEnum):
    RUNNING = "running"
    WAITING = "waiting"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    INTERRUPTED = "interrupted"
    BLOCKED = "blocked"
    EXPIRED = "expired"


class InvocationStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCEL_REQUESTED = "cancel_requested"
    CANCELLED = "cancelled"
    UNKNOWN = "unknown"


class InteractionStatus(StrEnum):
    PENDING = "pending"
    ANSWERED = "answered"
    REJECTED = "rejected"
    EXPIRED = "expired"
    SUPERSEDED = "superseded"


class FactKind(StrEnum):
    GOAL = "goal"
    PLAN = "plan"
    RUN = "run"
    INTERACTION = "interaction"
    INVOCATION = "invocation"
    OUTCOME = "outcome"
    ARTIFACT_REF = "artifact_ref"
    UNRESOLVED_ITEM = "unresolved_item"
    APPROVAL = "approval"
    OBSERVATION = "observation"
    DELIVERY = "delivery"
    COMPACTION = "compaction"
    MEMORY = "memory"
    SKILL = "skill"
    TOOL_ROUND = "tool_round"
    PROVIDER_ATTEMPT = "provider_attempt"
    CHECKPOINT = "checkpoint"
    GRACE_SUMMARY = "grace_summary"
    FINALIZER = "finalizer"


@dataclass(frozen=True, slots=True)
class SemanticFact:
    """One immutable Lobby-owned fact; payload must be JSON-compatible."""

    kind: FactKind
    entity_id: str
    state: str
    payload: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.entity_id or not self.entity_id.isascii() or not self.state:
            raise ValueError("semantic fact requires an ASCII entity_id and non-empty state")
        object.__setattr__(self, "payload", MappingProxyType(dict(self.payload)))


@dataclass(frozen=True, slots=True)
class StoredFact:
    session_id: str
    event_id: str
    cursor: int
    fact: SemanticFact


@dataclass(frozen=True, slots=True)
class DurableAck:
    session_id: str
    event_id: str
    cursor: int
    facts: tuple[StoredFact, ...]
    deduplicated: bool = False


@dataclass(frozen=True, slots=True)
class Outcome:
    """Execution and business meaning are deliberately independent."""

    invocation_id: str
    execution_status: InvocationStatus
    business_success: bool | None
    result: Mapping[str, Any] = field(default_factory=dict)
    evidence: tuple[str, ...] = ()
    unresolved: tuple[str, ...] = ()
    effects: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "result", MappingProxyType(dict(self.result)))
