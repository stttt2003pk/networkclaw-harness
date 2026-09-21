"""Partitioned multi-session registry with epoch-backed ownership."""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from networkclaw_harness.workspace import (
    FenceToken,
    LeasePolicy,
    LeaseRecord,
    ReferenceLeaseAuthority,
    SessionWorkspace,
)


class SessionRuntimeError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(slots=True)
class SessionState:
    tenant_id: str
    session_id: str
    owner_id: str
    workspace: SessionWorkspace
    lease: LeaseRecord
    idempotency_key: str
    active_run_id: str | None = None
    events: list[str] = field(default_factory=list)
    budgets: dict[str, int] = field(default_factory=dict)
    approvals: dict[str, str] = field(default_factory=dict)
    cache: dict[str, Any] = field(default_factory=dict)

    @property
    def token(self) -> FenceToken:
        return FenceToken(
            self.session_id, self.owner_id, self.lease.execution_epoch,
            self.lease.lease_id, self.lease.lease_version,
        )


class SessionRegistry:
    """No implicit current session: every lookup and mutation is keyed by session_id."""

    def __init__(self, authority: ReferenceLeaseAuthority, *, capacity: int,
                 lease_policy: LeasePolicy | None = None) -> None:
        if capacity <= 0:
            raise ValueError("session capacity must be positive")
        self._authority = authority
        self._capacity = capacity
        self._policy = lease_policy or LeasePolicy(ttl_ms=60_000, renew_interval_ms=20_000, grace_ms=5_000)
        self._sessions: dict[str, SessionState] = {}
        self._closed_keys: dict[tuple[str, str], SessionState] = {}
        self._lock = threading.RLock()

    def open(self, *, tenant_id: str, session_id: str, owner_id: str,
             workspace_root: str | Path, idempotency_key: str,
             expected_lease_version: int = 0, expected_execution_epoch: int = 0) -> SessionState:
        if not idempotency_key or not idempotency_key.isascii():
            raise SessionRuntimeError("invalid_idempotency_key", "session open requires an ASCII idempotency key")
        with self._lock:
            current = self._sessions.get(session_id)
            if current is not None:
                self._validate_same_open(current, tenant_id, owner_id, workspace_root)
                if current.idempotency_key != idempotency_key:
                    raise SessionRuntimeError(
                        "session_open_conflict", "session is already open under another idempotency key"
                    )
                return current
            if len(self._sessions) >= self._capacity:
                raise SessionRuntimeError("session_capacity_exceeded", "Harness session capacity is exhausted")
            try:
                lease = self._authority.acquire(
                    session_id=session_id, owner_id=owner_id, policy=self._policy,
                    expected_lease_version=expected_lease_version,
                    expected_execution_epoch=expected_execution_epoch,
                )
            except Exception as error:
                code = getattr(error, "code", "ownership_conflict")
                raise SessionRuntimeError(code, str(error)) from error
            workspace = SessionWorkspace.open(workspace_root, tenant_id=tenant_id, session_id=session_id)
            state = SessionState(tenant_id, session_id, owner_id, workspace, lease, idempotency_key)
            self._sessions[session_id] = state
            return state

    def resume(self, *, tenant_id: str, session_id: str, owner_id: str,
               workspace_root: str | Path, idempotency_key: str,
               expected_lease_version: int, expected_execution_epoch: int) -> SessionState:
        return self.open(
            tenant_id=tenant_id, session_id=session_id, owner_id=owner_id,
            workspace_root=workspace_root, idempotency_key=idempotency_key,
            expected_lease_version=expected_lease_version,
            expected_execution_epoch=expected_execution_epoch,
        )

    def close(self, session_id: str, *, reason: str = "closed") -> SessionState:
        with self._lock:
            state = self.require(session_id)
            if state.active_run_id is not None:
                raise SessionRuntimeError("session_busy", "cannot close a session with an active run")
            invalid = self._authority.invalidate(state.token)
            state.lease = invalid
            state.events.append(f"closed:{reason}")
            del self._sessions[session_id]
            self._closed_keys[(session_id, state.idempotency_key)] = state
            return state

    def require(self, session_id: str) -> SessionState:
        with self._lock:
            try:
                return self._sessions[session_id]
            except KeyError as error:
                raise SessionRuntimeError("session_not_open", "session is not open in this Harness") from error

    def begin_run(self, session_id: str, run_id: str) -> None:
        with self._lock:
            state = self.require(session_id)
            if state.active_run_id is not None:
                raise SessionRuntimeError("run_already_active", "session already has an active Hermes run")
            state.active_run_id = run_id

    def end_run(self, session_id: str, run_id: str) -> None:
        with self._lock:
            state = self.require(session_id)
            if state.active_run_id != run_id:
                raise SessionRuntimeError("run_identity_mismatch", "run is not active for this session")
            state.active_run_id = None

    def list_open(self) -> tuple[str, ...]:
        with self._lock:
            return tuple(self._sessions)

    @staticmethod
    def _validate_same_open(current: SessionState, tenant_id: str, owner_id: str,
                            workspace_root: str | Path) -> None:
        if current.owner_id != owner_id:
            raise SessionRuntimeError("session_owned", "session is already open under another owner")
        if current.tenant_id != tenant_id or current.workspace.root != Path(workspace_root).resolve():
            raise SessionRuntimeError("session_identity_conflict", "session open conflicts with its bound identity")
