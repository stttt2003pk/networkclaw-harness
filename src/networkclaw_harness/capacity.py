"""Bounded resource accounting and capacity admission for one Harness process."""

from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Mapping


class CapacityError(RuntimeError):
    def __init__(self, code: str, message: str, *, dimension: str) -> None:
        super().__init__(message)
        self.code = code
        self.dimension = dimension


@dataclass(frozen=True, slots=True)
class CapacityLimits:
    concurrent_sessions: int = 128
    concurrent_runs: int = 16
    concurrent_tools: int = 32
    concurrent_subagents: int = 16
    token_budget: int = 1_000_000
    queue_depth: int = 128
    cpu_cores: float = 2.0
    memory_bytes: int = 2 * 1024**3
    open_files: int = 512
    subprocesses: int = 16
    browser_contexts: int = 2
    mcp_servers: int = 8
    idle_reclaim_seconds: int = 900
    lazy_start: bool = True
    lazy_provider: bool = True
    lazy_browser: bool = True
    lazy_mcp: bool = True

    def __post_init__(self) -> None:
        if min(self.concurrent_sessions, self.concurrent_runs, self.concurrent_tools,
               self.concurrent_subagents, self.token_budget, self.queue_depth,
               self.memory_bytes, self.open_files, self.subprocesses,
               self.browser_contexts, self.mcp_servers, self.idle_reclaim_seconds) <= 0:
            raise ValueError("capacity limits must be positive")
        if self.cpu_cores <= 0:
            raise ValueError("CPU capacity must be positive")


class CapacityLedger:
    def __init__(self, limits: CapacityLimits = CapacityLimits()) -> None:
        self.limits = limits
        self._usage = {name: 0 for name in (
            "concurrent_sessions", "concurrent_runs", "concurrent_tools",
            "concurrent_subagents", "token_budget", "queue_depth",
        )}
        self._lock = threading.RLock()

    def reserve(self, dimension: str, amount: int = 1) -> None:
        with self._lock:
            limit = getattr(self.limits, dimension, None)
            if limit is None or amount <= 0:
                raise ValueError("unknown capacity dimension or amount")
            if self._usage[dimension] + amount > limit:
                raise CapacityError("resource_exhausted", f"{dimension} capacity is exhausted", dimension=dimension)
            self._usage[dimension] += amount

    def release(self, dimension: str, amount: int = 1) -> None:
        with self._lock:
            if dimension not in self._usage or amount <= 0:
                raise ValueError("unknown capacity dimension or amount")
            self._usage[dimension] = max(0, self._usage[dimension] - amount)

    def usage(self) -> Mapping[str, int]:
        with self._lock:
            return dict(self._usage)

    def reserve_run(self) -> None: self.reserve("concurrent_runs")
    def release_run(self) -> None: self.release("concurrent_runs")
    def reserve_tool(self) -> None: self.reserve("concurrent_tools")
    def release_tool(self) -> None: self.release("concurrent_tools")
    def reserve_subagent(self) -> None: self.reserve("concurrent_subagents")
    def release_subagent(self) -> None: self.release("concurrent_subagents")

    def reserve_tokens(self, amount: int) -> None: self.reserve("token_budget", amount)
