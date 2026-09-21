"""Policy-bound tool execution, cancellation, bounded output, and audit summaries."""

from __future__ import annotations

import concurrent.futures
import json
import threading
import time
from datetime import datetime, timezone
from dataclasses import dataclass, replace
from typing import Any, Callable, Mapping, Protocol, Sequence

from networkclaw_harness.policies import (
    AuthorizationDecision,
    PolicyRequest,
    SideEffect,
    ToolPolicyEngine,
    hash_schema,
)
from networkclaw_harness.runtime import DurableSessionPort, FactKind, InvocationStatus, SemanticFact
from networkclaw_harness.workspace import EpochGuard

from .registry import ToolRegistryError, ToolSurface, validate_schema


class ToolExecutionError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class CancellationToken:
    def __init__(self, event: threading.Event | None = None) -> None:
        self._event = event or threading.Event()
        self._authorization_hash: str | None = None

    def cancel(self) -> None:
        self._event.set()

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()

    def raise_if_cancelled(self) -> None:
        if self.cancelled:
            raise ToolExecutionError("tool_cancelled", "tool invocation was cancelled")

    @property
    def authorization_hash(self) -> str:
        if self._authorization_hash is None:
            raise ToolExecutionError("authorization_unbound", "tool authorization is not bound")
        return self._authorization_hash

    def bind_authorization(self, value: str) -> None:
        if self._authorization_hash is not None and self._authorization_hash != value:
            raise ToolExecutionError("authorization_rebind", "tool authorization cannot change during execution")
        self._authorization_hash = value


@dataclass(frozen=True, slots=True)
class ToolResult:
    tool_name: str
    status: InvocationStatus
    summary: str
    output: Mapping[str, Any] | None
    artifact_id: str | None
    retryable: bool
    authorization_hash: str
    preview: str | None = None
    duplicate_of: str | None = None


class ToolAuditPort(Protocol):
    def intent(self, request: PolicyRequest, decision: AuthorizationDecision) -> None: ...
    def result(self, request: PolicyRequest, result: ToolResult) -> None: ...


class DurableToolAudit:
    """Records bounded tool facts without raw arguments, secrets, or unrestricted output."""

    def __init__(self, durable: DurableSessionPort, *, invocation_id: str,
                 event_namespace: str = "", on_commit: Callable[[], None] | None = None,
                 secret_values: Sequence[str] = ()) -> None:
        self._durable = durable
        self._invocation_id = invocation_id
        self._event_namespace = event_namespace
        self._on_commit = on_commit
        self._secret_values = tuple(value for value in secret_values if value)
        self._result_refs: dict[str, str] = {}
        self._started: dict[str, tuple[float, str]] = {}

    def _event_id(self, invocation_id: str, phase: str) -> str:
        namespace = f"{self._event_namespace}:" if self._event_namespace else ""
        return f"tool:{namespace}{invocation_id}:{phase}"

    def intent(self, request: PolicyRequest, decision: AuthorizationDecision) -> None:
        invocation_id = request.invocation_id or self._invocation_id
        started_at = _now_iso()
        self._started[invocation_id] = (time.monotonic(), started_at)
        payload = {
            "tool_name": request.tool_name, "policy_id": request.policy_id,
            "arguments_hash": decision.arguments_hash,
            "authorization_hash": decision.authorization_hash,
            "approval_id": request.identity.approval.approval_id if request.identity.approval else None,
            "approval_version": request.identity.approval.interaction_version if request.identity.approval else None,
            "authorization_scope": list(request.authorization_scope),
            "display_arguments": _display_arguments(request.arguments, self._secret_values),
            "started_at": started_at,
        }
        self._durable.commit(
            session_id=request.identity.session_id, event_id=self._event_id(invocation_id, "intent"),
            facts=(SemanticFact(FactKind.INVOCATION, invocation_id, InvocationStatus.RUNNING, payload),),
        )
        if self._on_commit is not None:
            try:
                self._on_commit()
            except Exception:
                pass

    def result(self, request: PolicyRequest, result: ToolResult) -> None:
        invocation_id = request.invocation_id or self._invocation_id
        started = self._started.pop(invocation_id, None)
        payload = {
            "tool_name": result.tool_name, "summary": result.summary,
            "artifact_id": result.artifact_id, "retryable": result.retryable,
            "authorization_hash": result.authorization_hash,
            "result": _display_result(result.output, self._secret_values),
            "started_at": started[1] if started is not None else None,
            "ended_at": _now_iso(),
            "duration_ms": round((time.monotonic() - started[0]) * 1000) if started is not None else None,
        }
        self._durable.commit(
            session_id=request.identity.session_id, event_id=self._event_id(invocation_id, "result"),
            facts=(SemanticFact(FactKind.INVOCATION, invocation_id, result.status, payload),),
        )
        if self._on_commit is not None:
            try:
                self._on_commit()
            except Exception:
                pass


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


_DISPLAY_SECRET_KEYS = frozenset({"authorization", "api_key", "apikey", "token", "secret", "password", "cookie"})
_DISPLAY_LIMIT = 4096


def _display_value(value: Any, *, limit: int = _DISPLAY_LIMIT,
                   secret_values: Sequence[str] = ()) -> Any:
    if isinstance(value, Mapping):
        result = {}
        for key, item in value.items():
            name = str(key)
            result[name] = "[REDACTED]" if name.casefold().replace("-", "_") in _DISPLAY_SECRET_KEYS else _display_value(
                item, limit=limit, secret_values=secret_values,
            )
        return result
    if isinstance(value, (list, tuple)):
        return [_display_value(item, limit=limit, secret_values=secret_values) for item in value[:64]]
    if isinstance(value, str):
        projected = value
        for secret in secret_values:
            projected = projected.replace(secret, "[REDACTED]")
        return projected[:limit] + ("...[truncated]" if len(projected) > limit else "")
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return str(value)[:limit]


def _display_arguments(arguments: Mapping[str, Any], secret_values: Sequence[str] = ()) -> Mapping[str, Any]:
    return _display_value(arguments, secret_values=secret_values)


def _display_result(output: Mapping[str, Any] | None, secret_values: Sequence[str] = ()) -> Mapping[str, Any] | None:
    if output is None:
        return None
    return _display_value(output, secret_values=secret_values)


class ToolExecutor:
    def __init__(self, surface: ToolSurface, policies: ToolPolicyEngine,
                 profile, guard: EpochGuard,
                 artifact_writer: Callable[[str, bytes, str], str],
                 audit: ToolAuditPort, *, secret_values: Sequence[str] = ()) -> None:
        self._surface = surface
        self._policies = policies
        self._profile = profile
        self._guard = guard
        self._artifact_writer = artifact_writer
        self._audit = audit
        self._secret_values = tuple(value for value in secret_values if value)
        self._result_refs: dict[str, str] = {}

    def execute(self, request: PolicyRequest, *, cancellation: CancellationToken | None = None) -> ToolResult:
        token = cancellation or CancellationToken()
        token.raise_if_cancelled()
        manifest = self._surface.require(request.tool_name)
        if manifest.policy_id != request.policy_id or manifest.capability != request.capability:
            raise ToolExecutionError("manifest_policy_mismatch", "invocation does not match the tool manifest")
        validate_schema(request.arguments, manifest.input_schema, label="arguments")
        request = replace(request, tool_schema_hash=hash_schema(manifest.input_schema))
        decision = self._policies.authorize(request, self._profile)
        token.bind_authorization(decision.authorization_hash)
        self._guard.check(request.identity.fence_token)
        self._audit.intent(request, decision)
        self._guard.check(request.identity.fence_token)
        pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        try:
            future = pool.submit(manifest.adapter, request.arguments, token)
            try:
                raw = future.result(timeout=decision.policy.timeout_ms / 1000)
            except concurrent.futures.TimeoutError:
                token.cancel()
                if decision.policy.side_effect is not SideEffect.READ_ONLY:
                    result = ToolResult(
                        manifest.name, InvocationStatus.UNKNOWN,
                        f"{manifest.name}: result unknown after timeout", None, None, False,
                        decision.authorization_hash,
                    )
                    self._audit.result(request, result)
                    return result
                failed = ToolResult(
                    manifest.name, InvocationStatus.FAILED,
                    f"{manifest.name}: read-only timeout", None, None,
                    decision.policy.retryable, decision.authorization_hash,
                )
                self._audit.result(request, failed)
                raise ToolExecutionError("tool_timeout", "read-only tool exceeded its policy timeout")
        except ToolExecutionError as error:
            if error.code != "tool_timeout":
                if error.code == "tool_cancelled":
                    status = InvocationStatus.CANCELLED
                elif decision.policy.side_effect is SideEffect.READ_ONLY:
                    status = InvocationStatus.FAILED
                else:
                    status = InvocationStatus.UNKNOWN
                result = ToolResult(
                    manifest.name, status, f"{manifest.name}: {error.code}", None, None,
                    decision.policy.retryable if status is InvocationStatus.FAILED else False,
                    decision.authorization_hash,
                )
                self._audit.result(request, result)
                if status is InvocationStatus.UNKNOWN:
                    return result
            raise
        except Exception as error:
            status = (InvocationStatus.FAILED if decision.policy.side_effect is SideEffect.READ_ONLY
                      else InvocationStatus.UNKNOWN)
            result = ToolResult(
                manifest.name, status,
                f"{manifest.name}: {type(error).__name__}", None, None,
                decision.policy.retryable if status is InvocationStatus.FAILED else False,
                decision.authorization_hash,
            )
            self._audit.result(request, result)
            if status is InvocationStatus.UNKNOWN:
                return result
            raise ToolExecutionError("tool_failed", type(error).__name__) from error
        finally:
            pool.shutdown(wait=False, cancel_futures=True)
        token.raise_if_cancelled()
        if not isinstance(raw, Mapping):
            return self._invalid_output(request, manifest.name, decision)
        try:
            validate_schema(raw, manifest.output_schema, label="result")
        except ToolRegistryError:
            return self._invalid_output(request, manifest.name, decision)
        redacted = _redact(raw, self._secret_values)
        encoded = json.dumps(redacted, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
        self._guard.check(request.identity.fence_token)
        artifact_id = None
        output = dict(redacted)
        if len(encoded) > decision.policy.max_output_bytes:
            result_hash = __import__("hashlib").sha256(encoded).hexdigest()
            duplicate_of = self._result_refs.get(result_hash)
            preview = json.dumps(redacted, ensure_ascii=True, sort_keys=True)[:min(decision.policy.max_output_bytes, 1024)]
            try:
                artifact_id = duplicate_of or self._artifact_writer(manifest.name, encoded, decision.authorization_hash)
                self._result_refs.setdefault(result_hash, artifact_id)
            except Exception as error:
                status = (InvocationStatus.FAILED if decision.policy.side_effect is SideEffect.READ_ONLY
                          else InvocationStatus.UNKNOWN)
                result = ToolResult(
                    manifest.name, status, f"{manifest.name}: artifact commit failed",
                    None, None, decision.policy.retryable if status is InvocationStatus.FAILED else False,
                    decision.authorization_hash, preview=preview, duplicate_of=duplicate_of,
                )
                self._audit.result(request, result)
                return result
            output = None
        summary = _summary(manifest.name, redacted, len(encoded), artifact_id)
        result = ToolResult(
            manifest.name, InvocationStatus.SUCCEEDED, summary, output, artifact_id,
            decision.policy.retryable, decision.authorization_hash,
        )
        self._audit.result(request, result)
        return result

    def _invalid_output(self, request: PolicyRequest, tool_name: str,
                        decision: AuthorizationDecision) -> ToolResult:
        status = (InvocationStatus.FAILED if decision.policy.side_effect is SideEffect.READ_ONLY
                  else InvocationStatus.UNKNOWN)
        result = ToolResult(
            tool_name, status, f"{tool_name}: output contract invalid", None, None,
            decision.policy.retryable if status is InvocationStatus.FAILED else False,
            decision.authorization_hash,
        )
        self._audit.result(request, result)
        return result


_SENSITIVE_NAMES = {"secret", "token", "password", "authorization", "api_key", "cookie"}


def _summary(tool_name: str, result: Mapping[str, Any], size: int, artifact_id: str | None) -> str:
    status = result.get("exit_code", result.get("status", "completed"))
    safe_status = "redacted" if isinstance(status, str) and any(name in status.casefold() for name in _SENSITIVE_NAMES) else status
    suffix = f" artifact={artifact_id}" if artifact_id else ""
    return f"{tool_name}: status={safe_status} bytes={size}{suffix}"


def _redact(value: Any, secrets: Sequence[str]) -> Any:
    if isinstance(value, str):
        result = value
        for secret in secrets:
            result = result.replace(secret, "[REDACTED]")
        return result
    if isinstance(value, Mapping):
        return {
            str(key): "[REDACTED]" if any(
                name in str(key).casefold() for name in _SENSITIVE_NAMES
            ) else _redact(item, secrets)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_redact(item, secrets) for item in value]
    return value
