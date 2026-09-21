"""Shared run budgets and deterministic no-progress detection."""

from __future__ import annotations

import hashlib
import json
import threading
import time
import sys
from enum import StrEnum
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping


class BudgetExceeded(RuntimeError):
    def __init__(self, dimension: str) -> None:
        super().__init__(f"{dimension} budget exhausted")
        self.dimension = dimension


class IterationReceiptState(StrEnum):
    RESERVED = "reserved"
    REQUEST_EMITTED = "request_emitted"
    TOKEN_EMITTED = "token_emitted"
    REFUNDED = "refunded"
    COMMITTED = "committed"


class IterationReceipt:
    """One authoritative decision about whether a provider turn is chargeable."""

    def __init__(self, budget: "RunBudget", iteration: int) -> None:
        self._budget, self.iteration = budget, iteration
        self.state = IterationReceiptState.RESERVED

    def request_emitted(self) -> None:
        if self.state is IterationReceiptState.RESERVED:
            self.state = IterationReceiptState.REQUEST_EMITTED

    def token_emitted(self) -> None:
        if self.state in {IterationReceiptState.RESERVED, IterationReceiptState.REQUEST_EMITTED}:
            self.state = IterationReceiptState.TOKEN_EMITTED

    def refund(self) -> bool:
        if self.state is not IterationReceiptState.RESERVED:
            return False
        self._budget.refund_iteration()
        self.state = IterationReceiptState.REFUNDED
        return True

    def commit(self) -> None:
        if self.state is not IterationReceiptState.REFUNDED:
            self.state = IterationReceiptState.COMMITTED


@dataclass(frozen=True, slots=True)
class BudgetLimits:
    max_steps: int = 24
    max_retries: int = 4
    max_actions: int = 64
    max_output_bytes: int = 65_536
    timeout_seconds: float = 120.0
    iteration_warning_ratio: float = 0.8
    run_warning_ratio: float = 0.8
    action_warning_ratio: float = 0.8

    def __post_init__(self) -> None:
        if min(self.max_steps, self.max_retries + 1, self.max_actions, self.max_output_bytes) <= 0:
            raise ValueError("budget limits must be positive")
        if self.max_retries < 0 or self.timeout_seconds <= 0:
            raise ValueError("retry and timeout budgets must be non-negative/positive")
        for ratio in (self.iteration_warning_ratio, self.run_warning_ratio, self.action_warning_ratio):
            if not 0 < ratio < 1:
                raise ValueError("budget warning ratios must be between zero and one")


class RunBudget:
    """One non-resettable budget shared by the parent and all child work."""

    def __init__(self, limits: BudgetLimits) -> None:
        self.limits = limits
        self.started_at = time.monotonic()
        self._iterations = _hermes_iteration_budget(limits.max_steps)
        self.retries = 0
        self.actions = 0
        self.output_bytes = 0
        self.model_proposals = 0
        self.policy_denials = 0
        self.tool_executions = 0
        self.unknown_effects = 0
        self._run_warning_emitted = False
        self._iteration_warning_emitted = False
        self._action_warning_emitted = False
        self._lock = threading.Lock()

    def consume(self, dimension: str, amount: int = 1) -> None:
        with self._lock:
            if time.monotonic() - self.started_at > self.limits.timeout_seconds:
                raise BudgetExceeded("time")
            if dimension == "steps":
                if amount != 1:
                    raise ValueError("Hermes iteration budget is consumed one provider turn at a time")
                if not self._iterations.consume():
                    raise BudgetExceeded("steps")
                return
            current = getattr(self, dimension)
            maximum = getattr(self.limits, f"max_{dimension}")
            if current + amount > maximum:
                raise BudgetExceeded(dimension)
            setattr(self, dimension, current + amount)

    def record_proposal(self, count: int = 1) -> None:
        if count < 0:
            raise ValueError("proposal count must be non-negative")
        with self._lock:
            self.model_proposals += count

    def record_policy_denial(self, count: int = 1) -> None:
        if count < 0:
            raise ValueError("denial count must be non-negative")
        with self._lock:
            self.policy_denials += count

    def record_execution(self, *, unknown_effect: bool = False) -> None:
        with self._lock:
            self.tool_executions += 1
            if unknown_effect:
                self.unknown_effects += 1

    @property
    def steps(self) -> int:
        return int(self._iterations.used)

    @property
    def remaining_steps(self) -> int:
        return int(self._iterations.remaining)

    def refund_iteration(self) -> None:
        with self._lock:
            self._iterations.refund()

    def begin_provider_turn(self) -> IterationReceipt:
        """Reserve exactly one iteration at provider-turn start."""
        self.consume("steps")
        return IterationReceipt(self, self.steps)

    def check_time(self) -> None:
        with self._lock:
            if time.monotonic() - self.started_at > self.limits.timeout_seconds:
                raise BudgetExceeded("time")

    @property
    def elapsed_seconds(self) -> float:
        return max(0.0, time.monotonic() - self.started_at)

    def warning_dimensions(self) -> tuple[str, ...]:
        """Return one-shot Hermes-style soft warnings without consuming budget."""
        with self._lock:
            warnings: list[str] = []
            if self.steps / self.limits.max_steps >= self.limits.iteration_warning_ratio and not self._iteration_warning_emitted:
                self._iteration_warning_emitted = True; warnings.append("iterations")
            if self.actions / self.limits.max_actions >= self.limits.action_warning_ratio and not self._action_warning_emitted:
                self._action_warning_emitted = True; warnings.append("tool_safety")
            if self.elapsed_seconds / self.limits.timeout_seconds >= self.limits.run_warning_ratio and not self._run_warning_emitted:
                self._run_warning_emitted = True; warnings.append("run_time")
            return tuple(warnings)

    def can_consume_actions(self, amount: int) -> bool:
        with self._lock:
            return self.actions + amount <= self.limits.max_actions

    def consume_actions(self, amount: int) -> None:
        if amount < 0:
            raise ValueError("action amount must be non-negative")
        with self._lock:
            if time.monotonic() - self.started_at > self.limits.timeout_seconds:
                raise BudgetExceeded("time")
            if self.actions + amount > self.limits.max_actions:
                raise BudgetExceeded("tool_safety")
            self.actions += amount


def action_fingerprint(name: str, arguments: Mapping[str, Any], outcome: str | None = None) -> str:
    payload = {"name": name, "arguments": dict(arguments), "outcome": outcome}
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True, default=str)
    return hashlib.sha256(encoded.encode()).hexdigest()


def _hermes_iteration_budget(max_total: int):
    """Load the counter from the pinned vendor snapshot, never from a global install."""
    vendor = Path(__file__).resolve().parents[3] / "vendor" / "hermes"
    vendor_text = str(vendor)
    if vendor_text not in sys.path:
        sys.path.insert(0, vendor_text)
    try:
        from agent.iteration_budget import IterationBudget  # type: ignore
    except Exception as error:
        raise RuntimeError("pinned Hermes iteration runtime is unavailable") from error
    return IterationBudget(max_total)


class AgentIterationBudget:
    """Pinned-Hermes iteration counter for one parent or subagent.

    This counter is intentionally separate from :class:`RunBudget`: iteration
    ceilings are per agent, while wall time, tool safety and output remain
    shared run-level safety limits.
    """

    def __init__(self, max_iterations: int) -> None:
        if max_iterations <= 0:
            raise ValueError("agent iteration budget must be positive")
        self._counter = _hermes_iteration_budget(max_iterations)

    def consume(self) -> bool:
        return bool(self._counter.consume())

    def refund(self) -> None:
        self._counter.refund()

    @property
    def used(self) -> int:
        return int(self._counter.used)

    @property
    def remaining(self) -> int:
        return int(self._counter.remaining)

    @property
    def max_total(self) -> int:
        return int(self._counter.max_total)


class ProgressTracker:
    def __init__(self, *, repeat_limit: int = 2) -> None:
        if repeat_limit < 2:
            raise ValueError("repeat_limit must be at least two")
        self._repeat_limit = repeat_limit
        self._fingerprints: dict[str, int] = {}
        self._alternatives: list[str] = []

    def observe(self, fingerprint: str) -> bool:
        count = self._fingerprints.get(fingerprint, 0) + 1
        self._fingerprints[fingerprint] = count
        return count >= self._repeat_limit

    def select_alternative(self, alternative_id: str) -> None:
        if not alternative_id or alternative_id in self._alternatives:
            raise ValueError("alternative must be new and non-empty")
        self._alternatives.append(alternative_id)

    @property
    def alternatives(self) -> tuple[str, ...]:
        return tuple(self._alternatives)
