"""Execution-epoch authority port and an in-memory Lobby reference implementation."""

from __future__ import annotations

import threading
import uuid
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from typing import Callable, Protocol


class EpochError(RuntimeError):
    """A lease CAS or fenced execution attempted an invalid transition."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class LeasePolicy:
    ttl_ms: int
    renew_interval_ms: int
    grace_ms: int

    def __post_init__(self) -> None:
        if self.ttl_ms <= 0 or not 0 < self.renew_interval_ms < self.ttl_ms:
            raise ValueError("lease policy requires 0 < renew_interval_ms < ttl_ms")
        if not 0 <= self.grace_ms < self.ttl_ms:
            raise ValueError("lease policy requires 0 <= grace_ms < ttl_ms")


@dataclass(frozen=True, slots=True)
class LeaseRecord:
    session_id: str
    owner_id: str
    execution_epoch: int
    lease_id: str
    lease_version: int
    issued_at: datetime
    expires_at: datetime
    renew_by: datetime
    grace_expires_at: datetime
    policy: LeasePolicy
    valid: bool = True

    def to_mapping(self) -> dict[str, object]:
        def timestamp(value: datetime) -> str:
            return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")

        return {
            "session_id": self.session_id,
            "owner_id": self.owner_id,
            "execution_epoch": self.execution_epoch,
            "lease_id": self.lease_id,
            "lease_version": self.lease_version,
            "issued_at": timestamp(self.issued_at),
            "expires_at": timestamp(self.expires_at),
            "renew_by": timestamp(self.renew_by),
            "grace_expires_at": timestamp(self.grace_expires_at),
            "ttl_ms": self.policy.ttl_ms,
            "renew_interval_ms": self.policy.renew_interval_ms,
            "grace_ms": self.policy.grace_ms,
        }


@dataclass(frozen=True, slots=True)
class FenceToken:
    session_id: str
    owner_id: str
    execution_epoch: int
    lease_id: str
    lease_version: int


class LeaseAuthority(Protocol):
    def validate(self, token: FenceToken) -> LeaseRecord: ...


class ReferenceLeaseAuthority:
    """Thread-safe behavioral reference for the external Lobby CAS boundary."""

    def __init__(self, *, clock: Callable[[], datetime] | None = None) -> None:
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._records: dict[str, LeaseRecord] = {}
        self._lock = threading.RLock()

    def acquire(self, *, session_id: str, owner_id: str, policy: LeasePolicy,
                expected_lease_version: int, expected_execution_epoch: int,
                refork: bool = False) -> LeaseRecord:
        with self._lock:
            current = self._records.get(session_id)
            actual_version = current.lease_version if current else 0
            actual_epoch = current.execution_epoch if current else 0
            if (expected_lease_version, expected_execution_epoch) != (actual_version, actual_epoch):
                raise EpochError("cas_conflict", "lease version or execution epoch changed")
            owner_changed = current is None or current.owner_id != owner_id
            epoch = 1 if current is None else current.execution_epoch + int(owner_changed or refork)
            now = self._utc_now()
            record = LeaseRecord(
                session_id=session_id, owner_id=owner_id, execution_epoch=epoch,
                lease_id=uuid.uuid4().hex, lease_version=actual_version + 1,
                issued_at=now, expires_at=now + timedelta(milliseconds=policy.ttl_ms),
                renew_by=now + timedelta(milliseconds=policy.renew_interval_ms),
                grace_expires_at=now + timedelta(milliseconds=policy.ttl_ms + policy.grace_ms),
                policy=policy,
            )
            self._records[session_id] = record
            return record

    def renew(self, token: FenceToken) -> LeaseRecord:
        with self._lock:
            current = self._validate_locked(token)
            now = self._utc_now()
            if now >= current.expires_at:
                self._records[token.session_id] = replace(current, valid=False)
                raise EpochError("lease_expired", "lease expired before renewal")
            policy = current.policy
            renewed = replace(
                current, lease_version=current.lease_version + 1,
                issued_at=now, expires_at=now + timedelta(milliseconds=policy.ttl_ms),
                renew_by=now + timedelta(milliseconds=policy.renew_interval_ms),
                grace_expires_at=now + timedelta(milliseconds=policy.ttl_ms + policy.grace_ms),
            )
            self._records[token.session_id] = renewed
            return renewed

    def invalidate(self, token: FenceToken) -> LeaseRecord:
        with self._lock:
            current = self._validate_locked(token)
            invalid = replace(current, valid=False, lease_version=current.lease_version + 1)
            self._records[token.session_id] = invalid
            return invalid

    def token(self, record: LeaseRecord) -> FenceToken:
        return FenceToken(record.session_id, record.owner_id, record.execution_epoch, record.lease_id, record.lease_version)

    def validate(self, token: FenceToken) -> LeaseRecord:
        with self._lock:
            return self._validate_locked(token)

    def current(self, session_id: str) -> LeaseRecord | None:
        with self._lock:
            return self._records.get(session_id)

    def _validate_locked(self, token: FenceToken) -> LeaseRecord:
        current = self._records.get(token.session_id)
        if current is None:
            raise EpochError("lease_missing", "session has no lease")
        if not current.valid:
            raise EpochError("lease_lost", "lease is invalid")
        if (token.owner_id, token.execution_epoch, token.lease_id, token.lease_version) != (
            current.owner_id, current.execution_epoch, current.lease_id, current.lease_version
        ):
            raise EpochError("stale_epoch", "owner, epoch, lease, or version is stale")
        if self._utc_now() >= current.expires_at:
            self._records[token.session_id] = replace(current, valid=False)
            raise EpochError("lease_expired", "lease expired; protected actions are fenced")
        return current

    def _utc_now(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None:
            raise ValueError("lease authority clock must be timezone-aware")
        return value.astimezone(timezone.utc)


class EpochGuard:
    """Require a live authority check immediately before protected actions."""

    def __init__(self, authority: LeaseAuthority) -> None:
        self._authority = authority

    def check(self, token: FenceToken) -> LeaseRecord:
        return self._authority.validate(token)

    def run(self, token: FenceToken, action: Callable[[], object]) -> object:
        self.check(token)
        return action()
