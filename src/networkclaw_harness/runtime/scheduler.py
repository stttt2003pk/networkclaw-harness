"""Test/injected per-session admission fixture, not a production platform scheduler.

Production routing and placement belong to the external host.  This module remains
available for behavioral tests that need to model bounded per-session admission.
"""

from __future__ import annotations

import threading
from collections import deque
from dataclasses import dataclass
from typing import Any


class SchedulingError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class ScheduledInput:
    session_id: str
    item_id: str
    payload: Any
    control: bool = False


class FairSessionScheduler:
    """Behavioral fixture for local admission tests; never selected by the launcher."""
    def __init__(self, *, max_active_runs: int, max_queued_per_session: int = 128) -> None:
        if max_active_runs <= 0 or max_queued_per_session <= 0:
            raise ValueError("scheduler limits must be positive")
        self._limit = max_active_runs
        self._queue_limit = max_queued_per_session
        self._normal: dict[str, deque[ScheduledInput]] = {}
        self._control: dict[str, deque[ScheduledInput]] = {}
        self._ready: deque[str] = deque()
        self._ready_set: set[str] = set()
        self._active: dict[str, ScheduledInput] = {}
        self._lock = threading.RLock()

    def enqueue(self, item: ScheduledInput) -> None:
        with self._lock:
            queues = self._control if item.control else self._normal
            queue = queues.setdefault(item.session_id, deque())
            total = len(queue) + len((self._normal if item.control else self._control).get(item.session_id, ()))
            if total >= self._queue_limit:
                raise SchedulingError("session_queue_full", "session input queue capacity is exhausted")
            queue.append(item)
            if not item.control and item.session_id not in self._active:
                self._mark_ready(item.session_id)

    def take_control(self, session_id: str) -> ScheduledInput | None:
        """Controls can be consumed while a normal run is active."""
        with self._lock:
            queue = self._control.get(session_id)
            return queue.popleft() if queue else None

    def dispatch(self) -> ScheduledInput | None:
        with self._lock:
            if len(self._active) >= self._limit:
                return None
            while self._ready:
                session_id = self._ready.popleft()
                self._ready_set.discard(session_id)
                if session_id in self._active:
                    continue
                queue = self._normal.get(session_id)
                if not queue:
                    continue
                item = queue.popleft()
                self._active[session_id] = item
                return item
            return None

    def complete(self, session_id: str, item_id: str) -> None:
        with self._lock:
            current = self._active.get(session_id)
            if current is None or current.item_id != item_id:
                raise SchedulingError("active_item_mismatch", "completed item is not active for the session")
            del self._active[session_id]
            if self._normal.get(session_id):
                self._mark_ready(session_id)

    def close_session(self, session_id: str) -> None:
        with self._lock:
            if session_id in self._active:
                raise SchedulingError("session_busy", "cannot remove a session with an active run")
            self._normal.pop(session_id, None)
            self._control.pop(session_id, None)
            self._ready = deque(item for item in self._ready if item != session_id)
            self._ready_set.discard(session_id)

    def _mark_ready(self, session_id: str) -> None:
        if session_id not in self._ready_set:
            self._ready.append(session_id)
            self._ready_set.add(session_id)
