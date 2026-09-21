"""Production host adapter whose sole loop owner is vendored Hermes ``AIAgent``."""

from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, MutableSequence, Sequence
from urllib.parse import urlparse

from networkclaw_harness.protocol import InputFrame
from networkclaw_harness.workspace import SessionWorkspace

from .hermes_bootstrap import HermesRuntimeBootstrap, open_hermes_runtime
from .delegation import DelegationError, DelegationGrant, HostDelegationBroker
from .hermes_native_probe import configure_headless_agent, load_agent_class
from .provider_selection import ProviderSelection, ProviderSelectionError, load_selection


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

    def begin(self, emit: Emit | None) -> None:
        with self.lock:
            self.emit = emit
            self.buffered.clear()
            self.delta_count = 0

    def publish(self, event_type: str, payload: Mapping[str, Any]) -> None:
        item = (event_type, dict(payload))
        with self.lock:
            emit = self.emit
            if emit is None:
                self.buffered.append(item)
                return
        emit(*item)

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


@dataclass(slots=True)
class _HermesSession:
    workspace: SessionWorkspace
    runtime: HermesRuntimeBootstrap
    tool_session: Any
    bridge: _EventBridge = field(default_factory=_EventBridge)
    agent: Any = None
    route: tuple[str, str, str] | None = None
    lock: threading.RLock = field(default_factory=threading.RLock)
    broker: HostDelegationBroker | None = None
    active_turn_id: str | None = None
    active_run_id: str | None = None
    child_resources: dict[str, tuple[HermesRuntimeBootstrap, Any]] = field(default_factory=dict)


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
             lease: Mapping[str, Any]) -> None:
        from .hermes_tools import HermesHostToolSession, bind_hermes_host_tools

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
        replacement = _HermesSession(workspace, runtime, tool_session, bridge)
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
            state.tool_session.update_lease(lease)

    def close(self, session_id: str) -> None:
        with self._lock:
            state = self._sessions.pop(session_id, None)
        if state is not None:
            self._close_state(state)

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
            if state.active_turn_id is not None and frame.turn_id != state.active_turn_id:
                return (("turn.controlled", {"command": frame.type, "state": "stale_turn"}),)
            interrupted = False
            if state.agent is not None and state.active_turn_id is not None:
                hard_interrupt = getattr(state.agent, "hard_interrupt", None)
                if callable(hard_interrupt):
                    hard_interrupt("host requested turn cancellation", tool_reason="host turn cancellation")
                    interrupted = True
                else:
                    interrupted = bool(state.agent.interrupt())
            return (("turn.controlled", {"command": frame.type,
                                          "state": "cancel_requested" if interrupted else "idle"}),)
        if frame.type == "turn.steer":
            if state.active_turn_id is not None and frame.turn_id != state.active_turn_id:
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
        except (ProviderSelectionError, HermesHostAdapterError) as error:
            return (("turn.failed", {
                "code": error.code,
                "retryable": False,
                "next_actions": ["correct the authorized provider route and start a new turn"],
            }),)

        with state.lock:
            if state.active_turn_id is not None:
                return (("turn.failed", {"code": "turn_already_active", "retryable": True}),)
            state.active_turn_id = frame.turn_id
            state.active_run_id = frame.run_id
            try:
                agent = self._ensure_agent(state, selection, provider)
            except HermesHostAdapterError as error:
                state.active_turn_id = state.active_run_id = None
                return (("turn.failed", {
                    "code": error.code,
                    "retryable": False,
                    "next_actions": ["close and reopen the session with one authorized provider route"],
                }),)
            except Exception:
                state.active_turn_id = state.active_run_id = None
                return (("turn.failed", {
                    "code": "hermes_agent_initialization_failed",
                    "retryable": False,
                    "next_actions": ["inspect Harness stderr and provider configuration"],
                }),)
            state.bridge.begin(emit)
            state.bridge.publish("turn.started", {"runtime": "hermes_native"})
            try:
                result = agent.run_conversation(text)
            except Exception:
                state.active_turn_id = state.active_run_id = None
                return (("turn.failed", {
                    "code": "hermes_native_turn_failed",
                    "retryable": True,
                    "next_actions": ["inspect Harness stderr before retrying"],
                }),)
            if not isinstance(result, Mapping):
                state.active_turn_id = state.active_run_id = None
                return (("turn.failed", {"code": "hermes_result_invalid", "retryable": False}),)
            if result.get("interrupted") is True:
                state.active_turn_id = state.active_run_id = None
                return (("turn.cancelled", {
                    "status": "cancelled", "runtime": "hermes_native",
                    "api_calls": _bounded_count(result.get("api_calls")),
                }),)
            if result.get("failed") is True or result.get("completed") is not True:
                code = _safe_failure_code(result.get("failure_reason"))
                state.active_turn_id = state.active_run_id = None
                return (("turn.failed", {
                    "code": code,
                    "retryable": bool(result.get("failure_retryable", True)),
                    "api_calls": _bounded_count(result.get("api_calls")),
                    "next_actions": ["inspect Harness stderr and provider health before retrying"],
                }),)
            final_response = result.get("final_response")
            if state.bridge.delta_count == 0 and isinstance(final_response, str) and final_response:
                state.bridge.publish("assistant.delta", {
                    "content": final_response,
                    "final": True,
                    "source": "hermes",
                    "sequence": 1,
                })
            terminal = ("turn.completed", {
                "status": "completed",
                "runtime": "hermes_native",
                "api_calls": _bounded_count(result.get("api_calls")),
                "streamed": state.bridge.delta_count > 0,
            })
            state.active_turn_id = state.active_run_id = None
            if emit is None:
                return (*state.bridge.buffered, terminal)
            return (terminal,)

    def _ensure_agent(self, state: _HermesSession, selection: ProviderSelection,
                      provider: Mapping[str, str]) -> Any:
        route = (selection.provider_id, selection.model_id, selection.config_ref)
        if state.agent is not None:
            if state.route != route:
                raise HermesHostAdapterError(
                    "provider_route_changed",
                    "provider route cannot change inside an open native session",
                )
            return state.agent
        agent_type = self._agent_factory or load_agent_class()
        if self._agent_factory is None:
            from .hermes_tools import install_hermes_host_tools
            install_hermes_host_tools()
        iterations = _bounded_int(self._env.get("NETWORKCLAW_HARNESS_MAX_ITERATIONS"), 8, 1, 32)
        timeout = float(self._env.get("NETWORKCLAW_HARNESS_RUN_TIMEOUT_SECONDS", "120"))
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
        return state.agent

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
        with state.lock:
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
        )
        child_workspace = SessionWorkspace.open(
            grant.workspace_root,
            tenant_id=state.tool_session.tenant_id,
            session_id=grant.child_session_id,
        )
        child_runtime = self._runtime_factory(child_workspace)
        from .hermes_tools import HermesHostToolSession, bind_hermes_host_tools
        child_tools = HermesHostToolSession(
            tenant_id=state.tool_session.tenant_id,
            user_id=state.tool_session.user_id,
            workspace=child_workspace,
            lease=grant.lease,
            bridge=state.bridge,
            profile_name=self._env.get("NETWORKCLAW_HARNESS_PROFILE", "development"),
        )
        bind_hermes_host_tools(grant.child_session_id, child_tools)
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


def _bounded_count(value: Any) -> int:
    return min(max(int(value or 0), 0), 1_000_000)


def _bounded_int(value: str | None, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value) if value is not None else default
    except ValueError:
        parsed = default
    return min(max(parsed, minimum), maximum)
