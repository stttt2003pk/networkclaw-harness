"""Workspace-backed durable facts for the development host."""
from __future__ import annotations

import json
import sqlite3
import threading
from typing import Sequence

from networkclaw_harness.workspace import SessionWorkspace
from .durable import DurableWriteError
from .models import DurableAck, FactKind, SemanticFact, StoredFact


class WorkspaceDurableSessionStore:
    """One durable semantic-fact log per assigned session workspace."""

    def __init__(self, workspace: SessionWorkspace) -> None:
        self.workspace = workspace
        self.path = workspace.path_for("session-state/durable.db")
        self._lock = threading.RLock()
        # Hermes may dispatch a tool on a worker thread. All access is serialized by
        # ``_lock``, so the connection is deliberately shareable across those threads.
        self._db = sqlite3.connect(
            self.path, timeout=30, isolation_level=None, check_same_thread=False,
        )
        self._db.row_factory = sqlite3.Row
        with self._lock:
            self._db.execute("PRAGMA foreign_keys=ON")
            self._db.execute("PRAGMA busy_timeout=30000")
            self._db.executescript(
                """CREATE TABLE IF NOT EXISTS events (
                    session_id TEXT NOT NULL, event_id TEXT NOT NULL, fact_count INTEGER NOT NULL,
                    terminal_cursor INTEGER NOT NULL, PRIMARY KEY (session_id, event_id)
                );
                CREATE TABLE IF NOT EXISTS facts (
                    session_id TEXT NOT NULL, event_id TEXT NOT NULL, cursor INTEGER NOT NULL,
                    fact_index INTEGER NOT NULL,
                    kind TEXT NOT NULL, entity_id TEXT NOT NULL, state TEXT NOT NULL, payload TEXT NOT NULL,
                    PRIMARY KEY (session_id, cursor), UNIQUE (session_id, event_id, fact_index)
                );
                CREATE INDEX IF NOT EXISTS facts_session_cursor ON facts(session_id, cursor);"""
            )

    def close(self) -> None:
        with self._lock:
            self._db.close()

    def commit(self, *, session_id: str, event_id: str,
               facts: Sequence[SemanticFact]) -> DurableAck:
        if session_id != self.workspace.session_id or not event_id or not event_id.isascii():
            raise DurableWriteError("invalid_identity", "durable write identity does not match workspace")
        batch = tuple(facts)
        if not batch:
            raise DurableWriteError("empty_commit", "durable fact batch cannot be empty")
        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM facts WHERE session_id = ? AND event_id = ? ORDER BY cursor", (session_id, event_id)
            ).fetchall()
            if rows:
                current = tuple(self._row_fact(row) for row in rows)
                if tuple(item.fact for item in current) != batch:
                    raise DurableWriteError("idempotency_conflict", "event_id was already committed with different facts")
                return DurableAck(session_id, event_id, current[-1].cursor, current, deduplicated=True)
            self._db.execute("BEGIN IMMEDIATE")
            try:
                cursor = int(self._db.execute(
                    "SELECT COALESCE(MAX(cursor), 0) FROM facts WHERE session_id = ?", (session_id,)
                ).fetchone()[0])
                stored = []
                for fact_index, fact in enumerate(batch):
                    cursor += 1
                    self._db.execute(
                        "INSERT INTO facts(session_id,event_id,cursor,fact_index,kind,entity_id,state,payload) VALUES(?,?,?,?,?,?,?,?)",
                        (session_id, event_id, cursor, fact_index, str(fact.kind), fact.entity_id, fact.state,
                         json.dumps(dict(fact.payload), sort_keys=True, separators=(",", ":"))),
                    )
                    stored.append(StoredFact(session_id, event_id, cursor, fact))
                self._db.execute(
                    "INSERT INTO events(session_id,event_id,fact_count,terminal_cursor) VALUES(?,?,?,?)",
                    (session_id, event_id, len(batch), cursor),
                )
                self._db.commit()
            except Exception:
                self._db.rollback()
                raise
            return DurableAck(session_id, event_id, cursor, tuple(stored))

    def load(self, session_id: str, *, after_cursor: int = 0) -> tuple[StoredFact, ...]:
        if session_id != self.workspace.session_id:
            return ()
        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM facts WHERE session_id = ? AND cursor > ? ORDER BY cursor", (session_id, after_cursor)
            ).fetchall()
            return tuple(self._row_fact(row) for row in rows)

    def cursor(self, session_id: str) -> int:
        if session_id != self.workspace.session_id:
            return 0
        with self._lock:
            return int(self._db.execute(
                "SELECT COALESCE(MAX(cursor), 0) FROM facts WHERE session_id = ?", (session_id,)
            ).fetchone()[0])

    @staticmethod
    def _row_fact(row: sqlite3.Row) -> StoredFact:
        fact = SemanticFact(FactKind(str(row["kind"])), str(row["entity_id"]), str(row["state"]), json.loads(row["payload"]))
        return StoredFact(str(row["session_id"]), str(row["event_id"]), int(row["cursor"]), fact)
