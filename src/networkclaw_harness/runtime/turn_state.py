"""Replay-safe, public turn state.

This module intentionally contains only public turn facts. Provider prompts,
reasoning, credentials and raw payloads must never be put in a turn state.
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from enum import StrEnum
from typing import Any, Mapping


class TurnPhase(StrEnum):
    PREFLIGHT = "preflight"
    ROUTING = "routing"
    REQUEST = "request"
    TOOL = "tool"
    COMPRESSION = "compression"
    CHECKPOINT = "checkpoint"
    FINALIZE = "finalize"
    EXIT = "exit"


class TurnExitReason(StrEnum):
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    INTERRUPTED = "interrupted"
    STEERED = "steered"
    BUDGET = "budget"
    RETRY_EXHAUSTED = "retry_exhausted"
    UNKNOWN_EFFECT = "unknown_effect"
    BLOCKED = "blocked"


@dataclass(frozen=True, slots=True)
class RetryState:
    attempts: int = 0
    restart_count: int = 0
    last_error: str | None = None
    next_action: str | None = None


@dataclass(frozen=True, slots=True)
class TurnState:
    run_id: str
    turn_id: str
    iteration: int
    phase: TurnPhase = TurnPhase.PREFLIGHT
    attempt: int = 0
    retry: RetryState = field(default_factory=RetryState)
    compression: str | None = None
    interrupt: str | None = None
    exit_reason: TurnExitReason | None = None
    checkpointed: bool = False
    counters: Mapping[str, int] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.run_id or not self.turn_id or self.iteration < 1:
            raise ValueError("turn identity and positive iteration are required")
        object.__setattr__(self, "counters", {str(k): int(v) for k, v in self.counters.items() if int(v) >= 0})

    def with_phase(self, phase: TurnPhase, **changes: Any) -> "TurnState":
        values = {
            "run_id": self.run_id, "turn_id": self.turn_id, "iteration": self.iteration,
            "phase": phase, "attempt": self.attempt, "retry": self.retry,
            "compression": self.compression, "interrupt": self.interrupt,
            "exit_reason": self.exit_reason, "checkpointed": self.checkpointed,
            "counters": self.counters,
        }
        values.update(changes)
        return TurnState(**values)

    def public_dict(self) -> dict[str, Any]:
        """Return bounded JSON-compatible state suitable for durable replay."""
        value = asdict(self)
        value["phase"] = self.phase.value
        value["exit_reason"] = self.exit_reason.value if self.exit_reason else None
        return value


__all__ = ["RetryState", "TurnExitReason", "TurnPhase", "TurnState"]
