"""Host-authorized child allocation for native Hermes delegation."""

from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping


class DelegationError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class DelegationGrant:
    allocation_id: str
    child_session_id: str
    workspace_root: Path
    lease: Mapping[str, Any]
    max_iterations: int
    timeout_seconds: float
    max_output_bytes: int

    def hermes_mapping(self) -> dict[str, Any]:
        return {
            "allocation_id": self.allocation_id,
            "session_id": self.child_session_id,
            "cwd": str(self.workspace_root),
            "lease": dict(self.lease),
            "max_iterations": self.max_iterations,
            "timeout_seconds": self.timeout_seconds,
            "max_output_bytes": self.max_output_bytes,
        }


@dataclass(slots=True)
class _PendingAllocation:
    allocation_id: str
    event: threading.Event = field(default_factory=threading.Event)
    grant: DelegationGrant | None = None
    error: DelegationError | None = None


class HostDelegationBroker:
    """Block child construction until the host grants bounded resources."""

    def __init__(self, *, session_id: str, emit: Callable[[str, Mapping[str, Any]], None],
                 timeout_seconds: float = 30.0) -> None:
        self._session_id = session_id
        self._emit = emit
        self._timeout_seconds = timeout_seconds
        self._pending: dict[str, _PendingAllocation] = {}
        self._lock = threading.RLock()
        self._closed = False

    def request(self, *, task_index: int, task_count: int, depth: int,
                requested_iterations: int) -> DelegationGrant:
        allocation_id = f"allocation-{uuid.uuid4().hex}"
        pending = _PendingAllocation(allocation_id)
        with self._lock:
            if self._closed:
                raise DelegationError("delegation_broker_closed", "delegation broker is closed")
            self._pending[allocation_id] = pending
        self._emit("delegation.requested", {
            "allocation_id": allocation_id,
            "parent_session_id": self._session_id,
            "task_index": task_index,
            "task_count": task_count,
            "depth": depth,
            "requested_budget": {"max_iterations": max(1, requested_iterations)},
        })
        deadline = time.monotonic() + self._timeout_seconds
        try:
            while not pending.event.wait(timeout=min(0.1, max(0.0, deadline - time.monotonic()))):
                if time.monotonic() >= deadline:
                    raise DelegationError(
                        "delegation_allocation_timeout",
                        "host did not resolve the child allocation before the deadline",
                    )
            if pending.error is not None:
                raise pending.error
            if pending.grant is None:
                raise DelegationError("delegation_allocation_invalid", "host returned no child grant")
            return pending.grant
        finally:
            with self._lock:
                self._pending.pop(allocation_id, None)

    def resolve(self, allocation_id: str, payload: Mapping[str, Any]) -> DelegationGrant | None:
        with self._lock:
            pending = self._pending.get(allocation_id)
        if pending is None:
            raise DelegationError("delegation_allocation_not_found", "allocation is not pending")
        decision = str(payload.get("decision") or "").strip().lower()
        if decision == "deny":
            pending.error = DelegationError(
                "delegation_denied", str(payload.get("reason") or "host denied child allocation")[:256],
            )
            pending.event.set()
            return None
        if decision != "grant":
            raise DelegationError("delegation_resolution_invalid", "decision must be grant or deny")
        child_session_id = _ascii(payload.get("child_session_id"), "child_session_id")
        workspace_text = str(payload.get("workspace_root") or "").strip()
        workspace = Path(workspace_text)
        if not workspace_text or not workspace.is_absolute():
            raise DelegationError("delegation_resolution_invalid", "workspace_root must be absolute")
        lease = payload.get("lease")
        if not isinstance(lease, Mapping) or lease.get("session_id") != child_session_id:
            raise DelegationError("delegation_resolution_invalid", "lease must bind the child session")
        budget = payload.get("budget")
        if not isinstance(budget, Mapping):
            raise DelegationError("delegation_resolution_invalid", "budget is required")
        grant = DelegationGrant(
            allocation_id=allocation_id,
            child_session_id=child_session_id,
            workspace_root=workspace.resolve(strict=False),
            lease=dict(lease),
            max_iterations=_bounded_int(budget.get("max_iterations"), "max_iterations", 1, 64),
            timeout_seconds=float(_bounded_int(budget.get("timeout_seconds"), "timeout_seconds", 1, 3600)),
            max_output_bytes=_bounded_int(budget.get("max_output_bytes"), "max_output_bytes", 256, 1_048_576),
        )
        pending.grant = grant
        pending.event.set()
        return grant

    def close(self) -> None:
        with self._lock:
            self._closed = True
            pending = tuple(self._pending.values())
        for item in pending:
            item.error = DelegationError("delegation_broker_closed", "parent session closed")
            item.event.set()


def _ascii(value: Any, field_name: str) -> str:
    text = str(value or "").strip()
    if not text or not text.isascii():
        raise DelegationError("delegation_resolution_invalid", f"{field_name} must be non-empty ASCII")
    return text


def _bounded_int(value: Any, field_name: str, minimum: int, maximum: int) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or not minimum <= value <= maximum:
        raise DelegationError(
            "delegation_resolution_invalid",
            f"budget.{field_name} must be between {minimum} and {maximum}",
        )
    return value
