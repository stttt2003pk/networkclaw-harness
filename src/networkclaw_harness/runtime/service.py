"""Ordered semantic commits, side effects, checkpoints, and passive recovery."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any, Callable, Mapping, Sequence

from networkclaw_harness.workspace import Checkpoint, CheckpointStore, EpochGuard, FenceToken, WorkspaceError

from .durable import DurableSessionPort, DurableWriteError
from .models import FactKind, InvocationStatus, SemanticFact, StoredFact


class PersistenceFailure(DurableWriteError):
    def __init__(self, code: str, message: str, *, effect_state: str) -> None:
        super().__init__(code, message)
        self.code = code
        self.effect_state = effect_state


def classify_persistence_failure(error: Exception, *, effect_started: bool) -> PersistenceFailure:
    return PersistenceFailure(
        str(getattr(error, "code", "durable_write_failed")), str(error),
        effect_state="unknown" if effect_started else "safe",
    )


@dataclass(frozen=True, slots=True)
class ToolExecution:
    status: InvocationStatus
    result: Mapping[str, Any]
    business_success: bool | None
    artifact_id: str | None = None


@dataclass(frozen=True, slots=True)
class RecoveryView:
    session_id: str
    cursor: int
    facts: tuple[StoredFact, ...]
    checkpoint: Checkpoint | None
    interrupted_entities: tuple[str, ...]
    unknown_invocations: tuple[str, ...]
    runnable_actions: tuple[()] = ()
    ephemeral_handles: tuple[()] = ()


class SemanticPersistenceService:
    def __init__(self, durable: DurableSessionPort, guard: EpochGuard) -> None:
        self._durable = durable
        self._guard = guard

    def commit_facts(self, token: FenceToken, *, event_id: str,
                     facts: Sequence[SemanticFact]):
        self._guard.check(token)
        return self._durable.commit(session_id=token.session_id, event_id=event_id, facts=facts)

    def execute_tool(self, token: FenceToken, *, event_id: str, invocation_id: str,
                     intent: Mapping[str, Any], approval: Mapping[str, Any] | None,
                     executor: Callable[[], Mapping[str, Any]],
                     artifact_commit: Callable[[Mapping[str, Any]], str | None] | None = None,
                     business_success: Callable[[Mapping[str, Any]], bool | None] | None = None,
                     failure_injector: Callable[[str], None] | None = None) -> ToolExecution:
        inject = failure_injector or (lambda stage: None)
        self._guard.check(token)
        inject("before_intent")
        preconditions = [SemanticFact(FactKind.INVOCATION, invocation_id, InvocationStatus.RUNNING, intent)]
        if approval is not None:
            preconditions.append(SemanticFact(FactKind.APPROVAL, invocation_id, "approved", approval))
        try:
            self._durable.commit(session_id=token.session_id, event_id=f"{event_id}:intent", facts=preconditions)
        except Exception as error:
            raise classify_persistence_failure(error, effect_started=False) from error

        self._guard.check(token)
        inject("after_intent")
        try:
            result = dict(executor())
        except (ConnectionError, TimeoutError) as error:
            recovery_fields = {
                name: intent[name] for name in (
                    "action_type", "arguments_hash", "event_dedupe_key", "idempotency_key",
                    "idempotent", "read_only", "retryable", "run_id", "side_effecting",
                    "status_query_key", "tool_name",
                ) if name in intent
            }
            unknown = {
                **recovery_fields, "reason": type(error).__name__,
                "message": str(error), "replay_allowed": False,
            }
            try:
                self._durable.commit(
                    session_id=token.session_id, event_id=f"{event_id}:unknown",
                    facts=(SemanticFact(FactKind.INVOCATION, invocation_id, InvocationStatus.UNKNOWN, unknown),),
                )
            except Exception as commit_error:
                raise classify_persistence_failure(commit_error, effect_started=True) from commit_error
            return ToolExecution(InvocationStatus.UNKNOWN, unknown, None)
        except Exception as error:
            failed = {"reason": type(error).__name__, "message": str(error)}
            try:
                self._durable.commit(
                    session_id=token.session_id, event_id=f"{event_id}:failed",
                    facts=(SemanticFact(FactKind.INVOCATION, invocation_id, InvocationStatus.FAILED, failed),),
                )
            except Exception as commit_error:
                raise classify_persistence_failure(commit_error, effect_started=True) from commit_error
            return ToolExecution(InvocationStatus.FAILED, failed, False)

        inject("after_effect")
        self._guard.check(token)
        artifact_id = artifact_commit(result) if artifact_commit is not None else None
        succeeded = business_success(result) if business_success is not None else None
        payload = {"result": result, "business_success": succeeded}
        for name in ("tool_name", "run_id", "action_type"):
            if name in intent:
                payload[name] = intent[name]
        if artifact_id is not None:
            payload["artifact_id"] = artifact_id
        self._guard.check(token)
        inject("before_result_commit")
        try:
            self._durable.commit(
                session_id=token.session_id, event_id=f"{event_id}:result",
                facts=(
                    SemanticFact(FactKind.INVOCATION, invocation_id, InvocationStatus.SUCCEEDED, payload),
                    SemanticFact(FactKind.OUTCOME, invocation_id, "observed", payload),
                ),
            )
        except Exception as error:
            raise classify_persistence_failure(error, effect_started=True) from error
        return ToolExecution(InvocationStatus.SUCCEEDED, result, succeeded, artifact_id)

    def request_cancel(self, token: FenceToken, *, event_id: str, invocation_id: str) -> None:
        self._guard.check(token)
        self._durable.commit(
            session_id=token.session_id, event_id=event_id,
            facts=(SemanticFact(FactKind.INVOCATION, invocation_id, InvocationStatus.CANCEL_REQUESTED),),
        )

    def confirm_cancelled(self, token: FenceToken, *, event_id: str, invocation_id: str) -> None:
        self._guard.check(token)
        self._durable.commit(
            session_id=token.session_id, event_id=event_id,
            facts=(SemanticFact(FactKind.INVOCATION, invocation_id, InvocationStatus.CANCELLED),),
        )

    def commit_delivery(self, token: FenceToken, *, event_id: str,
                        delivery_facts: Sequence[SemanticFact],
                        artifact_commit: Callable[[], str], checkpoint_store: CheckpointStore,
                        hermes_snapshot_hash: str, state: Mapping[str, Any]) -> Checkpoint:
        self._guard.check(token)
        artifact_manifest_hash = artifact_commit()
        self._guard.check(token)
        ack = self._durable.commit(
            session_id=token.session_id, event_id=event_id, facts=delivery_facts,
        )
        return checkpoint_store.write(
            token, durable_cursor=str(ack.cursor), hermes_snapshot_hash=hermes_snapshot_hash,
            artifact_manifest_hash=artifact_manifest_hash, state=state,
        )

    def recover(self, token: FenceToken, *, checkpoint_store: CheckpointStore | None = None) -> RecoveryView:
        self._guard.check(token)
        facts = self._durable.load(token.session_id)
        interrupted: list[str] = []
        unknown: list[str] = []
        latest: dict[tuple[FactKind, str], StoredFact] = {}
        for stored in facts:
            latest[(stored.fact.kind, stored.fact.entity_id)] = stored
        for (kind, entity_id), stored in latest.items():
            if kind is FactKind.RUN and stored.fact.state == "running":
                interrupted.append(entity_id)
            if kind is FactKind.INVOCATION and stored.fact.state in {"queued", "running", "cancel_requested"}:
                unknown.append(entity_id)
        if interrupted or unknown:
            recovery_facts = tuple(
                [SemanticFact(FactKind.RUN, entity_id, "interrupted", {"recovered": True}) for entity_id in interrupted]
                + [SemanticFact(
                    FactKind.INVOCATION, entity_id, InvocationStatus.UNKNOWN,
                    {"recovered": True, "replay_allowed": False},
                ) for entity_id in unknown]
            )
            self._durable.commit(
                session_id=token.session_id,
                event_id=f"recovery:{token.execution_epoch}", facts=recovery_facts,
            )
            facts = self._durable.load(token.session_id)
        cursor = self._durable.cursor(token.session_id)
        checkpoint = None
        if checkpoint_store is not None and cursor:
            try:
                checkpoint = checkpoint_store.load(
                    durable_cursor=str(cursor), owner_id=token.owner_id,
                    execution_epoch=token.execution_epoch,
                )
            except WorkspaceError as error:
                if error.code not in {"checkpoint_stale", "checkpoint_incompatible"}:
                    raise
        if checkpoint is not None:
            ephemeral = {"pid", "process_handle", "tool_handle", "invocation_handle", "subagent_handle"}
            checkpoint = replace(
                checkpoint,
                state={key: value for key, value in checkpoint.state.items() if key not in ephemeral},
            )
        return RecoveryView(
            token.session_id, cursor, facts, checkpoint,
            tuple(interrupted), tuple(unknown),
        )
