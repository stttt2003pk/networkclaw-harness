"""NetworkClaw-owned tools exposed through the vendored Hermes registry."""

from __future__ import annotations

import json
import logging
import re
import threading
from dataclasses import replace
from datetime import datetime, timezone
from typing import Any, Mapping

from networkclaw_harness.policies import (
    FileAccess,
    PolicyError,
    PolicyRequest,
    SideEffect,
    ToolInvocationIdentity,
    ToolPolicy,
    ToolPolicyEngine,
    customer_profile,
    development_profile,
    staging_profile,
)
from networkclaw_harness.tools import (
    DurableToolAudit,
    ToolExecutionError,
    ToolExecutor,
    ToolLayer,
    ToolManifest,
    ToolRegistry,
    ToolRegistryError,
    ToolResult,
)
from networkclaw_harness.workspace import (
    EpochError,
    EpochGuard,
    FenceToken,
    LeasePolicy,
    LeaseRecord,
    SessionWorkspace,
    WorkspaceError,
)
from networkclaw_harness.runtime.models import FactKind, SemanticFact

from .workspace_persistence import WorkspaceDurableSessionStore


TOOL_NAME = "networkclaw_workspace_read"
TOOLSET_NAME = "networkclaw"
POLICY_ID = "networkclaw.workspace.read.v1"
MAX_READ_BYTES = 8 * 1024
LOGGER = logging.getLogger(__name__)

_INPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "path": {
            "type": "string",
            "description": "Workspace-relative UTF-8 text file path.",
        },
        "max_bytes": {
            "type": "integer",
            "description": f"Maximum bytes to return, from 1 to {MAX_READ_BYTES}.",
        },
    },
    "required": ["path"],
    "additionalProperties": False,
}

_OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "status": {"type": "string"},
        "summary": {"type": "string"},
        "next_actions": {"type": "array"},
        "artifacts": {"type": "array"},
        "path": {"type": "string"},
        "content": {"type": "string"},
        "bytes_read": {"type": "integer"},
        "truncated": {"type": "boolean"},
    },
    "required": [
        "status", "summary", "next_actions", "artifacts", "path", "content",
        "bytes_read", "truncated",
    ],
    "additionalProperties": False,
}


class _WorkspaceReader:
    def __init__(self, workspace: SessionWorkspace) -> None:
        self._workspace = workspace

    def __call__(self, arguments: Mapping[str, Any], cancellation: Any) -> Mapping[str, Any]:
        cancellation.raise_if_cancelled()
        relative = str(arguments.get("path") or "").strip()
        if not relative:
            raise ToolExecutionError("invalid_path", "path must be a non-empty workspace-relative file")
        limit = arguments.get("max_bytes", MAX_READ_BYTES)
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_READ_BYTES:
            raise ToolExecutionError("invalid_limit", f"max_bytes must be between 1 and {MAX_READ_BYTES}")
        try:
            path = self._workspace.path_for(relative)
        except WorkspaceError as error:
            raise ToolExecutionError(error.code, "path is outside the assigned workspace") from error
        if not path.is_file() or path.is_symlink():
            raise ToolExecutionError("file_not_found", "path is not a readable workspace file")
        try:
            with path.open("rb") as stream:
                content = stream.read(limit + 1)
            text = content[:limit].decode("utf-8", errors="strict")
        except UnicodeDecodeError as error:
            raise ToolExecutionError("invalid_encoding", "workspace_read accepts UTF-8 text files only") from error
        except OSError as error:
            raise ToolExecutionError("read_failed", "workspace file could not be read") from error
        cancellation.raise_if_cancelled()
        truncated = len(content) > limit
        size = min(len(content), limit)
        return {
            "status": "success",
            "summary": f"Read {relative} ({size} bytes{', truncated' if truncated else ''}).",
            "next_actions": ["Request a narrower file range or a larger bounded read."] if truncated else [],
            "artifacts": [],
            "path": relative,
            "content": text,
            "bytes_read": size,
            "truncated": truncated,
        }


class _ToolAudit:
    """Persist bounded metadata while keeping file content out of audit and protocol events."""

    def __init__(self, durable: WorkspaceDurableSessionStore, invocation_id: str, bridge: Any) -> None:
        self._delegate = DurableToolAudit(
            durable, invocation_id=invocation_id, event_namespace="hermes-native",
        )
        self._bridge = bridge
        self.completed = False

    def intent(self, request: PolicyRequest, decision: Any) -> None:
        self._delegate.intent(request, decision)
        self._bridge.publish("tool.started", {
            "tool_name": request.tool_name,
            "invocation_id": request.invocation_id,
            "policy_id": request.policy_id,
        })

    def result(self, request: PolicyRequest, result: ToolResult) -> None:
        metadata = None
        if result.output is not None:
            metadata = {
                key: result.output[key]
                for key in ("status", "summary", "path", "bytes_read", "truncated")
                if key in result.output
            }
        self._delegate.result(request, replace(result, output=metadata, preview=None))
        self.completed = True
        self._bridge.publish("tool.completed", {
            "tool_name": result.tool_name,
            "invocation_id": request.invocation_id,
            "status": str(result.status),
            "summary": result.summary,
            "retryable": result.retryable,
            **({"artifact_id": result.artifact_id} if result.artifact_id else {}),
        })


class HermesHostToolSession:
    """Session binding used by the process-global Hermes registry handler."""

    def __init__(self, *, tenant_id: str, user_id: str, workspace: SessionWorkspace,
                 lease: Mapping[str, Any], bridge: Any, profile_name: str) -> None:
        self.tenant_id = tenant_id
        self.user_id = user_id
        self.workspace = workspace
        self.bridge = bridge
        self._lease = dict(lease)
        self._lock = threading.RLock()
        self._next_invocation = 0
        self.durable = WorkspaceDurableSessionStore(workspace)
        self.guard = EpochGuard(self)
        self.profile = _profile(profile_name)
        registry = ToolRegistry()
        registry.register(ToolManifest(
            TOOL_NAME, "1.0.0",
            "Read one bounded UTF-8 text file from the host-assigned session workspace.",
            _INPUT_SCHEMA, _OUTPUT_SCHEMA, POLICY_ID, "core", ToolLayer.CORE,
            "networkclaw-harness", _WorkspaceReader(workspace),
        ))
        self.surface = registry.surface(
            profile=self.profile, reachable_services=(), session_tools=(TOOL_NAME,),
        )
        self.policies = ToolPolicyEngine((ToolPolicy(
            POLICY_ID, SideEffect.READ_ONLY, True, False,
            file_access=FileAccess.READ, timeout_ms=5_000, max_output_bytes=16 * 1024,
        ),))

    def update_lease(self, lease: Mapping[str, Any]) -> None:
        with self._lock:
            self._lease = dict(lease)

    def validate(self, token: FenceToken) -> LeaseRecord:
        with self._lock:
            record = _lease_record(self._lease)
        current = FenceToken(
            record.session_id, record.owner_id, record.execution_epoch,
            record.lease_id, record.lease_version,
        )
        if token != current:
            raise EpochError("stale_epoch", "tool execution fence is stale")
        if datetime.now(timezone.utc) >= record.expires_at:
            raise EpochError("lease_expired", "tool execution lease has expired")
        return record

    def execute(self, arguments: Mapping[str, Any]) -> str:
        with self._lock:
            token = _token(self._lease)
            self._next_invocation += 1
            invocation_id = f"native-tool-{self._next_invocation}"
        audit = _ToolAudit(self.durable, invocation_id, self.bridge)
        executor = ToolExecutor(
            self.surface, self.policies, self.profile, self.guard,
            lambda *_: (_ for _ in ()).throw(ToolExecutionError(
                "artifact_not_expected", "bounded workspace reads must remain inline",
            )),
            audit,
        )
        request = PolicyRequest(
            TOOL_NAME, POLICY_ID, "core", dict(arguments),
            ToolInvocationIdentity(
                self.tenant_id, self.user_id, self.workspace.session_id,
                self.workspace, token,
            ),
            paths=(str(arguments.get("path") or ""),),
            invocation_id=invocation_id,
        )
        try:
            result = executor.execute(request)
            if result.output is None:
                return _error_observation("result_unavailable", "Tool output was not available inline.")
            return json.dumps(result.output, sort_keys=True, ensure_ascii=True)
        except (EpochError, PolicyError, ToolExecutionError, ToolRegistryError, WorkspaceError) as error:
            code = str(getattr(error, "code", "tool_failed"))
            if not audit.completed:
                self.bridge.publish("tool.completed", {
                    "tool_name": TOOL_NAME, "invocation_id": invocation_id,
                    "status": "failed", "error_code": code, "retryable": False,
                })
            return _error_observation(code, "The bounded workspace read was rejected.")
        except Exception as error:
            LOGGER.exception("NetworkClaw workspace tool failed: %s", type(error).__name__)
            if not audit.completed:
                self.bridge.publish("tool.completed", {
                    "tool_name": TOOL_NAME, "invocation_id": invocation_id,
                    "status": "failed", "error_code": "tool_failed", "retryable": False,
                })
            return _error_observation("tool_failed", "The bounded workspace read failed safely.")

    def record_plan(self, *, invocation_id: str, result: Any) -> Mapping[str, Any] | None:
        """Commit a Hermes todo snapshot before publishing it as ``plan.updated``."""
        try:
            value = json.loads(result) if isinstance(result, str) else result
        except (TypeError, json.JSONDecodeError):
            return None
        if not isinstance(value, Mapping) or not isinstance(value.get("todos"), list):
            return None
        revision = value.get("revision")
        if not isinstance(revision, int) or isinstance(revision, bool) or revision < 0:
            return None
        items: list[dict[str, str]] = []
        for raw in value["todos"][:100]:
            if not isinstance(raw, Mapping):
                continue
            item_id = str(raw.get("id") or "").strip()[:128]
            title = str(raw.get("content") or raw.get("title") or "").strip()[:512]
            status = str(raw.get("status") or "pending").strip().lower()[:32]
            if item_id and item_id.isascii() and title:
                items.append({"id": item_id, "title": title, "status": status})
        plan_id = f"todo-{self.workspace.session_id}"
        safe_invocation = re.sub(r"[^A-Za-z0-9_.:-]", "_", invocation_id or str(revision))[:160]
        payload = {
            "schema_version": "networkclaw.plan.v1",
            "revision": revision,
            "items": items,
            "source": "hermes.todo_list",
        }
        ack = self.durable.commit(
            session_id=self.workspace.session_id,
            event_id=f"todo-plan:{safe_invocation}:{revision}",
            facts=(SemanticFact(FactKind.PLAN, plan_id, "updated", payload),),
        )
        return {"plan_id": plan_id, **payload, "cursor": str(ack.cursor)}

    def close(self) -> None:
        self.durable.close()


class _Dispatcher:
    def __init__(self) -> None:
        self._sessions: dict[str, HermesHostToolSession] = {}
        self._lock = threading.RLock()
        self._installed = False

    def install(self) -> None:
        with self._lock:
            if self._installed:
                return
            from tools.registry import registry
            registry.register(
                name=TOOL_NAME,
                toolset=TOOLSET_NAME,
                schema={
                    "description": "Read one bounded UTF-8 text file from the assigned workspace.",
                    "parameters": dict(_INPUT_SCHEMA),
                },
                handler=_dispatch,
                max_result_size_chars=16 * 1024,
            )
            self._installed = True

    def bind(self, session_id: str, session: HermesHostToolSession) -> None:
        with self._lock:
            self._sessions[session_id] = session

    def unbind(self, session_id: str, session: HermesHostToolSession) -> None:
        with self._lock:
            if self._sessions.get(session_id) is session:
                self._sessions.pop(session_id, None)

    def execute(self, session_id: str, arguments: Mapping[str, Any]) -> str:
        with self._lock:
            session = self._sessions.get(session_id)
        if session is None:
            return _error_observation("session_not_open", "The tool session is unavailable.")
        return session.execute(arguments)


_DISPATCHER = _Dispatcher()


def install_hermes_host_tools() -> None:
    _DISPATCHER.install()


def bind_hermes_host_tools(session_id: str, session: HermesHostToolSession) -> None:
    _DISPATCHER.bind(session_id, session)


def unbind_hermes_host_tools(session_id: str, session: HermesHostToolSession) -> None:
    _DISPATCHER.unbind(session_id, session)


def _dispatch(arguments: Mapping[str, Any], *, session_id: str = "", **_: Any) -> str:
    return _DISPATCHER.execute(session_id, arguments)


def _profile(name: str):
    return {
        "customer": customer_profile,
        "staging": staging_profile,
        "development": development_profile,
    }.get(name.strip().lower(), development_profile)()


def _timestamp(value: Any, field: str) -> datetime:
    if not isinstance(value, str):
        raise EpochError("invalid_lease", f"lease {field} is invalid")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise EpochError("invalid_lease", f"lease {field} is invalid") from error
    if parsed.tzinfo is None:
        raise EpochError("invalid_lease", f"lease {field} must be timezone-aware")
    return parsed.astimezone(timezone.utc)


def _lease_record(lease: Mapping[str, Any]) -> LeaseRecord:
    try:
        policy = LeasePolicy(
            int(lease["ttl_ms"]), int(lease["renew_interval_ms"]), int(lease["grace_ms"]),
        )
        return LeaseRecord(
            session_id=str(lease["session_id"]), owner_id=str(lease["owner_id"]),
            execution_epoch=int(lease["execution_epoch"]), lease_id=str(lease["lease_id"]),
            lease_version=int(lease["lease_version"]),
            issued_at=_timestamp(lease["issued_at"], "issued_at"),
            expires_at=_timestamp(lease["expires_at"], "expires_at"),
            renew_by=_timestamp(lease["renew_by"], "renew_by"),
            grace_expires_at=_timestamp(lease["grace_expires_at"], "grace_expires_at"),
            policy=policy,
        )
    except (KeyError, TypeError, ValueError) as error:
        raise EpochError("invalid_lease", "tool execution lease is malformed") from error


def _token(lease: Mapping[str, Any]) -> FenceToken:
    record = _lease_record(lease)
    return FenceToken(
        record.session_id, record.owner_id, record.execution_epoch,
        record.lease_id, record.lease_version,
    )


def _error_observation(code: str, summary: str) -> str:
    return json.dumps({
        "status": "error",
        "summary": summary,
        "error_code": code,
        "next_actions": ["Correct the bounded path or stop after a repeated rejection."],
        "artifacts": [],
        "stop_condition": "Do not retry unchanged arguments after this error.",
    }, sort_keys=True, ensure_ascii=True)
