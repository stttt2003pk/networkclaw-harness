"""Durable recovery, takeover classification, and unknown-effect reconciliation."""

from __future__ import annotations

import hashlib
import threading
from dataclasses import dataclass, field, replace
from enum import StrEnum
from types import MappingProxyType
from typing import Any, Callable, Mapping, Protocol, Sequence

from networkclaw_harness.workspace import (
    Checkpoint, CheckpointStore, EpochGuard, FenceToken, WorkspaceError,
)

from .durable import DurableSessionPort
from .models import FactKind, InteractionStatus, InvocationStatus, RunStatus, SemanticFact, StoredFact


class RecoveryError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class RecoveryClassification(StrEnum):
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    WAITING = "waiting"
    SAFE_TO_RETRY = "safe_to_retry"
    UNKNOWN = "unknown"
    CORRUPTED = "corrupted"


class CheckpointDisposition(StrEnum):
    ACCEPTED = "accepted"
    REBUILD = "rebuild"
    DEGRADED = "degraded"
    BLOCKED = "blocked"


class ReconciliationAction(StrEnum):
    NONE = "none"
    RETRY_NEW_INVOCATION = "retry_new_invocation"
    QUERY_REQUIRED = "query_required"
    USER_DECISION_REQUIRED = "user_decision_required"
    OBSERVED_SUCCEEDED = "observed_succeeded"
    OBSERVED_FAILED = "observed_failed"


class ActualEffectState(StrEnum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    NOT_FOUND = "not_found"
    UNKNOWN = "unknown"


class UserEffectDecision(StrEnum):
    ACCEPT_SUCCEEDED = "accept_succeeded"
    ACCEPT_FAILED = "accept_failed"
    RETRY_NEW_INVOCATION = "retry_new_invocation"


class WaitingRecoveryChoice(StrEnum):
    CONTINUE = "continue"
    CANCEL = "cancel"
    START_LINKED_RUN = "start_linked_run"


@dataclass(frozen=True, slots=True)
class RecoveryDiagnostic:
    code: str
    severity: str
    resource_id: str
    summary: str


@dataclass(frozen=True, slots=True)
class RecoveryItem:
    kind: FactKind
    entity_id: str
    state: str
    classification: RecoveryClassification
    cursor: int
    payload: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "payload", MappingProxyType(dict(self.payload)))


@dataclass(frozen=True, slots=True)
class RecoverySnapshot:
    session_id: str
    durable_cursor: int
    execution_epoch: int
    items: tuple[RecoveryItem, ...]
    checkpoint: Checkpoint | None
    checkpoint_disposition: CheckpointDisposition
    diagnostics: tuple[RecoveryDiagnostic, ...]
    waiting_interaction_ids: tuple[str, ...]
    runnable_actions: tuple[str, ...] = ()
    ephemeral_handles: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ToolRecoveryContract:
    tool_name: str
    read_only: bool
    idempotent: bool
    retryable: bool
    side_effecting: bool
    idempotency_key: str | None
    event_dedupe_key: str
    status_query_key: str | None = None

    def __post_init__(self) -> None:
        if not self.tool_name or not self.event_dedupe_key:
            raise ValueError("tool name and event dedupe key are required")
        if self.side_effecting and self.read_only:
            raise ValueError("a side-effecting tool cannot be read-only")
        if self.idempotent and not self.idempotency_key:
            raise ValueError("idempotent tools require an operation idempotency key")

    @property
    def safe_to_retry(self) -> bool:
        return self.read_only and self.idempotent and self.retryable and not self.side_effecting


@dataclass(frozen=True, slots=True)
class ReconciliationResult:
    invocation_id: str
    classification: RecoveryClassification
    action: ReconciliationAction
    actual_state: ActualEffectState | None = None
    requires_new_invocation: bool = False
    summary: str = ""


class EffectStatusQuery(Protocol):
    def __call__(self, contract: ToolRecoveryContract) -> ActualEffectState | str: ...


Migration = Callable[[Mapping[str, Any]], Mapping[str, Any]]
ArtifactVerifier = Callable[[str], str]


class RecoveryReconciler:
    """Reconstructs a safe view without scheduling work from the dead process."""

    def __init__(self, durable: DurableSessionPort, guard: EpochGuard, *, fact_schema_version: int = 1,
                 migrations: Mapping[tuple[FactKind, int], Migration] | None = None) -> None:
        if fact_schema_version < 1:
            raise ValueError("fact schema version must be positive")
        self._durable = durable
        self._guard = guard
        self._schema_version = fact_schema_version
        self._migrations = dict(migrations or {})

    def recover(self, token: FenceToken, *, checkpoint_store: CheckpointStore | None = None,
                hermes_snapshot_hash: str | None = None,
                artifact_manifest_hash: str | None = None,
                artifact_verify: ArtifactVerifier | None = None) -> RecoverySnapshot:
        session_id = token.session_id
        self._guard.check(token)
        initial = self._durable.load(session_id)
        durable_cursor = self._durable.cursor(session_id)
        migrated, migration_diagnostics = self._migrate(initial, token)
        durable_cursor = self._durable.cursor(session_id)
        latest = _latest(migrated)
        recovery_facts: list[SemanticFact] = []
        for (kind, entity_id), stored in latest.items():
            if kind is FactKind.RUN and stored.fact.state == RunStatus.RUNNING:
                recovery_facts.append(SemanticFact(
                    FactKind.RUN, entity_id, RunStatus.INTERRUPTED,
                    {"recovered": True, "source_cursor": stored.cursor,
                     "execution_epoch": token.execution_epoch},
                ))
            if kind is FactKind.INVOCATION and stored.fact.state in {
                InvocationStatus.QUEUED, InvocationStatus.RUNNING,
                InvocationStatus.CANCEL_REQUESTED,
            }:
                recovery_facts.append(SemanticFact(
                    FactKind.INVOCATION, entity_id, InvocationStatus.UNKNOWN,
                    {**_recovery_identity(stored.fact.payload), "recovered": True,
                     "source_cursor": stored.cursor, "replay_allowed": False,
                     "execution_epoch": token.execution_epoch},
                ))
        if recovery_facts:
            identity = hashlib.sha256(
                ":".join(f"{fact.kind}:{fact.entity_id}:{fact.state}" for fact in recovery_facts).encode()
            ).hexdigest()[:16]
            self._guard.check(token)
            self._durable.commit(
                session_id=session_id,
                event_id=f"recovery:{token.execution_epoch}:{identity}",
                facts=tuple(recovery_facts),
            )
            self._guard.check(token)
            migrated, repeated_diagnostics = self._migrate(self._durable.load(session_id), token)
            migration_diagnostics.extend(repeated_diagnostics)
            durable_cursor = self._durable.cursor(session_id)
            latest = _latest(migrated)

        checkpoint, disposition, checkpoint_diagnostics = self._checkpoint(
            checkpoint_store, token, durable_cursor,
            hermes_snapshot_hash, artifact_manifest_hash,
        )
        items: list[RecoveryItem] = []
        diagnostics = [*migration_diagnostics, *checkpoint_diagnostics]
        waiting: list[str] = []
        for (kind, entity_id), stored in sorted(latest.items(), key=lambda item: item[1].cursor):
            classification = _classify(stored.fact)
            if kind is FactKind.ARTIFACT_REF and artifact_verify is not None:
                artifact_state = artifact_verify(entity_id)
                if artifact_state != "available":
                    classification = RecoveryClassification.CORRUPTED
                    code = "artifact_missing" if artifact_state == "missing" else "artifact_hash_mismatch"
                    diagnostics.append(RecoveryDiagnostic(
                        code, "error", entity_id,
                        f"Artifact verification returned {artifact_state}.",
                    ))
            if kind is FactKind.INTERACTION and classification is RecoveryClassification.WAITING:
                waiting.append(entity_id)
            items.append(RecoveryItem(
                kind, entity_id, stored.fact.state, classification,
                stored.cursor, _strip_ephemeral(stored.fact.payload),
            ))
        if any(item.classification is RecoveryClassification.CORRUPTED for item in items):
            disposition = CheckpointDisposition.BLOCKED
        self._guard.check(token)
        return RecoverySnapshot(
            session_id, durable_cursor, token.execution_epoch, tuple(items),
            checkpoint, disposition, tuple(_deduplicate_diagnostics(diagnostics)), tuple(waiting),
        )

    def record_waiting_choice(self, token: FenceToken, *, run_id: str,
                              choice: WaitingRecoveryChoice | str,
                              decision_id: str, linked_run_id: str | None = None) -> SemanticFact:
        selected = WaitingRecoveryChoice(choice)
        self._guard.check(token)
        if selected is WaitingRecoveryChoice.START_LINKED_RUN and not linked_run_id:
            raise RecoveryError("linked_run_required", "starting a related run requires linked_run_id")
        payload = {
            "run_id": run_id, "choice": selected, "decision_id": decision_id,
            "linked_run_id": linked_run_id, "execution_epoch": token.execution_epoch,
        }
        for stored in self._durable.load(token.session_id):
            prior = stored.fact
            if (prior.kind is FactKind.OBSERVATION and prior.state == "recovery_choice"
                    and prior.payload.get("run_id") == run_id):
                if prior.entity_id == decision_id and dict(prior.payload) == payload:
                    return prior
                raise RecoveryError("recovery_choice_conflict", "waiting run already has a recovery choice")
        latest = _latest(self._durable.load(token.session_id))
        run = latest.get((FactKind.RUN, run_id))
        if run is None or run.fact.state != RunStatus.WAITING:
            raise RecoveryError("run_not_waiting", "recovery choice requires a durable waiting run")
        if (selected is WaitingRecoveryChoice.START_LINKED_RUN
                and (FactKind.RUN, str(linked_run_id)) in latest):
            raise RecoveryError("linked_run_conflict", "linked run id already exists")
        fact = SemanticFact(FactKind.OBSERVATION, decision_id, "recovery_choice", payload)
        facts = [fact]
        if selected is WaitingRecoveryChoice.CANCEL:
            facts.append(SemanticFact(FactKind.RUN, run_id, RunStatus.CANCELLED, {
                "recovery_decision_id": decision_id,
                "execution_epoch": token.execution_epoch,
            }))
        elif selected is WaitingRecoveryChoice.START_LINKED_RUN:
            assert linked_run_id is not None
            facts.extend((
                SemanticFact(FactKind.RUN, run_id, RunStatus.BLOCKED, {
                    "recovery_decision_id": decision_id,
                    "superseded_by_run_id": linked_run_id,
                    "execution_epoch": token.execution_epoch,
                }),
                SemanticFact(FactKind.RUN, linked_run_id, RunStatus.RUNNING, {
                    "recovery_decision_id": decision_id,
                    "linked_from_run_id": run_id,
                    "execution_epoch": token.execution_epoch,
                }),
            ))
        self._guard.check(token)
        self._durable.commit(
            session_id=token.session_id, event_id=f"recovery-choice:{decision_id}", facts=tuple(facts),
        )
        return fact

    def _migrate(self, records: Sequence[StoredFact], token: FenceToken
                 ) -> tuple[tuple[StoredFact, ...], list[RecoveryDiagnostic]]:
        result: list[StoredFact] = []
        diagnostics: list[RecoveryDiagnostic] = []
        for stored in records:
            payload = dict(stored.fact.payload)
            version = int(payload.pop("schema_version", self._schema_version))
            if version > self._schema_version:
                raise RecoveryError("fact_schema_incompatible", "durable fact schema is newer than this runtime")
            original = version
            while version < self._schema_version:
                migration = self._migrations.get((stored.fact.kind, version))
                if migration is None:
                    raise RecoveryError("fact_migration_missing", "durable fact migration is unavailable")
                payload = dict(migration(payload))
                version += 1
            if original != version:
                payload["schema_version"] = version
                migrated_fact = replace(stored.fact, payload=payload)
                stored = replace(stored, fact=migrated_fact)
                diagnostics.append(RecoveryDiagnostic(
                    "fact_migrated", "info", stored.fact.entity_id,
                    f"Fact schema migrated from {original} to {version}.",
                ))
                audit = SemanticFact(FactKind.OBSERVATION, f"migration-{stored.cursor}", "migrated", {
                    "source_cursor": stored.cursor, "fact_kind": stored.fact.kind,
                    "from_version": original, "to_version": version,
                    "execution_epoch": token.execution_epoch,
                })
                self._guard.check(token)
                self._durable.commit(
                    session_id=token.session_id,
                    event_id=f"recovery-migration:{stored.cursor}:v{version}", facts=(audit,),
                )
            result.append(stored)
        return tuple(result), diagnostics

    @staticmethod
    def _checkpoint(store: CheckpointStore | None, token: FenceToken, durable_cursor: int,
                    hermes_hash: str | None, artifact_hash: str | None
                    ) -> tuple[Checkpoint | None, CheckpointDisposition, list[RecoveryDiagnostic]]:
        if store is None:
            return None, CheckpointDisposition.REBUILD, []
        try:
            checkpoint = store.load(
                durable_cursor=str(durable_cursor), owner_id=token.owner_id,
                execution_epoch=token.execution_epoch,
            )
        except WorkspaceError as error:
            dispositions = {
                "checkpoint_stale": CheckpointDisposition.REBUILD,
                "checkpoint_corrupt": CheckpointDisposition.BLOCKED,
                "checkpoint_incompatible": CheckpointDisposition.BLOCKED,
            }
            disposition = dispositions.get(error.code, CheckpointDisposition.BLOCKED)
            return None, disposition, [RecoveryDiagnostic(
                error.code, "warning" if disposition is CheckpointDisposition.REBUILD else "error",
                "checkpoint", str(error),
            )]
        if checkpoint is None:
            return None, CheckpointDisposition.REBUILD, [RecoveryDiagnostic(
                "checkpoint_missing", "warning", "checkpoint",
                "Workspace checkpoint is missing and will be rebuilt from durable facts.",
            )]
        checkpoint = replace(checkpoint, state=_strip_ephemeral(checkpoint.state))
        diagnostics: list[RecoveryDiagnostic] = []
        disposition = CheckpointDisposition.ACCEPTED
        if hermes_hash is not None and checkpoint.hermes_snapshot_hash != hermes_hash:
            diagnostics.append(RecoveryDiagnostic(
                "hermes_snapshot_hash_mismatch", "warning", "checkpoint",
                "Hermes snapshot hash does not match and must be rebuilt.",
            ))
            disposition = CheckpointDisposition.REBUILD
            checkpoint = None
        if artifact_hash is not None and checkpoint is not None and checkpoint.artifact_manifest_hash != artifact_hash:
            diagnostics.append(RecoveryDiagnostic(
                "artifact_manifest_hash_mismatch", "error", "checkpoint",
                "Artifact manifest hash does not match durable metadata.",
            ))
            disposition = CheckpointDisposition.BLOCKED
            checkpoint = None
        return checkpoint, disposition, diagnostics


class SideEffectReconciler:
    def __init__(self, durable: DurableSessionPort, guard: EpochGuard) -> None:
        self._durable = durable
        self._guard = guard
        self._lock = threading.RLock()

    def reconcile(self, token: FenceToken, *, invocation_id: str,
                  contract: ToolRecoveryContract,
                  status_query: EffectStatusQuery | None = None,
                  user_decision: UserEffectDecision | str | None = None,
                  new_invocation_id: str | None = None) -> ReconciliationResult:
        with self._lock:
            self._guard.check(token)
            latest = _latest(self._durable.load(token.session_id)).get((FactKind.INVOCATION, invocation_id))
            if latest is None:
                raise RecoveryError("invocation_not_found", "invocation does not exist")
            if latest.fact.state != InvocationStatus.UNKNOWN:
                return ReconciliationResult(
                    invocation_id, RecoveryClassification.COMPLETED, ReconciliationAction.NONE,
                    summary="Invocation already has a concrete state.",
                )
            durable_contract = _contract_from_payload(latest.fact.payload)
            if durable_contract is None:
                raise RecoveryError("recovery_contract_missing", "unknown invocation has no durable recovery contract")
            if durable_contract != contract:
                raise RecoveryError("recovery_contract_mismatch", "reconciliation contract differs from durable intent")
            if contract.safe_to_retry:
                if not new_invocation_id:
                    return ReconciliationResult(
                        invocation_id, RecoveryClassification.SAFE_TO_RETRY,
                        ReconciliationAction.RETRY_NEW_INVOCATION, requires_new_invocation=True,
                        summary="A new invocation id is required for the retry.",
                    )
                return self._record_retry(token, invocation_id, new_invocation_id, contract, "policy")
            query_attempted = status_query is not None and bool(contract.status_query_key)
            if query_attempted:
                actual = ActualEffectState(status_query(contract))
                self._guard.check(token)
                if actual in {ActualEffectState.SUCCEEDED, ActualEffectState.FAILED}:
                    return self._record_observation(token, invocation_id, contract, actual)
            if user_decision is None:
                action = (ReconciliationAction.QUERY_REQUIRED if contract.status_query_key and not query_attempted
                          else ReconciliationAction.USER_DECISION_REQUIRED)
                return ReconciliationResult(
                    invocation_id, RecoveryClassification.UNKNOWN, action,
                    summary="Unknown side effect remains blocked pending reconciliation.",
                )
            decision = UserEffectDecision(user_decision)
            if decision is UserEffectDecision.RETRY_NEW_INVOCATION:
                if not new_invocation_id:
                    raise RecoveryError("new_invocation_required", "an explicit retry requires a new invocation id")
                if not contract.idempotent or not contract.idempotency_key:
                    raise RecoveryError("unsafe_retry", "non-idempotent unknown effects cannot be retried")
                return self._record_retry(token, invocation_id, new_invocation_id, contract, "user")
            actual = (ActualEffectState.SUCCEEDED if decision is UserEffectDecision.ACCEPT_SUCCEEDED
                      else ActualEffectState.FAILED)
            return self._record_observation(token, invocation_id, contract, actual, source="user")

    def _record_observation(self, token: FenceToken, invocation_id: str,
                            contract: ToolRecoveryContract, actual: ActualEffectState,
                            *, source: str = "status_query") -> ReconciliationResult:
        status = InvocationStatus.SUCCEEDED if actual is ActualEffectState.SUCCEEDED else InvocationStatus.FAILED
        action = (ReconciliationAction.OBSERVED_SUCCEEDED if actual is ActualEffectState.SUCCEEDED
                  else ReconciliationAction.OBSERVED_FAILED)
        payload = _contract_payload(contract) | {
            "reconciled": True, "reconciliation_source": source,
            "actual_state": actual, "replay_allowed": False,
            "execution_epoch": token.execution_epoch,
        }
        self._guard.check(token)
        self._require_unknown(token.session_id, invocation_id)
        self._durable.commit(
            session_id=token.session_id,
            event_id=f"reconcile:{invocation_id}:{source}:{actual}",
            facts=(
                SemanticFact(FactKind.INVOCATION, invocation_id, status, payload),
                SemanticFact(FactKind.OUTCOME, invocation_id, "observed", {
                    **payload, "execution_status": status,
                    "business_success": actual is ActualEffectState.SUCCEEDED,
                }),
            ),
        )
        return ReconciliationResult(
            invocation_id, RecoveryClassification.COMPLETED, action, actual,
            summary=f"Actual business state was reconciled as {actual}.",
        )

    def _record_retry(self, token: FenceToken, invocation_id: str, new_invocation_id: str,
                      contract: ToolRecoveryContract, source: str) -> ReconciliationResult:
        if new_invocation_id == invocation_id:
            raise RecoveryError("new_invocation_required", "retry cannot reuse the old invocation id")
        payload = _contract_payload(contract) | {
            "reconciliation_source": source, "retries_invocation_id": invocation_id,
            "execution_epoch": token.execution_epoch,
        }
        self._guard.check(token)
        self._require_unknown(token.session_id, invocation_id)
        self._durable.commit(
            session_id=token.session_id,
            event_id=f"reconcile:{invocation_id}:retry:{new_invocation_id}",
            facts=(SemanticFact(FactKind.INVOCATION, new_invocation_id, InvocationStatus.QUEUED, payload),),
        )
        return ReconciliationResult(
            invocation_id, RecoveryClassification.SAFE_TO_RETRY,
            ReconciliationAction.RETRY_NEW_INVOCATION,
            requires_new_invocation=True,
            summary=f"Retry was authorized as new invocation {new_invocation_id}.",
        )

    def _require_unknown(self, session_id: str, invocation_id: str) -> None:
        latest = _latest(self._durable.load(session_id)).get((FactKind.INVOCATION, invocation_id))
        if latest is None or latest.fact.state != InvocationStatus.UNKNOWN:
            raise RecoveryError("reconciliation_conflict", "invocation state changed during reconciliation")


def _latest(records: Sequence[StoredFact]) -> dict[tuple[FactKind, str], StoredFact]:
    result: dict[tuple[FactKind, str], StoredFact] = {}
    for stored in records:
        result[(stored.fact.kind, stored.fact.entity_id)] = stored
    return result


def _classify(fact: SemanticFact) -> RecoveryClassification:
    if fact.kind is FactKind.RUN:
        if fact.state == RunStatus.COMPLETED:
            return RecoveryClassification.COMPLETED
        if fact.state == RunStatus.CANCELLED:
            return RecoveryClassification.CANCELLED
        if fact.state == RunStatus.WAITING:
            return RecoveryClassification.WAITING
        return RecoveryClassification.UNKNOWN
    if fact.kind is FactKind.INTERACTION:
        return (RecoveryClassification.WAITING if fact.state == InteractionStatus.PENDING
                else RecoveryClassification.COMPLETED)
    if fact.kind is FactKind.INVOCATION:
        if fact.state == InvocationStatus.CANCELLED:
            return RecoveryClassification.CANCELLED
        if fact.state == InvocationStatus.UNKNOWN:
            contract = _contract_from_payload(fact.payload)
            return (RecoveryClassification.SAFE_TO_RETRY if contract and contract.safe_to_retry
                    else RecoveryClassification.UNKNOWN)
        if fact.state in {InvocationStatus.SUCCEEDED, InvocationStatus.FAILED}:
            return RecoveryClassification.COMPLETED
        return RecoveryClassification.UNKNOWN
    return RecoveryClassification.COMPLETED


def _contract_from_payload(payload: Mapping[str, Any]) -> ToolRecoveryContract | None:
    required = {"tool_name", "read_only", "idempotent", "retryable", "side_effecting", "event_dedupe_key"}
    if not required <= payload.keys():
        return None
    return ToolRecoveryContract(
        tool_name=str(payload["tool_name"]), read_only=bool(payload["read_only"]),
        idempotent=bool(payload["idempotent"]), retryable=bool(payload["retryable"]),
        side_effecting=bool(payload["side_effecting"]),
        idempotency_key=str(payload["idempotency_key"]) if payload.get("idempotency_key") else None,
        event_dedupe_key=str(payload["event_dedupe_key"]),
        status_query_key=str(payload["status_query_key"]) if payload.get("status_query_key") else None,
    )


def _contract_payload(contract: ToolRecoveryContract) -> dict[str, Any]:
    return {
        "tool_name": contract.tool_name, "read_only": contract.read_only,
        "idempotent": contract.idempotent, "retryable": contract.retryable,
        "side_effecting": contract.side_effecting,
        "idempotency_key": contract.idempotency_key,
        "event_dedupe_key": contract.event_dedupe_key,
        "status_query_key": contract.status_query_key,
    }


def _recovery_identity(payload: Mapping[str, Any]) -> dict[str, Any]:
    names = {
        "action_type", "arguments_hash", "event_dedupe_key", "idempotency_key",
        "idempotent", "read_only", "retryable", "run_id", "side_effecting",
        "status_query_key", "tool_name",
    }
    return {name: payload[name] for name in names if name in payload}


_EPHEMERAL_KEYS = frozenset({
    "browser_handle", "mcp_handle", "pid", "pipe", "process_handle",
    "subagent_handle", "tool_handle", "invocation_handle",
})


def _strip_ephemeral(payload: Mapping[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in payload.items() if key not in _EPHEMERAL_KEYS}


def _deduplicate_diagnostics(values: Sequence[RecoveryDiagnostic]) -> list[RecoveryDiagnostic]:
    result: list[RecoveryDiagnostic] = []
    seen: set[tuple[str, str, str]] = set()
    for value in values:
        identity = (value.code, value.resource_id, value.summary)
        if identity not in seen:
            seen.add(identity)
            result.append(value)
    return result
