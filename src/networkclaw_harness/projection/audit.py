"""Append-only, redacted audit records derived from durable semantic facts."""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from types import MappingProxyType
from typing import Any, Mapping, Protocol, Sequence

from networkclaw_harness.runtime import DurableSessionPort, FactKind, StoredFact

from .events import project_mapping


class AuditError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class AuditPolicy:
    retention_days: int = 90
    allowed_roles: tuple[str, ...] = ("tenant_auditor", "support_diagnostics")
    max_export_records: int = 10_000

    def __post_init__(self) -> None:
        if self.retention_days <= 0 or self.max_export_records <= 0 or not self.allowed_roles:
            raise ValueError("audit policy limits and roles are required")


@dataclass(frozen=True, slots=True)
class AuditContext:
    tenant_id: str
    user_id: str
    session_id: str
    actor: str
    execution_epoch: int
    request_id: str | None = None
    run_id: str | None = None
    secret_values: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class AuditRecord:
    audit_id: str
    tenant_id: str
    user_id: str
    session_id: str
    actor: str
    request_id: str | None
    run_id: str | None
    cursor: int
    event_id: str
    action: str
    resource_id: str
    execution_epoch: int
    outcome: str
    evidence_refs: tuple[str, ...]
    details: Mapping[str, Any]
    occurred_at: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "details", MappingProxyType(dict(self.details)))


class AuditStorePort(Protocol):
    def append(self, records: Sequence[AuditRecord]) -> None: ...
    def query(self, tenant_id: str, session_id: str | None = None) -> tuple[AuditRecord, ...]: ...
    def purge_before(self, cutoff: datetime) -> int: ...


class ReferenceAuditStore:
    def __init__(self) -> None:
        self._records: list[AuditRecord] = []
        self._identities: set[str] = set()
        self._lock = threading.RLock()

    def append(self, records: Sequence[AuditRecord]) -> None:
        with self._lock:
            for record in records:
                if record.audit_id in self._identities:
                    continue
                self._identities.add(record.audit_id)
                self._records.append(record)

    def query(self, tenant_id: str, session_id: str | None = None) -> tuple[AuditRecord, ...]:
        with self._lock:
            return tuple(item for item in self._records
                         if item.tenant_id == tenant_id
                         and (session_id is None or item.session_id == session_id))

    def purge_before(self, cutoff: datetime) -> int:
        if cutoff.tzinfo is None:
            raise ValueError("audit cutoff must be timezone-aware")
        with self._lock:
            retained = [item for item in self._records
                        if datetime.fromisoformat(item.occurred_at.replace("Z", "+00:00")) >= cutoff]
            removed = len(self._records) - len(retained)
            self._records = retained
            self._identities = {item.audit_id for item in retained}
            return removed


class AuditService:
    def __init__(self, durable: DurableSessionPort, store: AuditStorePort,
                 policy: AuditPolicy = AuditPolicy()) -> None:
        self._durable = durable
        self._store = store
        self._policy = policy

    def capture(self, context: AuditContext, *, after_cursor: int = 0,
                now: datetime | None = None) -> tuple[AuditRecord, ...]:
        timestamp = _timestamp(now or datetime.now(timezone.utc))
        records = tuple(
            self._record(stored, context, timestamp)
            for stored in self._durable.load(context.session_id, after_cursor=after_cursor)
        )
        self._store.append(records)
        return records

    def export(self, *, tenant_id: str, role: str, session_id: str | None = None) -> str:
        self._authorize(role)
        records = self._store.query(tenant_id, session_id)
        if len(records) > self._policy.max_export_records:
            raise AuditError("audit_export_too_large", "audit export exceeds its record limit")
        return "".join(json.dumps(
            _audit_mapping(record),
            sort_keys=True, separators=(",", ":"), ensure_ascii=True,
        ) + "\n" for record in records)

    def diagnostic_snapshot(self, *, tenant_id: str, role: str,
                            session_id: str) -> Mapping[str, Any]:
        self._authorize(role)
        records = self._store.query(tenant_id, session_id)
        actions: dict[str, int] = {}
        outcomes: dict[str, int] = {}
        for record in records:
            actions[record.action] = actions.get(record.action, 0) + 1
            outcomes[record.outcome] = outcomes.get(record.outcome, 0) + 1
        return {
            "tenant_id": tenant_id, "session_id": session_id,
            "record_count": len(records),
            "latest_cursor": max((item.cursor for item in records), default=0),
            "actions": actions, "outcomes": outcomes,
        }

    def enforce_retention(self, *, now: datetime | None = None) -> int:
        current = now or datetime.now(timezone.utc)
        return self._store.purge_before(current - timedelta(days=self._policy.retention_days))

    def _record(self, stored: StoredFact, context: AuditContext,
                timestamp: str) -> AuditRecord:
        fact = stored.fact
        details = _audit_details(fact.payload, context.secret_values)
        projected_evidence = project_mapping(
            {"items": list(fact.payload.get("evidence_refs", ()))},
            secret_values=context.secret_values,
        )["items"]
        evidence = tuple(str(item) for item in projected_evidence)
        run_id = str(fact.payload.get("run_id") or context.run_id or "") or None
        return AuditRecord(
            audit_id=f"{context.tenant_id}:{stored.session_id}:{stored.cursor}",
            tenant_id=context.tenant_id, user_id=context.user_id,
            session_id=context.session_id, actor=context.actor,
            request_id=context.request_id, run_id=run_id,
            cursor=stored.cursor, event_id=stored.event_id,
            action=f"{fact.kind}.{fact.state}", resource_id=fact.entity_id,
            execution_epoch=context.execution_epoch, outcome=fact.state,
            evidence_refs=evidence, details=details, occurred_at=timestamp,
        )

    def _authorize(self, role: str) -> None:
        if role not in self._policy.allowed_roles:
            raise AuditError("audit_access_denied", "role cannot access audit records")


_AUDIT_FIELDS = frozenset({
    "action_type", "approval_id", "approval_version", "arguments_hash",
    "authorization_hash", "business_success", "error_code", "evidence_refs",
    "execution_status", "interaction_id", "interaction_version", "policy_id",
    "replay_allowed", "run_id", "run_version", "status", "summary", "tool_name",
    "provider_metadata",
})


def _audit_details(payload: Mapping[str, Any], secrets: Sequence[str]) -> Mapping[str, Any]:
    selected = {key: value for key, value in payload.items() if key in _AUDIT_FIELDS}
    return project_mapping(selected, secret_values=secrets)


def _timestamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _audit_mapping(record: AuditRecord) -> dict[str, Any]:
    return {
        "audit_id": record.audit_id, "tenant_id": record.tenant_id,
        "user_id": record.user_id, "session_id": record.session_id,
        "actor": record.actor, "request_id": record.request_id,
        "run_id": record.run_id, "cursor": record.cursor,
        "event_id": record.event_id, "action": record.action,
        "resource_id": record.resource_id,
        "execution_epoch": record.execution_epoch, "outcome": record.outcome,
        "evidence_refs": record.evidence_refs, "details": dict(record.details),
        "occurred_at": record.occurred_at,
    }
