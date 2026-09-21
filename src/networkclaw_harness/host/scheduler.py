"""Bounded fair admission queues for protocol-visible session work."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from typing import Any

from networkclaw_harness.protocol import ProtocolError


@dataclass(frozen=True, slots=True)
class ScheduledCommand:
    session_id: str
    request_id: str
    value: Any


class AdmissionScheduler:
    """Prioritize controls and round-robin normal work across sessions."""

    def __init__(self, *, per_session_limit: int = 64) -> None:
        self._limit = per_session_limit
        self._normal: dict[str, deque[ScheduledCommand]] = {}
        self._ready_sessions: deque[str] = deque()
        self._controls: deque[ScheduledCommand] = deque()

    def submit(self, command: ScheduledCommand, *, control: bool = False) -> int:
        if control:
            self._controls.append(command)
            return 0
        queue = self._normal.setdefault(command.session_id, deque())
        if len(queue) >= self._limit:
            raise ProtocolError("resource_exhausted", "session input queue is full")
        was_empty = not queue
        queue.append(command)
        if was_empty:
            self._ready_sessions.append(command.session_id)
        return len(queue)

    def next(self) -> ScheduledCommand | None:
        if self._controls:
            return self._controls.popleft()
        if not self._ready_sessions:
            return None
        session_id = self._ready_sessions.popleft()
        queue = self._normal[session_id]
        command = queue.popleft()
        if queue:
            self._ready_sessions.append(session_id)
        else:
            del self._normal[session_id]
        return command

    @property
    def depth(self) -> int:
        return len(self._controls) + sum(len(queue) for queue in self._normal.values())
