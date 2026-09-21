"""Durable interaction, steering, approval, cancellation, and waiting control."""

from __future__ import annotations

import hashlib
import json
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import StrEnum
from types import MappingProxyType
from typing import Any, Callable, Mapping, Sequence

from networkclaw_harness.policies import ApprovalSnapshot, hash_arguments

from .durable import DurableSessionPort
from .models import FactKind, InteractionStatus, InvocationStatus, RunStatus, SemanticFact


class InteractionError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class InputKind(StrEnum):
    NEW_GOAL = "new_goal"
    GOAL_SUPPLEMENT = "goal_supplement"
    STEER = "steer"
    CLARIFICATION_ANSWER = "clarification_answer"
    APPROVAL_RESOLUTION = "approval_resolution"
    CANCEL = "cancel"


class InteractionKind(StrEnum):
    CLARIFICATION = "clarification"
    APPROVAL = "approval"


class ApprovalDecision(StrEnum):
    APPROVED = "approved"
    DENIED = "denied"


class CancelDisposition(StrEnum):
    CANCELLED = "cancelled"
    PENDING = "pending"
    UNKNOWN = "unknown"


class TimeoutDisposition(StrEnum):
    CANCEL = "cancel"
    BLOCK = "block"
    PARTIAL = "partial"


class DisconnectDisposition(StrEnum):
    CONTINUE = "continue"
    CANCEL = "cancel"
    BLOCK = "block"


@dataclass(frozen=True, slots=True)
class RuntimeControlPolicy:
    frontend_disconnect: DisconnectDisposition = DisconnectDisposition.CONTINUE
    host_disconnect: DisconnectDisposition = DisconnectDisposition.CANCEL
    background_allowed: bool = True
    timeout_disposition: TimeoutDisposition = TimeoutDisposition.BLOCK

    def __post_init__(self) -> None:
        if self.frontend_disconnect is DisconnectDisposition.CONTINUE and not self.background_allowed:
            raise ValueError("frontend continuation requires background execution")
        if self.host_disconnect is DisconnectDisposition.CONTINUE:
            raise ValueError("host disconnect cannot leave unfenced execution running")


@dataclass(frozen=True, slots=True)
class ApprovalDetails:
    tool_name: str
    arguments_summary: str
    arguments_hash: str
    risk: str
    scope: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class InteractionRequest:
    interaction_id: str
    session_id: str
    run_id: str
    item_id: str
    version: int
    kind: InteractionKind
    prompt: str
    options: tuple[str, ...]
    reason: str
    blocked_items: tuple[str, ...]
    deadline: datetime
    approval: ApprovalDetails | None = None

    def __post_init__(self) -> None:
        identities = (self.interaction_id, self.session_id, self.run_id, self.item_id)
        if any(not value or not value.isascii() for value in identities):
            raise ValueError("interaction identities must be non-empty ASCII strings")
        if self.version < 1 or not self.prompt or not self.reason:
            raise ValueError("interaction version, prompt, and reason are required")
        if self.deadline.tzinfo is None:
            raise ValueError("interaction deadline must be timezone-aware")
        if self.kind is InteractionKind.APPROVAL and self.approval is None:
            raise ValueError("approval interactions require approval details")
        if self.kind is InteractionKind.CLARIFICATION and self.approval is not None:
            raise ValueError("clarifications cannot contain approval details")


@dataclass(frozen=True, slots=True)
class ControlResult:
    code: str
    run_version: int
    run_status: str
    interaction_id: str | None = None
    interaction_version: int | None = None
    deduplicated: bool = False
    resumed_items: tuple[str, ...] = ()
    invocation_states: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "invocation_states", MappingProxyType(dict(self.invocation_states)))


@dataclass(frozen=True, slots=True)
class WaitingView:
    session_id: str
    run_id: str
    run_version: int
    requests: tuple[InteractionRequest, ...]
    runnable_items: tuple[str, ...]
    status: str


@dataclass(frozen=True, slots=True)
class SteerUpdate:
    interaction_id: str
    run_version: int
    text: str


@dataclass(slots=True)
class _InteractionState:
    request: InteractionRequest
    status: InteractionStatus = InteractionStatus.PENDING
    resolution_hash: str | None = None
    resolution: Mapping[str, Any] | None = None
    approval_revoked: bool = False


@dataclass(slots=True)
class _InvocationControl:
    side_effecting: bool
    cancel: Callable[[], CancelDisposition]
    state: CancelDisposition | None = None


@dataclass(slots=True)
class _RunControl:
    tenant_id: str
    user_id: str
    session_id: str
    run_id: str
    version: int
    status: str = RunStatus.RUNNING
    runnable_items: set[str] = field(default_factory=set)
    interactions: dict[str, _InteractionState] = field(default_factory=dict)
    invocations: dict[str, _InvocationControl] = field(default_factory=dict)
    cancellation: threading.Event = field(default_factory=threading.Event)
    controls: dict[str, str] = field(default_factory=dict)
    steers: list[SteerUpdate] = field(default_factory=list)


class InteractionController:
    """High-priority control path that never waits on a model invocation."""

    def __init__(self, durable: DurableSessionPort,
                 policy: RuntimeControlPolicy = RuntimeControlPolicy()) -> None:
        self._durable = durable
        self._policy = policy
        self._runs: dict[tuple[str, str], _RunControl] = {}
        self._lock = threading.RLock()

    @staticmethod
    def classify(command: str, *, active_run: bool) -> InputKind:
        values = {
            "turn.steer": InputKind.STEER,
            "turn.cancel": InputKind.CANCEL,
            "clarification.answer": InputKind.CLARIFICATION_ANSWER,
            "approval.resolve": InputKind.APPROVAL_RESOLUTION,
        }
        if command == "user.input":
            return InputKind.GOAL_SUPPLEMENT if active_run else InputKind.NEW_GOAL
        try:
            return values[command]
        except KeyError as error:
            raise InteractionError("unsupported_input", f"unsupported input command: {command}") from error

    def register_run(self, *, tenant_id: str, user_id: str, session_id: str,
                     run_id: str, version: int = 1,
                     runnable_items: Sequence[str] = ()) -> threading.Event:
        if version < 1:
            raise ValueError("run version must be positive")
        key = (session_id, run_id)
        with self._lock:
            if key in self._runs:
                raise InteractionError("run_already_registered", "run control already exists")
            run = _RunControl(
                tenant_id, user_id, session_id, run_id, version,
                runnable_items=set(runnable_items),
            )
            self._runs[key] = run
            return run.cancellation

    def record_user_input(self, *, session_id: str, interaction_id: str,
                          item_id: str, text: str, run_id: str | None = None,
                          expected_run_version: int | None = None) -> ControlResult:
        if not text.strip():
            raise InteractionError("invalid_input", "user input must be non-empty")
        if run_id is None:
            self._durable.commit(
                session_id=session_id, event_id=f"input:{interaction_id}",
                facts=(SemanticFact(FactKind.INTERACTION, interaction_id,
                                    InteractionStatus.ANSWERED, {
                    "kind": InputKind.NEW_GOAL, "run_id": None, "item_id": item_id,
                    "interaction_version": 1, "text": text,
                }),),
            )
            return ControlResult("input_recorded", 0, "queued", interaction_id, 1)
        with self._lock:
            run = self._require_run(session_id, run_id)
            if expected_run_version is None:
                raise InteractionError("run_version_required", "active-run input requires a run version")
            fingerprint = hashlib.sha256(text.encode()).hexdigest()
            known = run.controls.get(interaction_id)
            if known is not None:
                if known == fingerprint:
                    return ControlResult("input_recorded", run.version, run.status,
                                         interaction_id, 1, True)
                raise InteractionError("interaction_id_conflict", "input id already has different content")
            self._check_run_version(run, expected_run_version)
            run.version += 1
            run.controls[interaction_id] = fingerprint
            self._commit(run, f"input:{interaction_id}", (
                SemanticFact(FactKind.INTERACTION, interaction_id, InteractionStatus.ANSWERED, {
                    "kind": InputKind.GOAL_SUPPLEMENT, "run_id": run_id,
                    "item_id": item_id, "interaction_version": 1,
                    "applies_after_run_version": expected_run_version,
                    "run_version": run.version, "text": text,
                }),
                SemanticFact(FactKind.RUN, run_id, run.status, {"run_version": run.version}),
            ))
            return ControlResult("input_recorded", run.version, run.status, interaction_id, 1)

    def request_clarification(self, *, session_id: str, run_id: str,
                              interaction_id: str, item_id: str, version: int,
                              expected_run_version: int, question: str,
                              options: Sequence[str], reason: str,
                              blocked_items: Sequence[str], timeout_seconds: float,
                              now: datetime | None = None) -> ControlResult:
        if timeout_seconds <= 0:
            raise ValueError("interaction timeout must be positive")
        current = now or datetime.now(timezone.utc)
        request = InteractionRequest(
            interaction_id, session_id, run_id, item_id, version,
            InteractionKind.CLARIFICATION, question, tuple(options), reason,
            tuple(blocked_items), current + timedelta(seconds=timeout_seconds),
        )
        return self._request(request, expected_run_version)

    def request_approval(self, *, session_id: str, run_id: str,
                         interaction_id: str, item_id: str, version: int,
                         expected_run_version: int, tool_name: str,
                         arguments: Mapping[str, Any], arguments_summary: str,
                         risk: str, scope: Sequence[str], reason: str,
                         blocked_items: Sequence[str], timeout_seconds: float,
                         now: datetime | None = None) -> ControlResult:
        if timeout_seconds <= 0:
            raise ValueError("interaction timeout must be positive")
        current = now or datetime.now(timezone.utc)
        details = ApprovalDetails(
            tool_name, arguments_summary, hash_arguments(arguments), risk, tuple(scope),
        )
        request = InteractionRequest(
            interaction_id, session_id, run_id, item_id, version,
            InteractionKind.APPROVAL, f"Approve {tool_name}", ("approve", "deny"), reason,
            tuple(blocked_items), current + timedelta(seconds=timeout_seconds), details,
        )
        return self._request(request, expected_run_version)

    def _request(self, request: InteractionRequest, expected_run_version: int) -> ControlResult:
        with self._lock:
            run = self._require_run(request.session_id, request.run_id)
            self._check_run_version(run, expected_run_version)
            existing = run.interactions.get(request.interaction_id)
            if existing is not None:
                if existing.request == request:
                    return ControlResult(
                        "interaction_requested", run.version, run.status,
                        request.interaction_id, request.version, True,
                    )
                raise InteractionError("interaction_id_conflict", "interaction id already has different content")
            run.interactions[request.interaction_id] = _InteractionState(request)
            run.runnable_items.difference_update(request.blocked_items)
            if not run.runnable_items:
                run.status = RunStatus.WAITING
            facts = [SemanticFact(
                FactKind.INTERACTION, request.interaction_id, InteractionStatus.PENDING,
                _request_payload(request),
            )]
            if request.kind is InteractionKind.APPROVAL:
                assert request.approval is not None
                facts.append(SemanticFact(
                    FactKind.APPROVAL, request.interaction_id, "pending",
                    _approval_payload(request.approval, request.version, request.deadline),
                ))
            facts.append(SemanticFact(
                FactKind.RUN, run.run_id, run.status,
                {"run_version": run.version, "waiting_on": request.interaction_id},
            ))
            self._commit(run, f"interaction:{request.interaction_id}:v{request.version}:requested", facts)
            return ControlResult(
                "interaction_requested", run.version, run.status,
                request.interaction_id, request.version,
            )

    def answer_clarification(self, *, session_id: str, run_id: str,
                             interaction_id: str, interaction_version: int,
                             expected_run_version: int, resolution_id: str,
                             answer: str, now: datetime | None = None) -> ControlResult:
        if not answer.strip():
            raise InteractionError("invalid_resolution", "clarification answer must be non-empty")
        return self._resolve(
            session_id, run_id, interaction_id, interaction_version,
            expected_run_version, resolution_id, {"answer": answer},
            InteractionKind.CLARIFICATION, now or datetime.now(timezone.utc),
        )

    def resolve_approval(self, *, session_id: str, run_id: str,
                         interaction_id: str, interaction_version: int,
                         expected_run_version: int, resolution_id: str,
                         decision: ApprovalDecision | str,
                         now: datetime | None = None) -> ControlResult:
        aliases = {"approve": ApprovalDecision.APPROVED, "deny": ApprovalDecision.DENIED}
        try:
            value = aliases[str(decision)] if str(decision) in aliases else ApprovalDecision(decision)
        except ValueError as error:
            raise InteractionError("invalid_resolution", "approval decision must be approved or denied") from error
        return self._resolve(
            session_id, run_id, interaction_id, interaction_version,
            expected_run_version, resolution_id, {"decision": value},
            InteractionKind.APPROVAL, now or datetime.now(timezone.utc),
        )

    def _resolve(self, session_id: str, run_id: str, interaction_id: str,
                 interaction_version: int, expected_run_version: int,
                 resolution_id: str, resolution: Mapping[str, Any],
                 expected_kind: InteractionKind, now: datetime) -> ControlResult:
        encoded = json.dumps(dict(resolution), sort_keys=True, separators=(",", ":"), default=str)
        resolution_hash = hashlib.sha256(f"{resolution_id}:{encoded}".encode()).hexdigest()
        with self._lock:
            run = self._require_run(session_id, run_id)
            state = self._require_interaction(run, interaction_id, interaction_version, expected_kind)
            if state.resolution_hash is not None:
                if state.resolution_hash == resolution_hash:
                    return ControlResult(
                        "interaction_resolved", run.version, run.status,
                        interaction_id, interaction_version, True,
                    )
                raise InteractionError("resolution_conflict", "interaction already has another resolution")
            self._check_run_version(run, expected_run_version)
            if now >= state.request.deadline:
                self._expire_state(run, state, self._policy.timeout_disposition, now)
                raise InteractionError("interaction_expired", "interaction resolution arrived after its deadline")
            state.status = InteractionStatus.ANSWERED
            state.resolution_hash = resolution_hash
            state.resolution = MappingProxyType(dict(resolution))
            run.version += 1
            run.runnable_items.update(state.request.blocked_items)
            if run.status is RunStatus.WAITING:
                run.status = RunStatus.RUNNING
            payload = {
                "kind": state.request.kind,
                "run_id": run.run_id,
                "item_id": state.request.item_id,
                "interaction_version": interaction_version,
                "resolution_id": resolution_id,
                "resolution_hash": resolution_hash,
                **dict(resolution),
            }
            facts = [SemanticFact(FactKind.INTERACTION, interaction_id, InteractionStatus.ANSWERED, payload)]
            if expected_kind is InteractionKind.APPROVAL:
                decision = str(resolution["decision"])
                facts.append(SemanticFact(FactKind.APPROVAL, interaction_id, decision, payload))
                if decision == ApprovalDecision.DENIED:
                    facts.append(SemanticFact(
                        FactKind.OUTCOME, state.request.item_id, "rejected",
                        {"error_code": "approval_denied", "interaction_id": interaction_id},
                    ))
            facts.append(SemanticFact(
                FactKind.RUN, run.run_id, run.status,
                {"run_version": run.version, "resumed_items": list(state.request.blocked_items)},
            ))
            self._commit(run, f"interaction:{interaction_id}:v{interaction_version}:resolved", facts)
            return ControlResult(
                "interaction_resolved", run.version, run.status,
                interaction_id, interaction_version,
                resumed_items=state.request.blocked_items,
            )

    def approval_snapshot(self, *, session_id: str, run_id: str,
                          interaction_id: str, interaction_version: int,
                          arguments: Mapping[str, Any], now: datetime | None = None) -> ApprovalSnapshot:
        current = now or datetime.now(timezone.utc)
        with self._lock:
            run = self._require_run(session_id, run_id)
            state = self._require_interaction(
                run, interaction_id, interaction_version, InteractionKind.APPROVAL,
            )
            details = state.request.approval
            assert details is not None
            if state.status is not InteractionStatus.ANSWERED or state.approval_revoked:
                raise InteractionError("approval_not_active", "approval is not active")
            if current >= state.request.deadline:
                raise InteractionError("approval_expired", "approval has expired")
            if state.resolution is None or state.resolution.get("decision") != ApprovalDecision.APPROVED:
                raise InteractionError("approval_denied", "approval was denied")
            if hash_arguments(arguments) != details.arguments_hash:
                raise InteractionError("approval_arguments_changed", "tool arguments changed after approval")
            return ApprovalSnapshot(
                interaction_id, run.tenant_id, run.session_id, details.tool_name,
                details.arguments_hash, state.request.deadline, True,
                interaction_version, details.scope,
            )

    def revoke_approval(self, *, session_id: str, run_id: str,
                        interaction_id: str, interaction_version: int,
                        expected_run_version: int, revocation_id: str) -> ControlResult:
        with self._lock:
            run = self._require_run(session_id, run_id)
            self._check_run_version(run, expected_run_version)
            state = self._require_interaction(
                run, interaction_id, interaction_version, InteractionKind.APPROVAL,
            )
            fingerprint = f"revoke:{revocation_id}"
            known = run.controls.get(fingerprint)
            if known is not None:
                return ControlResult("approval_revoked", run.version, run.status,
                                     interaction_id, interaction_version, True)
            if state.approval_revoked:
                raise InteractionError("approval_not_active", "approval was already revoked")
            if (state.status is not InteractionStatus.ANSWERED or state.resolution is None
                    or state.resolution.get("decision") != ApprovalDecision.APPROVED):
                raise InteractionError("approval_not_active", "only an active approval can be revoked")
            state.approval_revoked = True
            run.version += 1
            run.controls[fingerprint] = interaction_id
            self._commit(run, f"approval:{interaction_id}:v{interaction_version}:revoked", (
                SemanticFact(FactKind.APPROVAL, interaction_id, "revoked", {
                    "interaction_version": interaction_version,
                    "revocation_id": revocation_id,
                }),
                SemanticFact(FactKind.OUTCOME, state.request.item_id, "rejected", {
                    "error_code": "approval_revoked", "interaction_id": interaction_id,
                }),
            ))
            return ControlResult("approval_revoked", run.version, run.status,
                                 interaction_id, interaction_version)

    def steer(self, *, session_id: str, run_id: str, interaction_id: str,
              expected_run_version: int, text: str) -> ControlResult:
        if not text.strip():
            raise InteractionError("invalid_steer", "steer text must be non-empty")
        fingerprint = hashlib.sha256(text.encode()).hexdigest()
        with self._lock:
            run = self._require_run(session_id, run_id)
            known = run.controls.get(interaction_id)
            if known is not None:
                if known == fingerprint:
                    return ControlResult("steer_applied", run.version, run.status,
                                         interaction_id, deduplicated=True)
                raise InteractionError("interaction_id_conflict", "steer id already has different content")
            self._check_run_version(run, expected_run_version)
            run.version += 1
            run.controls[interaction_id] = fingerprint
            run.steers.append(SteerUpdate(interaction_id, run.version, text))
            self._commit(run, f"steer:{interaction_id}", (
                SemanticFact(FactKind.INTERACTION, interaction_id, InteractionStatus.ANSWERED, {
                    "kind": InputKind.STEER, "run_id": run_id,
                    "applies_after_run_version": expected_run_version,
                    "run_version": run.version, "text": text,
                }),
                SemanticFact(FactKind.RUN, run_id, run.status, {
                    "run_version": run.version, "steer_interaction_id": interaction_id,
                }),
            ))
            return ControlResult("steer_applied", run.version, run.status, interaction_id)

    def steer_updates(self, *, session_id: str, run_id: str,
                      after_run_version: int = 0) -> tuple[SteerUpdate, ...]:
        with self._lock:
            run = self._require_run(session_id, run_id)
            return tuple(item for item in run.steers if item.run_version > after_run_version)

    def register_invocation(self, *, session_id: str, run_id: str, invocation_id: str,
                            side_effecting: bool,
                            cancel: Callable[[], CancelDisposition]) -> None:
        with self._lock:
            run = self._require_run(session_id, run_id)
            if invocation_id in run.invocations:
                raise InteractionError("invocation_already_registered", "invocation control already exists")
            run.invocations[invocation_id] = _InvocationControl(side_effecting, cancel)

    def request_cancel(self, *, session_id: str, run_id: str, interaction_id: str,
                       expected_run_version: int) -> ControlResult:
        with self._lock:
            run = self._require_run(session_id, run_id)
            known = run.controls.get(interaction_id)
            if known == InputKind.CANCEL:
                return ControlResult(
                    "cancel_requested", run.version, run.status, interaction_id,
                    deduplicated=True,
                )
            self._check_run_version(run, expected_run_version)
            run.version += 1
            run.controls[interaction_id] = InputKind.CANCEL
            run.cancellation.set()
            run.status = "cancel_requested"
            states: dict[str, str] = {}
            facts: list[SemanticFact] = [
                SemanticFact(FactKind.INTERACTION, interaction_id, InteractionStatus.ANSWERED, {
                    "kind": InputKind.CANCEL, "run_id": run_id, "run_version": run.version,
                }),
                SemanticFact(FactKind.RUN, run_id, "cancel_requested", {"run_version": run.version}),
            ]
            for invocation_id, control in run.invocations.items():
                try:
                    disposition = CancelDisposition(control.cancel())
                except Exception:
                    disposition = CancelDisposition.UNKNOWN if control.side_effecting else CancelDisposition.PENDING
                if disposition is CancelDisposition.PENDING and control.side_effecting:
                    disposition = CancelDisposition.UNKNOWN
                control.state = disposition
                states[invocation_id] = disposition
                invocation_status = {
                    CancelDisposition.CANCELLED: InvocationStatus.CANCELLED,
                    CancelDisposition.PENDING: InvocationStatus.CANCEL_REQUESTED,
                    CancelDisposition.UNKNOWN: InvocationStatus.UNKNOWN,
                }[disposition]
                facts.append(SemanticFact(FactKind.INVOCATION, invocation_id, invocation_status, {
                    "cancel_interaction_id": interaction_id,
                    "replay_allowed": False if disposition is CancelDisposition.UNKNOWN else None,
                }))
                facts.append(SemanticFact(FactKind.OUTCOME, invocation_id, "observed", {
                    "execution_status": invocation_status,
                    "business_success": None,
                    "error_code": f"cancel_{disposition}",
                }))
            if all(value in {CancelDisposition.CANCELLED, CancelDisposition.UNKNOWN} for value in states.values()):
                run.status = RunStatus.CANCELLED
                facts.append(SemanticFact(FactKind.RUN, run_id, RunStatus.CANCELLED, {
                    "run_version": run.version, "unknown_invocations": [
                        key for key, value in states.items() if value is CancelDisposition.UNKNOWN
                    ],
                }))
            self._commit(run, f"cancel:{interaction_id}", facts)
            return ControlResult(
                "cancel_requested", run.version, run.status, interaction_id,
                invocation_states=states,
            )

    def acknowledge_cancellation(self, *, session_id: str, run_id: str,
                                 invocation_id: str,
                                 disposition: CancelDisposition | str) -> ControlResult:
        try:
            value = CancelDisposition(disposition)
        except ValueError as error:
            raise InteractionError("invalid_cancel_ack", "cancel acknowledgement is invalid") from error
        if value is CancelDisposition.PENDING:
            raise InteractionError("invalid_cancel_ack", "acknowledgement must be terminal")
        with self._lock:
            run = self._require_run(session_id, run_id)
            control = run.invocations.get(invocation_id)
            if control is None:
                raise InteractionError("invocation_not_found", "invocation is not controlled by this run")
            if not run.cancellation.is_set():
                raise InteractionError("cancel_not_requested", "run cancellation was not requested")
            if control.state in {CancelDisposition.CANCELLED, CancelDisposition.UNKNOWN}:
                if control.state is value:
                    return ControlResult(
                        "cancel_acknowledged", run.version, run.status,
                        invocation_states={invocation_id: value}, deduplicated=True,
                    )
                raise InteractionError("cancel_ack_conflict", "invocation already has another terminal result")
            control.state = value
            run.version += 1
            if all(item.state in {CancelDisposition.CANCELLED, CancelDisposition.UNKNOWN}
                   for item in run.invocations.values()):
                run.status = RunStatus.CANCELLED
            invocation_status = (
                InvocationStatus.CANCELLED if value is CancelDisposition.CANCELLED
                else InvocationStatus.UNKNOWN
            )
            self._commit(run, f"cancel:{invocation_id}:ack", (
                SemanticFact(FactKind.INVOCATION, invocation_id, invocation_status, {
                    "replay_allowed": False if value is CancelDisposition.UNKNOWN else None,
                }),
                SemanticFact(FactKind.OUTCOME, invocation_id, "observed", {
                    "execution_status": invocation_status, "business_success": None,
                    "error_code": f"cancel_{value}",
                }),
                SemanticFact(FactKind.RUN, run_id, run.status, {"run_version": run.version}),
            ))
            return ControlResult(
                "cancel_acknowledged", run.version, run.status,
                invocation_states={invocation_id: value},
            )

    def expire(self, *, now: datetime | None = None) -> tuple[ControlResult, ...]:
        current = now or datetime.now(timezone.utc)
        results: list[ControlResult] = []
        with self._lock:
            for run in self._runs.values():
                for state in run.interactions.values():
                    if state.status is InteractionStatus.PENDING and current >= state.request.deadline:
                        self._expire_state(run, state, self._policy.timeout_disposition, current)
                        results.append(ControlResult(
                            "interaction_expired", run.version, run.status,
                            state.request.interaction_id, state.request.version,
                        ))
        return tuple(results)

    def _expire_state(self, run: _RunControl, state: _InteractionState,
                      disposition: TimeoutDisposition, now: datetime) -> None:
        state.status = InteractionStatus.EXPIRED
        run.version += 1
        if disposition is TimeoutDisposition.CANCEL:
            run.cancellation.set()
            run.status = "cancel_requested"
        else:
            run.status = RunStatus.BLOCKED
        facts: list[SemanticFact] = [
            SemanticFact(FactKind.INTERACTION, state.request.interaction_id,
                         InteractionStatus.EXPIRED, {
                             "kind": state.request.kind,
                             "run_id": run.run_id,
                             "item_id": state.request.item_id,
                             "interaction_version": state.request.version,
                             "expired_at": _timestamp(now), "timeout_disposition": disposition,
                         }),
            SemanticFact(FactKind.RUN, run.run_id, run.status, {
                "run_version": run.version, "timeout_disposition": disposition,
            }),
        ]
        if state.request.kind is InteractionKind.APPROVAL:
            facts.extend((
                SemanticFact(FactKind.APPROVAL, state.request.interaction_id, "expired", {
                    "interaction_version": state.request.version,
                }),
                SemanticFact(FactKind.OUTCOME, state.request.item_id, "rejected", {
                    "error_code": "approval_expired",
                }),
            ))
        if disposition is TimeoutDisposition.CANCEL:
            pending = False
            for invocation_id, control in run.invocations.items():
                try:
                    cancel_state = CancelDisposition(control.cancel())
                except Exception:
                    cancel_state = (CancelDisposition.UNKNOWN if control.side_effecting
                                    else CancelDisposition.PENDING)
                if cancel_state is CancelDisposition.PENDING and control.side_effecting:
                    cancel_state = CancelDisposition.UNKNOWN
                control.state = cancel_state
                pending = pending or cancel_state is CancelDisposition.PENDING
                invocation_status = {
                    CancelDisposition.CANCELLED: InvocationStatus.CANCELLED,
                    CancelDisposition.PENDING: InvocationStatus.CANCEL_REQUESTED,
                    CancelDisposition.UNKNOWN: InvocationStatus.UNKNOWN,
                }[cancel_state]
                facts.extend((
                    SemanticFact(FactKind.INVOCATION, invocation_id, invocation_status, {
                        "timeout_interaction_id": state.request.interaction_id,
                        "replay_allowed": False if cancel_state is CancelDisposition.UNKNOWN else None,
                    }),
                    SemanticFact(FactKind.OUTCOME, invocation_id, "observed", {
                        "execution_status": invocation_status, "business_success": None,
                        "error_code": f"timeout_cancel_{cancel_state}",
                    }),
                ))
            if not pending:
                run.status = RunStatus.CANCELLED
                facts.append(SemanticFact(FactKind.RUN, run.run_id, RunStatus.CANCELLED, {
                    "run_version": run.version, "timeout_disposition": disposition,
                }))
        if disposition is TimeoutDisposition.PARTIAL:
            facts.append(SemanticFact(FactKind.DELIVERY, run.run_id, "partial", {
                "goal_complete": False,
                "unresolved": [f"interaction {state.request.interaction_id} expired"],
            }))
        self._commit(
            run,
            f"interaction:{state.request.interaction_id}:v{state.request.version}:expired",
            facts,
        )

    def waiting(self, *, session_id: str, run_id: str) -> WaitingView:
        with self._lock:
            run = self._require_run(session_id, run_id)
            requests = tuple(
                state.request for state in run.interactions.values()
                if state.status is InteractionStatus.PENDING
            )
            return WaitingView(
                session_id, run_id, run.version, requests,
                tuple(sorted(run.runnable_items)), run.status,
            )

    def cancellation_event(self, *, session_id: str, run_id: str) -> threading.Event:
        with self._lock:
            return self._require_run(session_id, run_id).cancellation

    def recover_waiting(self, *, session_id: str, run_id: str) -> WaitingView:
        latest: dict[str, Any] = {}
        run_version = 0
        run_status = RunStatus.INTERRUPTED
        for stored in self._durable.load(session_id):
            fact = stored.fact
            if fact.kind is FactKind.RUN and fact.entity_id == run_id:
                run_status = fact.state
                run_version = max(run_version, int(fact.payload.get("run_version", 0)))
            if fact.kind is FactKind.INTERACTION and fact.payload.get("run_id") == run_id:
                latest[fact.entity_id] = fact
        requests = tuple(
            _request_from_payload(fact.payload) for fact in latest.values()
            if fact.state == InteractionStatus.PENDING
        )
        return WaitingView(session_id, run_id, run_version, requests, (), run_status)

    def restore_waiting(self, *, tenant_id: str, user_id: str,
                        session_id: str, run_id: str,
                        now: datetime | None = None) -> WaitingView:
        """Restore durable pending interactions into a new process control runtime."""
        current = now or datetime.now(timezone.utc)
        view = self.recover_waiting(session_id=session_id, run_id=run_id)
        if view.status != RunStatus.WAITING:
            raise InteractionError("run_not_waiting", "only a durable waiting run can restore interactions")
        if not view.requests:
            raise InteractionError("interaction_not_found", "run has no durable pending interaction")
        key = (session_id, run_id)
        with self._lock:
            if key in self._runs:
                raise InteractionError("run_already_registered", "run control already exists")
            run = _RunControl(
                tenant_id, user_id, session_id, run_id, max(1, view.run_version),
                status=RunStatus.WAITING,
            )
            run.interactions = {
                request.interaction_id: _InteractionState(request)
                for request in view.requests
            }
            self._runs[key] = run
            for state in tuple(run.interactions.values()):
                if current >= state.request.deadline:
                    self._expire_state(run, state, self._policy.timeout_disposition, current)
            return self.waiting(session_id=session_id, run_id=run_id)

    def handle_disconnect(self, *, session_id: str, run_id: str, source: str,
                          interaction_id: str, expected_run_version: int) -> ControlResult:
        if source == "frontend":
            disposition = self._policy.frontend_disconnect
        elif source == "host":
            disposition = self._policy.host_disconnect
        else:
            raise InteractionError("invalid_disconnect_source", "disconnect source must be frontend or host")
        if disposition is DisconnectDisposition.CANCEL:
            return self.request_cancel(
                session_id=session_id, run_id=run_id,
                interaction_id=interaction_id, expected_run_version=expected_run_version,
            )
        with self._lock:
            run = self._require_run(session_id, run_id)
            self._check_run_version(run, expected_run_version)
            run.version += 1
            if disposition is DisconnectDisposition.BLOCK:
                run.status = RunStatus.BLOCKED
            self._commit(run, f"disconnect:{interaction_id}", (
                SemanticFact(FactKind.OBSERVATION, interaction_id, disposition, {
                    "source": source, "background_allowed": self._policy.background_allowed,
                    "run_version": run.version,
                }),
                SemanticFact(FactKind.RUN, run_id, run.status, {"run_version": run.version}),
            ))
            return ControlResult(f"disconnect_{disposition}", run.version, run.status, interaction_id)

    def _require_run(self, session_id: str, run_id: str) -> _RunControl:
        try:
            return self._runs[(session_id, run_id)]
        except KeyError as error:
            raise InteractionError("run_not_active", "run is not registered in the control runtime") from error

    @staticmethod
    def _check_run_version(run: _RunControl, expected: int) -> None:
        if expected != run.version:
            raise InteractionError("stale_run_version", "control targets a stale run version")

    @staticmethod
    def _require_interaction(run: _RunControl, interaction_id: str, version: int,
                             kind: InteractionKind) -> _InteractionState:
        state = run.interactions.get(interaction_id)
        if state is None:
            raise InteractionError("interaction_not_found", "interaction does not belong to this run")
        if state.request.version != version:
            raise InteractionError("stale_interaction_version", "interaction version is stale")
        if state.request.kind is not kind:
            raise InteractionError("interaction_kind_mismatch", "resolution has the wrong interaction kind")
        return state

    def _commit(self, run: _RunControl, event_id: str,
                facts: Sequence[SemanticFact]) -> None:
        self._durable.commit(session_id=run.session_id, event_id=event_id, facts=facts)


def _request_payload(request: InteractionRequest) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "kind": request.kind, "interaction_id": request.interaction_id,
        "session_id": request.session_id,
        "run_id": request.run_id, "item_id": request.item_id,
        "interaction_version": request.version, "prompt": request.prompt,
        "options": list(request.options), "reason": request.reason,
        "blocked_items": list(request.blocked_items),
        "deadline": _timestamp(request.deadline),
    }
    if request.approval is not None:
        payload["approval"] = _approval_payload(
            request.approval, request.version, request.deadline,
        )
    return payload


def _approval_payload(details: ApprovalDetails, version: int,
                      deadline: datetime) -> dict[str, Any]:
    return {
        "tool_name": details.tool_name,
        "arguments_summary": details.arguments_summary,
        "arguments_hash": details.arguments_hash,
        "risk": details.risk, "scope": list(details.scope),
        "interaction_version": version, "expires_at": _timestamp(deadline),
    }


def _request_from_payload(payload: Mapping[str, Any]) -> InteractionRequest:
    approval_value = payload.get("approval")
    approval = None
    if isinstance(approval_value, Mapping):
        approval = ApprovalDetails(
            str(approval_value["tool_name"]), str(approval_value["arguments_summary"]),
            str(approval_value["arguments_hash"]), str(approval_value["risk"]),
            tuple(str(item) for item in approval_value.get("scope", ())),
        )
    return InteractionRequest(
        interaction_id=str(payload.get("interaction_id") or "recovered"),
        session_id=str(payload["session_id"]), run_id=str(payload["run_id"]),
        item_id=str(payload["item_id"]), version=int(payload["interaction_version"]),
        kind=InteractionKind(payload["kind"]), prompt=str(payload["prompt"]),
        options=tuple(str(item) for item in payload.get("options", ())),
        reason=str(payload["reason"]),
        blocked_items=tuple(str(item) for item in payload.get("blocked_items", ())),
        deadline=datetime.fromisoformat(str(payload["deadline"]).replace("Z", "+00:00")),
        approval=approval,
    )


def _timestamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
