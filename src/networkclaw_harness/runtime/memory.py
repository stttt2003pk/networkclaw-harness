"""Policy-bound, provenance-preserving memory retrieval with no runtime mutation path."""

from __future__ import annotations

import threading
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol


class MemoryError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class MemoryScope(StrEnum):
    SESSION = "session"
    USER_PREFERENCE = "user_preference"
    CROSS_SESSION = "cross_session"


@dataclass(frozen=True, slots=True)
class MemoryPolicy:
    user_preferences_enabled: bool = True
    cross_session_enabled: bool = False


@dataclass(frozen=True, slots=True)
class MemoryRecord:
    memory_id: str
    tenant_id: str
    user_id: str
    session_id: str | None
    scope: MemoryScope
    text: str
    source_refs: tuple[str, ...]
    kind: str = "fact"

    def __post_init__(self) -> None:
        if any(not value or not value.isascii() for value in (self.memory_id, self.tenant_id, self.user_id)):
            raise ValueError("memory identities must be non-empty ASCII strings")
        if self.scope is MemoryScope.SESSION and not self.session_id:
            raise ValueError("session memory requires its session_id")
        if not self.source_refs:
            raise ValueError("memory requires source references")
        if self.kind in {"skill_source", "tool_source", "dependency", "installation"}:
            raise MemoryError("memory_mutation_forbidden", "memory cannot store or install mutable runtime sources")


class MemoryPort(Protocol):
    def record(self, value: MemoryRecord) -> None: ...
    def search(self, *, tenant_id: str, user_id: str, session_id: str,
               query: str, policy: MemoryPolicy) -> tuple[MemoryRecord, ...]: ...


class ReferenceMemoryStore:
    """A small behavioral retrieval store; production uses a tenant-scoped durable index."""

    def __init__(self) -> None:
        self._records: dict[str, MemoryRecord] = {}
        self._lock = threading.RLock()

    def record(self, value: MemoryRecord) -> None:
        with self._lock:
            current = self._records.get(value.memory_id)
            if current is not None and current != value:
                raise MemoryError("memory_id_conflict", "memory id already names different data")
            self._records[value.memory_id] = value

    def search(self, *, tenant_id: str, user_id: str, session_id: str,
               query: str, policy: MemoryPolicy) -> tuple[MemoryRecord, ...]:
        terms = {term.casefold() for term in query.split() if term}
        with self._lock:
            matches = []
            for record in self._records.values():
                if record.tenant_id != tenant_id or record.user_id != user_id:
                    continue
                if record.scope is MemoryScope.SESSION and record.session_id != session_id:
                    continue
                if record.scope is MemoryScope.USER_PREFERENCE and not policy.user_preferences_enabled:
                    continue
                if record.scope is MemoryScope.CROSS_SESSION and not policy.cross_session_enabled:
                    continue
                text_terms = set(record.text.casefold().split())
                if not terms or terms & text_terms:
                    matches.append(record)
            return tuple(sorted(matches, key=lambda item: item.memory_id))
