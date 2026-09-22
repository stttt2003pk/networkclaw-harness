"""Production host adapter whose sole loop owner is vendored Hermes ``AIAgent``."""

from __future__ import annotations

import os
import json
import inspect
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, MutableSequence, Sequence
from urllib.parse import urlparse

from networkclaw_harness.protocol import InputFrame
from networkclaw_harness.workspace import SessionWorkspace

from .hermes_bootstrap import HermesRuntimeBootstrap, open_hermes_runtime
from .delegation import DelegationError, DelegationGrant, HostDelegationBroker
from .hermes_native_probe import configure_headless_agent, load_agent_class
from .provider_selection import ProviderSelection, ProviderSelectionError, load_selection
from .models import FactKind, SemanticFact


class HermesHostAdapterError(RuntimeError):
    """A native session cannot be admitted without violating the host contract."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


Emit = Callable[[str, Mapping[str, Any]], None]


@dataclass(slots=True)
class _EventBridge:
    emit: Emit | None = None
    buffered: MutableSequence[tuple[str, Mapping[str, Any]]] = field(default_factory=list)
    delta_count: int = 0
    plan_recorder: Callable[..., Mapping[str, Any] | None] | None = None
    lock: threading.RLock = field(default_factory=threading.RLock)
    generation: int = 0
    fenced: bool = False

    def begin(self, emit: Emit | None, *, generation: int = 0) -> None:
        with self.lock:
            self.emit = emit
            self.buffered.clear()
            self.delta_count = 0
            self.generation = generation

    def publish(self, event_type: str, payload: Mapping[str, Any]) -> None:
        item = (event_type, dict(payload))
        with self.lock:
            emit = self.emit
            if self.fenced:
                return
            if emit is None:
                self.buffered.append(item)
                return
        emit(*item)

    def is_current(self, generation: int) -> bool:
        with self.lock:
            return not self.fenced and self.generation == generation

    def callbacks_for(self, generation: int) -> dict[str, Callable[..., None]]:
        """Bind native callback sinks to one turn generation.

        Hermes may deliver a late callback after interruption or replacement.  The wrapper
        prevents that callback from being attributed to a newer turn using the shared bridge.
        """
        def current(callback: Callable[..., None]) -> Callable[..., None]:
            def guarded(*args: Any, **kwargs: Any) -> None:
                if self.is_current(generation):
                    callback(*args, **kwargs)
            return guarded
        return {
            "stream_delta_callback": current(self.on_delta),
            "tool_complete_callback": current(self.on_tool_complete),
            "tool_progress_callback": current(self.on_tool_progress),
        }

    def on_delta(self, value: Any, *args: Any, **kwargs: Any) -> None:
        if not isinstance(value, str) or not value:
            return
        self.delta_count += 1
        self.publish("assistant.delta", {
            "content": value,
            "final": False,
            "source": "hermes",
            "sequence": self.delta_count,
        })

    def on_tool_complete(self, invocation_id: Any, name: Any, arguments: Any,
                         result: Any, *args: Any, **kwargs: Any) -> None:
        if str(name) != "todo_list" or self.plan_recorder is None:
            return
        plan = self.plan_recorder(invocation_id=str(invocation_id or "todo"), result=result)
        if plan is not None:
            self.publish("plan.updated", plan)

    def on_tool_progress(self, event_type: Any, name: Any = None, preview: Any = None,
                         arguments: Any = None, *args: Any, **kwargs: Any) -> None:
        kind = str(event_type or "")
        if not kind.startswith("subagent."):
            return
        terminal = kind in {"subagent.complete", "subagent.completed", "subagent.failed", "subagent.interrupted"}
        payload = {
            "status": str(kwargs.get("status") or ("completed" if terminal else "running")),
            "subagent_id": str(kwargs.get("subagent_id") or kwargs.get("child_subagent_id") or "")[:160],
            "parent_id": str(kwargs.get("parent_id") or "")[:160],
            "depth": int(kwargs.get("depth") or 0),
        }
        self.publish("subagent.completed" if terminal else "subagent.started", payload)

    def callbacks(self) -> dict[str, Callable[..., None]]:
        return {
            "stream_delta_callback": self.on_delta,
            "tool_complete_callback": self.on_tool_complete,
            "tool_progress_callback": self.on_tool_progress,
        }


class _LineageBridge:
    """Bounded child-event projection; child never writes through parent state."""

    def __init__(self, parent: _EventBridge, *, parent_session_id: str,
                 parent_turn_id: str | None, parent_generation: int,
                 child_session_id: str) -> None:
        self._parent = parent
        self._lineage = {
            "parent_session_id": parent_session_id[:160],
            "parent_turn_id": (parent_turn_id or "")[:160],
            "parent_generation": max(0, int(parent_generation)),
            "child_session_id": child_session_id[:160],
        }

    def publish(self, event_type: str, payload: Mapping[str, Any]) -> None:
        bounded = dict(payload)
        bounded["lineage"] = dict(self._lineage)
        self._parent.publish(event_type, bounded)


@dataclass(slots=True)
class _HermesSession:
    workspace: SessionWorkspace
    runtime: HermesRuntimeBootstrap
    tool_session: Any
    bridge: _EventBridge = field(default_factory=_EventBridge)
    # SessionBinding is durable identity plus host-owned resources.  The agent is
    # deliberately only a cache entry: evicting it must not evict this object.
    agent: Any = None
    route: tuple[str, ...] | None = None
    agent_generation: int = 0
    agent_last_used: float = 0.0
    turn_generation: int = 0
    fenced: bool = False
    execution_epoch: int = 0
    lease_version: int = 0
    admission: threading.Lock = field(default_factory=threading.Lock)
    state_lock: threading.RLock = field(default_factory=threading.RLock)
    broker: HostDelegationBroker | None = None
    active_turn_id: str | None = None
    active_run_id: str | None = None
    active_request_id: str | None = None
    interrupt_reason_code: str | None = None
    pending_cancels: dict[tuple[str, str], str] = field(default_factory=dict)
    child_resources: dict[str, tuple[HermesRuntimeBootstrap, Any]] = field(default_factory=dict)
    tool_signature: tuple[str, ...] = ("networkclaw", "todo", "delegation")
    grant_profile_signature: str = ""
    host_grant: Mapping[str, Any] | None = None

    @property
    def session_id(self) -> str:
        return self.workspace.session_id


# Public names make the ownership model explicit for host integrations without
# exposing the internal cache implementation as the source of truth.
SessionBinding = _HermesSession


@dataclass(frozen=True, slots=True)
class AgentCacheEntry:
    agent: Any
    route: tuple[str, ...]
    generation: int
    last_used: float


class HermesHostAdapter:
    """Translate protocol-v1 sessions and turns to one native Hermes loop per session."""

    def __init__(
        self,
        *,
        environ: Mapping[str, str] | None = None,
        agent_factory: Callable[..., Any] | None = None,
        runtime_factory: Callable[[SessionWorkspace], HermesRuntimeBootstrap] = open_hermes_runtime,
    ) -> None:
        self._env = dict(os.environ if environ is None else environ)
        self._agent_factory = agent_factory
        self._runtime_factory = runtime_factory
        self._sessions: dict[str, _HermesSession] = {}
        self._lock = threading.RLock()

    def open(self, *, session_id: str, tenant_id: str, user_id: str,
             workspace: SessionWorkspace,
             lease: Mapping[str, Any],
             host_grant: Mapping[str, Any] | None = None) -> None:
        from .hermes_tools import HermesHostToolSession, bind_hermes_host_tools

        if host_grant is not None:
            if host_grant.get("session_id") != session_id or host_grant.get("execution_epoch") != lease.get("execution_epoch"):
                raise HermesHostAdapterError("host_grant_identity_invalid", "host grant does not bind the session")
            if Path(str(host_grant.get("workspace") or "")).resolve() != workspace.root:
                raise HermesHostAdapterError("host_grant_workspace_invalid", "host grant workspace does not match session workspace")
        runtime = self._runtime_factory(workspace)
        bridge = _EventBridge()
        try:
            tool_session = HermesHostToolSession(
                tenant_id=tenant_id, user_id=user_id, workspace=workspace, lease=lease,
                bridge=bridge,
                profile_name=self._env.get("NETWORKCLAW_HARNESS_PROFILE", "development"),
            )
        except Exception:
            runtime.close()
            raise
        bridge.plan_recorder = tool_session.record_plan
        replacement = _HermesSession(
            workspace, runtime, tool_session, bridge,
            execution_epoch=int(lease.get("execution_epoch") or 0),
            lease_version=int(lease.get("lease_version") or 0),
            tool_signature=tuple(sorted(str(item) for item in (host_grant or {}).get(
                "allowed_tools", ("networkclaw", "todo", "delegation")))),
            grant_profile_signature=_stable_signature((host_grant or {}).get("resource_profile")),
            host_grant=dict(host_grant) if host_grant is not None else None,
        )
        replacement.broker = HostDelegationBroker(
            session_id=session_id,
            emit=bridge.publish,
            timeout_seconds=float(self._env.get("NETWORKCLAW_DELEGATION_ALLOCATION_TIMEOUT_SECONDS", "30")),
        )
        bind_hermes_host_tools(session_id, tool_session)
        with self._lock:
            previous = self._sessions.pop(session_id, None)
            self._sessions[session_id] = replacement
        if previous is not None:
            self._close_state(previous)

    def update_lease(self, session_id: str, lease: Mapping[str, Any]) -> None:
        with self._lock:
            state = self._sessions.get(session_id)
        if state is not None:
            with state.state_lock:
                epoch = int(lease.get("execution_epoch") or 0)
                version = int(lease.get("lease_version") or 0)
                if epoch < state.execution_epoch or (epoch == state.execution_epoch and version < state.lease_version):
                    return
                epoch_changed = epoch != state.execution_epoch
                if epoch_changed:
                    state.fenced = True
                    state.bridge.fenced = True
                    old_agent = state.agent
                    if old_agent is not None:
                        interrupt = getattr(old_agent, "hard_interrupt", None) or getattr(old_agent, "interrupt", None)
                        if callable(interrupt):
                            try:
                                interrupt("execution epoch replaced", tool_reason="execution epoch replaced")
                            except TypeError:
                                interrupt()
                        if state.active_turn_id is None:
                            close = getattr(old_agent, "close", None)
                            if callable(close):
                                close()
                            state.agent = None
                            state.route = None
                state.tool_session.update_lease(lease)
                if not epoch_changed or state.active_turn_id is None:
                    state.fenced = False
                    state.bridge.fenced = False
                state.execution_epoch = epoch
                state.lease_version = version

    def fence(self, session_id: str, reason: str = "lease_lost") -> None:
        """Fence a binding and interrupt its native turn exactly once."""
        with self._lock:
            state = self._sessions.get(session_id)
        if state is None:
            return
        with state.state_lock:
            if state.fenced:
                return
            state.fenced = True
            state.bridge.fenced = True
            state.interrupt_reason_code = _reason_code(reason)
            agent = state.agent
        if agent is not None:
            interrupt = getattr(agent, "hard_interrupt", None) or getattr(agent, "interrupt", None)
            if callable(interrupt):
                try:
                    interrupt(reason, tool_reason=reason)
                except TypeError:
                    interrupt()

    def close(self, session_id: str) -> None:
        with self._lock:
            state = self._sessions.pop(session_id, None)
        if state is not None:
            self._close_state(state)

    def evict_agent(self, session_id: str) -> bool:
        """Drop only the in-memory AIAgent cache entry; durable session remains open."""
        with self._lock:
            state = self._sessions.get(session_id)
        if state is None:
            return False
        with state.state_lock:
            if state.active_turn_id is not None:
                return False
            had_agent = state.agent is not None
            self._evict_agent(state)
            return had_agent

    def cache_entry(self, session_id: str) -> AgentCacheEntry | None:
        with self._lock:
            state = self._sessions.get(session_id)
        if state is None or state.agent is None or state.route is None:
            return None
        return AgentCacheEntry(state.agent, state.route, state.agent_generation, state.agent_last_used)

    def host_disconnected(self, session_ids: Sequence[str]) -> None:
        for session_id in session_ids:
            self.close(session_id)

    def handle(self, frame: InputFrame) -> Sequence[tuple[str, Mapping[str, Any]]]:
        return self._handle(frame, None)

    def handle_stream(self, frame: InputFrame, emit: Emit) -> Sequence[tuple[str, Mapping[str, Any]]]:
        return self._handle(frame, emit)

    def _handle(self, frame: InputFrame, emit: Emit | None) -> Sequence[tuple[str, Mapping[str, Any]]]:
        if frame.session_id is None:
            return (("error", {"code": "invalid_identity", "message": "session_id is required"}),)
        with self._lock:
            state = self._sessions.get(frame.session_id)
        if state is None:
            return (("turn.failed", {"code": "session_not_open", "retryable": False}),)
        if frame.type == "turn.cancel":
            target_request_id = _payload_string(frame.payload, "target_request_id")
            target_turn_id = _payload_string(frame.payload, "target_turn_id") or frame.turn_id
            target_epoch = _payload_int(frame.payload, "execution_epoch")
            reason_code = _reason_code(_payload_string(frame.payload, "reason_code") or "user_cancel")
            if target_epoch is not None and target_epoch != state.execution_epoch:
                return (('turn.controlled', {"command": frame.type, "state": "stale_epoch", "reason_code": reason_code}),)
            if state.active_turn_id is not None and (
                target_turn_id != state.active_turn_id or frame.run_id != state.active_run_id
            ):
                return (("turn.controlled", {"command": frame.type, "state": "stale_turn"}),)
            if target_request_id and state.active_request_id and target_request_id != state.active_request_id:
                return (("turn.controlled", {"command": frame.type, "state": "stale_request"}),)
            if state.active_turn_id is None:
                if target_request_id and target_turn_id and frame.run_id:
                    with state.state_lock:
                        state.pending_cancels[(target_turn_id, frame.run_id)] = reason_code
                    return (("turn.controlled", {"command": frame.type, "state": "queued", "reason_code": reason_code}),)
                return (("turn.controlled", {"command": frame.type, "state": "idle", "reason_code": reason_code}),)
            interrupted = False
            with state.state_lock:
                state.interrupt_reason_code = reason_code
            if state.agent is not None and state.active_turn_id is not None:
                hard_interrupt = getattr(state.agent, "hard_interrupt", None)
                if callable(hard_interrupt):
                    hard_interrupt(reason_code, tool_reason=reason_code)
                    interrupted = True
                else:
                    interrupted = bool(state.agent.interrupt())
            return (("turn.controlled", {"command": frame.type,
                                          "state": "cancel_requested" if interrupted else "idle",
                                          "reason_code": reason_code}),)
        if frame.type == "turn.steer":
            if state.active_turn_id is not None and (
                frame.turn_id != state.active_turn_id or frame.run_id != state.active_run_id
            ):
                return (("turn.controlled", {"command": frame.type, "state": "stale_turn"}),)
            text = str(frame.payload.get("text") or "").strip()
            redirect = getattr(state.agent, "redirect", None) if state.agent is not None else None
            accepted = False
            if text and state.agent is not None and callable(redirect):
                deadline = time.monotonic() + 0.5
                while not accepted and state.active_turn_id is not None and time.monotonic() < deadline:
                    accepted = bool(redirect(text))
                    if not accepted:
                        time.sleep(0.01)
            if text and state.agent is not None and not accepted:
                accepted = bool(state.agent.steer(text))
            return (("turn.controlled", {"command": frame.type,
                                          "state": "accepted" if accepted else "rejected"}),)
        if frame.type == "delegation.resolve":
            allocation_id = frame.interaction_id or str(frame.payload.get("allocation_id") or "")
            try:
                if state.broker is None:
                    raise DelegationError("delegation_broker_closed", "delegation broker is unavailable")
                grant = state.broker.resolve(allocation_id, frame.payload)
            except DelegationError as error:
                return (("turn.controlled", {
                    "command": frame.type, "state": "rejected", "error_code": error.code,
                }),)
            return (("turn.controlled", {
                "command": frame.type,
                "state": "granted" if grant is not None else "denied",
                "allocation_id": allocation_id,
            }),)
        if frame.type != "user.input":
            return (("turn.failed", {"code": "unsupported_command", "retryable": False}),)
        return self._run_turn(state, frame, emit)

    def _run_turn(self, state: _HermesSession, frame: InputFrame,
                  emit: Emit | None) -> Sequence[tuple[str, Mapping[str, Any]]]:
        text = str(frame.payload.get("text") or "").strip()
        if not text:
            return (("turn.failed", {"code": "invalid_request", "retryable": False}),)
        try:
            selection = load_selection(frame.payload, self._env)
            provider = self._provider_config(selection)
            turn_params = _validated_turn_parameters(frame.payload, state.host_grant)
        except (ProviderSelectionError, HermesHostAdapterError) as error:
            return (("turn.failed", {
                "code": error.code,
                "retryable": False,
                "next_actions": ["correct the authorized provider route and start a new turn"],
            }),)

        if not state.admission.acquire(blocking=False):
            return (("turn.failed", {"code": "turn_already_active", "retryable": True}),)
        with state.state_lock:
            if state.fenced:
                state.admission.release()
                return (("turn.failed", {"code": "stale_epoch", "retryable": False}),)
            state.active_turn_id = frame.turn_id
            state.active_run_id = frame.run_id
            state.active_request_id = frame.request_id
            state.interrupt_reason_code = None
            state.turn_generation += 1
            turn_generation = state.turn_generation
            turn_epoch = state.execution_epoch
            try:
                agent = self._ensure_agent(state, selection, provider)
            except HermesHostAdapterError as error:
                state.active_turn_id = state.active_run_id = state.active_request_id = None
                state.admission.release()
                return (("turn.failed", {
                    "code": error.code,
                    "retryable": False,
                    "next_actions": ["close and reopen the session with one authorized provider route"],
                }),)
            except Exception:
                state.active_turn_id = state.active_run_id = None
                state.admission.release()
                return (("turn.failed", {
                    "code": "hermes_agent_initialization_failed",
                    "retryable": False,
                    "next_actions": ["inspect Harness stderr and provider configuration"],
                }),)
            state.bridge.begin(emit, generation=turn_generation)
            # Bind callback attributes to this exact generation.  This is deliberately done
            # immediately before execution so callbacks from an older native run cannot publish
            # into the next turn's bridge.
            for name, callback in state.bridge.callbacks_for(turn_generation).items():
                setattr(agent, name, callback)
            state.bridge.publish("turn.started", {"runtime": "hermes_native"})
            pending_reason = state.pending_cancels.pop((frame.turn_id or "", frame.run_id or ""), None)
            if pending_reason:
                state.interrupt_reason_code = pending_reason
                interrupt = getattr(agent, "hard_interrupt", None) or getattr(agent, "interrupt", None)
                if callable(interrupt):
                    try:
                        interrupt(pending_reason, tool_reason=pending_reason)
                    except TypeError:
                        interrupt()
        # Do not hold the binding lock while Hermes performs model/tool work.
        turn_fenced = False
        try:
            result = _run_native_turn(agent, text, turn_params)
        except Exception:
            with state.state_lock:
                state.active_turn_id = state.active_run_id = state.active_request_id = None
            return (("turn.failed", {
                    "code": "hermes_native_turn_failed",
                    "reason_code": "provider_interrupted",
                    "retryable": True,
                    "next_actions": ["inspect Harness stderr before retrying"],
                }),)
        finally:
            with state.state_lock:
                state.agent_last_used = time.monotonic()
                state.active_turn_id = state.active_run_id = state.active_request_id = None
                turn_fenced = state.execution_epoch != turn_epoch or state.fenced
                if state.execution_epoch != turn_epoch:
                    state.fenced = False
                    state.bridge.fenced = False
                    self._evict_agent(state)
            state.admission.release()
        if not isinstance(result, Mapping):
            return (("turn.failed", {"code": "hermes_result_invalid", "retryable": False}),)
        if result.get("interrupted") is True:
            return (("turn.cancelled", {
                    "status": "cancelled", "runtime": "hermes_native",
                    "reason_code": state.interrupt_reason_code or "user_cancel",
                    "generation": turn_generation,
                    "api_calls": _bounded_count(result.get("api_calls")),
                }),)
        if turn_fenced or state.fenced:
            return (("turn.cancelled", {"status": "interrupted", "runtime": "hermes_native",
                                         "reason_code": state.interrupt_reason_code or "platform_lease_lost",
                                         "generation": turn_generation,
                                         "api_calls": _bounded_count(result.get("api_calls"))}),)
        if result.get("failed") is True or result.get("completed") is not True:
            code = _safe_failure_code(result.get("failure_reason"))
            return (("turn.failed", {
                "code": code,
                "reason_code": _failure_reason_code(result.get("failure_reason")),
                "generation": turn_generation,
                "retryable": bool(result.get("failure_retryable", True)),
                "api_calls": _bounded_count(result.get("api_calls")),
                "next_actions": ["inspect Harness stderr and provider health before retrying"],
            }),)
        final_response = result.get("final_response")
        if state.bridge.delta_count == 0 and isinstance(final_response, str) and final_response:
            state.bridge.publish("assistant.delta", {
                "content": final_response, "final": True, "source": "hermes", "sequence": 1,
            })
        terminal = ("turn.completed", {
            "status": "completed", "runtime": "hermes_native",
            "generation": turn_generation,
            "api_calls": _bounded_count(result.get("api_calls")),
            "streamed": state.bridge.delta_count > 0,
        })
        if emit is None:
            return (*state.bridge.buffered, terminal)
        return (terminal,)

    def _ensure_agent(self, state: _HermesSession, selection: ProviderSelection,
                      provider: Mapping[str, str]) -> Any:
        route = (
            selection.provider_id, selection.model_id, selection.config_ref,
            str(provider.get("base_url") or ""),
            self._env.get("NETWORKCLAW_HARNESS_PROFILE", "development").strip().lower(),
            ",".join(state.tool_signature),
            state.grant_profile_signature,
        )
        if state.agent is not None:
            if state.route != route:
                self._evict_agent(state)
            else:
                state.agent_last_used = time.monotonic()
                return state.agent
        agent_type = self._agent_factory or load_agent_class()
        if self._agent_factory is None:
            from .hermes_tools import install_hermes_host_tools
            install_hermes_host_tools()
        grant = state.host_grant or {}
        budget = grant.get("turn_budget") if isinstance(grant.get("turn_budget"), Mapping) else {}
        iterations = _bounded_int(
            budget.get("max_steps") or self._env.get("NETWORKCLAW_HARNESS_MAX_ITERATIONS"),
            8, 1, 32,
        )
        timeout = float(budget.get("timeout_seconds") or self._env.get("NETWORKCLAW_HARNESS_RUN_TIMEOUT_SECONDS", "120"))
        state.agent = configure_headless_agent(agent_type(
            model=selection.model_id,
            provider=provider["provider"],
            api_mode="chat_completions",
            base_url=provider["base_url"],
            api_key=provider["api_key"],
            requested_provider=selection.provider_id,
            session_id=state.workspace.session_id,
            session_db=state.runtime.session_db,
            cwd=str(state.workspace.root),
            max_iterations=iterations,
            run_budget_seconds=max(1.0, min(timeout, 3600.0)),
            quiet_mode=True,
            platform="gateway",
            enabled_toolsets=["networkclaw", "todo", "delegation"],
            skip_context_files=True,
            skip_memory=True,
            skip_background_review=True,
            **state.bridge.callbacks(),
        ))
        state.agent._networkclaw_take_delegation_grant = (
            lambda **kwargs: self._take_delegation_grant(state, **kwargs)
        )
        state.agent._networkclaw_release_delegation_grants = (
            lambda allocation_ids: self._release_delegation_grants(state, allocation_ids)
        )
        state.route = route
        state.agent_generation += 1
        state.agent_last_used = time.monotonic()
        return state.agent

    @staticmethod
    def _evict_agent(state: _HermesSession) -> None:
        agent = state.agent
        state.agent = None
        state.route = None
        if agent is not None:
            close = getattr(agent, "close", None)
            if callable(close):
                close()

    def _provider_config(self, selection: ProviderSelection) -> Mapping[str, str]:
        if selection.config_ref != "env:openai" or selection.provider_id != "openai":
            raise HermesHostAdapterError("config_ref_not_authorized", "provider config is not authorized")
        api_key = self._env.get("OPENAI_API_KEY", "").strip()
        base_url = self._env.get("OPENAI_BASE_URL", "https://api.openai.com/v1").strip().rstrip("/")
        if not api_key:
            raise HermesHostAdapterError("provider_credentials_missing", "provider credentials are unavailable")
        parsed = urlparse(base_url)
        localhost = parsed.hostname in {"127.0.0.1", "localhost"}
        if parsed.scheme != "https" and not (parsed.scheme == "http" and localhost):
            raise HermesHostAdapterError("provider_endpoint_invalid", "provider endpoint is not allowed")
        allowed = {value.strip().lower() for value in self._env.get(
            "NETWORKCLAW_HARNESS_ALLOWED_EGRESS_HOSTS", ""
        ).split(",") if value.strip()}
        profile = self._env.get("NETWORKCLAW_HARNESS_PROFILE", "development").strip().lower()
        hostname = (parsed.hostname or "").lower()
        if profile == "customer" and not allowed:
            raise HermesHostAdapterError("provider_egress_denied", "customer profile requires an egress allowlist")
        if allowed and hostname not in allowed:
            raise HermesHostAdapterError("provider_egress_denied", "provider endpoint is outside policy")
        return {"provider": "openai", "base_url": base_url, "api_key": api_key}

    @staticmethod
    def _close_state(state: _HermesSession) -> None:
        from .hermes_tools import unbind_hermes_host_tools

        if state.broker is not None:
            state.broker.close()
        if state.agent is not None and state.active_turn_id is not None:
            state.agent.interrupt()
        with state.state_lock:
            try:
                if state.agent is not None:
                    state.agent.close()
            finally:
                HermesHostAdapter._release_delegation_grants(
                    state, tuple(state.child_resources),
                )
                unbind_hermes_host_tools(state.workspace.session_id, state.tool_session)
                state.tool_session.close()
                state.agent = None
                state.runtime.close()

    def _take_delegation_grant(self, state: _HermesSession, *, task_index: int,
                               task_count: int, depth: int,
                               requested_iterations: int, **_: Any) -> Mapping[str, Any]:
        if state.broker is None:
            raise DelegationError("delegation_broker_closed", "delegation broker is unavailable")
        grant = state.broker.request(
            task_index=task_index, task_count=task_count, depth=depth,
            requested_iterations=requested_iterations,
            parent_turn_id=state.active_turn_id,
            parent_generation=state.turn_generation,
        )
        if grant.child_session_id == state.workspace.session_id:
            raise DelegationError("delegation_identity_conflict", "child session must be distinct from parent")
        if grant.workspace_root == state.workspace.root:
            raise DelegationError("delegation_workspace_conflict", "child workspace must be distinct from parent")
        child_workspace = SessionWorkspace.open(
            grant.workspace_root,
            tenant_id=state.tool_session.tenant_id,
            session_id=grant.child_session_id,
        )
        child_runtime = self._runtime_factory(child_workspace)
        from .hermes_tools import HermesHostToolSession, bind_hermes_host_tools
        child_bridge = _LineageBridge(
            state.bridge,
            parent_session_id=state.workspace.session_id,
            parent_turn_id=grant.parent_turn_id,
            parent_generation=grant.parent_generation,
            child_session_id=grant.child_session_id,
        )
        child_tools = HermesHostToolSession(
            tenant_id=state.tool_session.tenant_id,
            user_id=state.tool_session.user_id,
            workspace=child_workspace,
            lease=grant.lease,
            bridge=child_bridge,
            profile_name=self._env.get("NETWORKCLAW_HARNESS_PROFILE", "development"),
        )
        bind_hermes_host_tools(grant.child_session_id, child_tools)
        child_tools.durable.commit(
            session_id=grant.child_session_id,
            event_id=f"lineage:{grant.allocation_id}",
            facts=(SemanticFact(FactKind.OBSERVATION, grant.child_session_id, "child_session_created", {
                "parent_session_id": state.workspace.session_id,
                "parent_turn_id": grant.parent_turn_id,
                "parent_generation": grant.parent_generation,
                "allocation_id": grant.allocation_id,
            }),),
        )
        state.child_resources[grant.allocation_id] = (child_runtime, child_tools)
        return {**grant.hermes_mapping(), "session_db": child_runtime.session_db}

    @staticmethod
    def _release_delegation_grants(state: _HermesSession, allocation_ids: Sequence[str]) -> None:
        from .hermes_tools import unbind_hermes_host_tools
        for allocation_id in allocation_ids:
            resources = state.child_resources.pop(allocation_id, None)
            if resources is None:
                continue
            runtime, tool_session = resources
            unbind_hermes_host_tools(tool_session.workspace.session_id, tool_session)
            tool_session.close()
            runtime.close()


def _safe_failure_code(value: Any) -> str:
    text = str(value or "").strip().lower()
    allowed = {
        "billing", "content_policy_blocked", "context_overflow", "provider_error",
        "rate_limit", "timeout", "unknown",
    }
    return f"hermes_{text}" if text in allowed else "hermes_native_turn_failed"


def _failure_reason_code(value: Any) -> str:
    text = str(value or "").strip().lower()
    if text in {"provider_interrupted", "provider_interruption", "interrupted"}:
        return "provider_interrupted"
    if text in {"turn_timeout", "timeout", "timed_out"}:
        return "turn_timeout"
    if text in {"liveness_timeout", "liveness", "heartbeat_timeout"}:
        return "liveness_timeout"
    if text in {"harness_shutdown", "shutdown"}:
        return "harness_shutdown"
    return "provider_interrupted" if text else "turn_timeout"


def _reason_code(value: Any) -> str:
    text = str(value or "").strip().lower()
    allowed = {
        "user_cancel", "platform_lease_lost", "epoch_takeover", "provider_interrupted",
        "turn_timeout", "liveness_timeout", "session_closed", "harness_shutdown",
    }
    return text if text in allowed else "user_cancel"


def _payload_string(payload: Mapping[str, Any], key: str) -> str | None:
    value = payload.get(key)
    return value if isinstance(value, str) and value else None


def _payload_int(payload: Mapping[str, Any], key: str) -> int | None:
    value = payload.get(key)
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    return None


def _bounded_count(value: Any) -> int:
    return min(max(int(value or 0), 0), 1_000_000)


def _bounded_int(value: Any, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value) if value is not None else default
    except (TypeError, ValueError):
        parsed = default
    return min(max(parsed, minimum), maximum)


def _stable_signature(value: Any) -> str:
    """Create a bounded, deterministic cache-key fragment without exposing secrets."""
    if value is None:
        return ""
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)[:512]
    except (TypeError, ValueError):
        return str(value)[:512]


def _validated_turn_parameters(payload: Mapping[str, Any], grant: Mapping[str, Any] | None) -> dict[str, Any]:
    """Validate per-turn controls without inventing a second Agent policy layer."""
    params: dict[str, Any] = {}
    system_message = payload.get("system_message")
    if system_message is not None:
        if not isinstance(system_message, str) or len(system_message) > 64 * 1024:
            raise HermesHostAdapterError("system_message_invalid", "system_message is not a bounded string")
        params["system_message"] = system_message
    effort = payload.get("reasoning_effort")
    if effort is not None:
        if effort not in {"minimal", "low", "medium", "high"}:
            raise HermesHostAdapterError("reasoning_effort_invalid", "reasoning_effort is not authorized")
        params["reasoning_effort"] = effort
    if "allowed_tools" in payload:
        allowed = payload["allowed_tools"]
        if not isinstance(allowed, list) or any(not isinstance(item, str) or not item.isascii() for item in allowed):
            raise HermesHostAdapterError("allowed_tools_invalid", "allowed_tools must be an ASCII string array")
        grant_tools = set(grant.get("allowed_tools", ())) if isinstance(grant, Mapping) else None
        if grant_tools is not None and not set(allowed).issubset(grant_tools):
            raise HermesHostAdapterError("allowed_tools_exceed_grant", "turn tools exceed the host grant")
        params["allowed_tools"] = tuple(dict.fromkeys(allowed))
    for key in ("turn_author", "relay_metadata", "memory_snapshot", "mentions", "picker_resources", "agent_snapshot", "specialists"):
        if key in payload:
            value = payload[key]
            if key in {"mentions", "picker_resources"} and (not isinstance(value, list) or not all(isinstance(item, str) for item in value)):
                raise HermesHostAdapterError(f"{key}_invalid", f"{key} must be a string array")
            params[key] = value
    return params


def _run_native_turn(agent: Any, text: str, params: Mapping[str, Any]) -> Mapping[str, Any]:
    """Call Hermes' native turn façade with only parameters supported by the pinned Agent."""
    kwargs: dict[str, Any] = {}
    signature = inspect.signature(agent.run_conversation)
    accepts_kwargs = any(parameter.kind is inspect.Parameter.VAR_KEYWORD for parameter in signature.parameters.values())
    if (accepts_kwargs or "system_message" in signature.parameters) and "system_message" in params:
        kwargs["system_message"] = params["system_message"]
    if (accepts_kwargs or "turn_author" in signature.parameters) and "turn_author" in params:
        kwargs["turn_author"] = params["turn_author"]
    if (accepts_kwargs or "relay_metadata" in signature.parameters) and "relay_metadata" in params:
        kwargs["relay_metadata"] = params["relay_metadata"]

    previous_reasoning = getattr(agent, "reasoning_config", None)
    previous_tools = getattr(agent, "tools", None)
    previous_names = getattr(agent, "valid_tool_names", None)
    try:
        if "reasoning_effort" in params:
            current = dict(previous_reasoning or {})
            current["enabled"] = True
            current["effort"] = params["reasoning_effort"]
            agent.reasoning_config = current
        if "allowed_tools" in params and previous_tools is not None:
            selected = set(params["allowed_tools"])
            agent.tools = [tool for tool in previous_tools if tool.get("function", {}).get("name") in selected]
            agent.valid_tool_names = {tool.get("function", {}).get("name") for tool in agent.tools}
        return agent.run_conversation(text, **kwargs)
    finally:
        if "reasoning_effort" in params:
            agent.reasoning_config = previous_reasoning
        if "allowed_tools" in params and previous_tools is not None:
            agent.tools = previous_tools
            agent.valid_tool_names = previous_names
