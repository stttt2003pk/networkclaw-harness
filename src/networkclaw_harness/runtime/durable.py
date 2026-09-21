"""Lobby durable fact port and a transactional in-memory reference store."""

from __future__ import annotations

import threading
from typing import Protocol, Sequence

from .models import DurableAck, SemanticFact, StoredFact


class DurableWriteError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class DurableSessionPort(Protocol):
    def commit(self, *, session_id: str, event_id: str,
               facts: Sequence[SemanticFact]) -> DurableAck: ...
    def load(self, session_id: str, *, after_cursor: int = 0) -> tuple[StoredFact, ...]: ...
    def cursor(self, session_id: str) -> int: ...


class ReferenceDurableSessionStore:
    """Atomic event batches, cursor allocation, and event-id deduplication."""

    def __init__(self) -> None:
        self._facts: dict[str, list[StoredFact]] = {}
        self._events: dict[tuple[str, str], DurableAck] = {}
        self._failures: list[Exception] = []
        self._lock = threading.RLock()

    def fail_next_commit(self, error: Exception | None = None) -> None:
        with self._lock:
            self._failures.append(error or DurableWriteError("durable_unavailable", "durable commit failed"))

    def commit(self, *, session_id: str, event_id: str,
               facts: Sequence[SemanticFact]) -> DurableAck:
        if not session_id or not session_id.isascii() or not event_id or not event_id.isascii():
            raise DurableWriteError("invalid_identity", "session_id and event_id must be non-empty ASCII strings")
        batch = tuple(facts)
        if not batch:
            raise DurableWriteError("empty_commit", "durable fact batch cannot be empty")
        with self._lock:
            existing = self._events.get((session_id, event_id))
            if existing is not None:
                if tuple(item.fact for item in existing.facts) != batch:
                    raise DurableWriteError("idempotency_conflict", "event_id was already committed with different facts")
                return DurableAck(session_id, event_id, existing.cursor, existing.facts, deduplicated=True)
            if self._failures:
                raise self._failures.pop(0)
            records = self._facts.setdefault(session_id, [])
            start = records[-1].cursor if records else 0
            pending = tuple(
                StoredFact(session_id, event_id, start + index, fact)
                for index, fact in enumerate(batch, start=1)
            )
            records.extend(pending)
            ack = DurableAck(session_id, event_id, pending[-1].cursor, pending)
            self._events[(session_id, event_id)] = ack
            return ack

    def load(self, session_id: str, *, after_cursor: int = 0) -> tuple[StoredFact, ...]:
        with self._lock:
            return tuple(item for item in self._facts.get(session_id, ()) if item.cursor > after_cursor)

    def cursor(self, session_id: str) -> int:
        with self._lock:
            records = self._facts.get(session_id, ())
            return records[-1].cursor if records else 0
