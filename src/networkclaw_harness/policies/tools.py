"""Fail-closed tool policy decisions bound to session identity and arguments."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any, Mapping, Sequence, TYPE_CHECKING
from urllib.parse import urlparse

from networkclaw_harness.workspace import FenceToken, SessionWorkspace, WorkspaceError

if TYPE_CHECKING:
    from .profiles import RuntimeProfile


class PolicyError(PermissionError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class SideEffect(StrEnum):
    READ_ONLY = "read_only"
    REVERSIBLE = "reversible"
    EXTERNAL = "external"
    IRREVERSIBLE = "irreversible"


class FileAccess(StrEnum):
    NONE = "none"
    READ = "read"
    WRITE = "write"


@dataclass(frozen=True, slots=True)
class ToolPolicy:
    policy_id: str
    side_effect: SideEffect
    retryable: bool
    approval_required: bool
    network_required: bool = False
    allowed_hosts: tuple[str, ...] = ()
    file_access: FileAccess = FileAccess.NONE
    command_allowlist: tuple[str, ...] = ()
    timeout_ms: int = 30_000
    max_output_bytes: int = 16 * 1024

    def __post_init__(self) -> None:
        if not self.policy_id or self.timeout_ms <= 0 or self.max_output_bytes <= 0:
            raise ValueError("tool policy requires an id and positive resource limits")
        if self.retryable and self.side_effect is not SideEffect.READ_ONLY:
            raise ValueError("only read-only tool policies can be automatically retryable")
        if self.network_required and not self.allowed_hosts:
            raise ValueError("network policies require an explicit destination allowlist")


@dataclass(frozen=True, slots=True)
class ApprovalSnapshot:
    approval_id: str
    tenant_id: str
    session_id: str
    tool_name: str
    arguments_hash: str
    expires_at: datetime
    approved: bool
    interaction_version: int = 1
    scope: tuple[str, ...] = ()
    tool_schema_hash: str = ""


@dataclass(frozen=True, slots=True)
class ToolInvocationIdentity:
    tenant_id: str
    user_id: str
    session_id: str
    workspace: SessionWorkspace
    fence_token: FenceToken
    approval: ApprovalSnapshot | None = None


@dataclass(frozen=True, slots=True)
class PolicyRequest:
    tool_name: str
    policy_id: str
    capability: str
    arguments: Mapping[str, Any]
    identity: ToolInvocationIdentity
    network_destination: str | None = None
    paths: tuple[str, ...] = ()
    command: tuple[str, ...] = ()
    authorization_scope: tuple[str, ...] = ()
    tool_schema_hash: str = ""
    # Stable provider/tool call identity used to merge timeline start/end facts.
    invocation_id: str = ""


@dataclass(frozen=True, slots=True)
class AuthorizationDecision:
    policy: ToolPolicy
    arguments_hash: str
    authorization_hash: str


class ToolPolicyEngine:
    def __init__(self, policies: Sequence[ToolPolicy]) -> None:
        self._policies = {policy.policy_id: policy for policy in policies}
        if len(self._policies) != len(tuple(policies)):
            raise ValueError("tool policy ids must be unique")

    def authorize(self, request: PolicyRequest, profile: "RuntimeProfile",
                  *, now: datetime | None = None) -> AuthorizationDecision:
        policy = self._policies.get(request.policy_id)
        if policy is None:
            raise PolicyError("unknown_policy", "tool policy is not registered")
        identity = request.identity
        if identity.workspace.tenant_id != identity.tenant_id or identity.workspace.session_id != identity.session_id:
            raise PolicyError("identity_mismatch", "workspace identity does not match the tool invocation")
        if identity.fence_token.session_id != identity.session_id:
            raise PolicyError("identity_mismatch", "execution fence belongs to another session")
        _check_capability(profile, request.capability)
        arguments_hash = hash_arguments(request.arguments)
        if policy.approval_required:
            self._check_approval(request, arguments_hash, now or datetime.now(timezone.utc))
        if policy.network_required and request.network_destination is None:
            raise PolicyError("destination_required", "network tool requires an explicit destination")
        if request.network_destination is not None:
            if not policy.allowed_hosts:
                raise PolicyError("network_not_allowed", "tool policy does not permit network access")
            _check_destination(request.network_destination, policy.allowed_hosts)
        if request.paths and policy.file_access is FileAccess.NONE:
            raise PolicyError("file_access_not_allowed", "tool policy does not permit file access")
        for path in request.paths:
            try:
                identity.workspace.path_for(path)
            except WorkspaceError as error:
                raise PolicyError("path_not_allowed", "tool path is outside the session workspace") from error
        if request.command:
            if not policy.command_allowlist or request.command[0] not in policy.command_allowlist:
                raise PolicyError("command_not_allowed", "command executable is not allowed by policy")
        authorization_hash = hashlib.sha256(json.dumps({
            "tenant_id": identity.tenant_id, "user_id": identity.user_id,
            "session_id": identity.session_id, "workspace": str(identity.workspace.root),
            "owner_id": identity.fence_token.owner_id,
            "execution_epoch": identity.fence_token.execution_epoch,
            "lease_id": identity.fence_token.lease_id,
            "tool_name": request.tool_name, "policy_id": policy.policy_id,
            "arguments_hash": arguments_hash,
            "tool_schema_hash": request.tool_schema_hash,
            "approval_id": identity.approval.approval_id if identity.approval else None,
            "approval_version": identity.approval.interaction_version if identity.approval else None,
            "authorization_scope": list(request.authorization_scope),
        }, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        return AuthorizationDecision(policy, arguments_hash, authorization_hash)

    @staticmethod
    def _check_approval(request: PolicyRequest, arguments_hash: str, now: datetime) -> None:
        approval = request.identity.approval
        if approval is None:
            raise PolicyError("approval_required", "tool requires a host approval snapshot")
        if approval.expires_at.tzinfo is None:
            raise PolicyError("invalid_approval", "approval expiry must be timezone-aware")
        if now.astimezone(timezone.utc) >= approval.expires_at.astimezone(timezone.utc):
            raise PolicyError("approval_expired", "tool approval has expired")
        expected = (request.identity.tenant_id, request.identity.session_id, request.tool_name, arguments_hash)
        actual = (approval.tenant_id, approval.session_id, approval.tool_name, approval.arguments_hash)
        if not approval.approved or actual != expected:
            raise PolicyError("approval_mismatch", "approval is denied or bound to different arguments")
        if approval.scope != request.authorization_scope:
            raise PolicyError("approval_scope_mismatch", "approval is bound to a different authorization scope")
        if approval.tool_schema_hash != request.tool_schema_hash:
            raise PolicyError("approval_schema_mismatch", "approval is bound to a different tool schema")


def hash_arguments(arguments: Mapping[str, Any]) -> str:
    try:
        encoded = json.dumps(arguments, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
    except (TypeError, ValueError) as error:
        raise PolicyError("invalid_arguments", "tool arguments must be JSON-compatible") from error
    return hashlib.sha256(encoded).hexdigest()


def hash_schema(schema: Mapping[str, Any]) -> str:
    """Return the canonical hash used to bind approval to a tool input contract."""
    return hash_arguments(dict(schema))


def _check_capability(profile: RuntimeProfile, capability: str) -> None:
    enabled = {
        "core": True,
        "shell": profile.shell_enabled,
        "network": profile.network_enabled,
        "browser": profile.browser_enabled and profile.network_enabled,
        "mcp": profile.network_enabled,
        "business_api": profile.network_enabled,
    }.get(capability)
    if enabled is None:
        raise PolicyError("unknown_capability", "tool capability is unknown")
    if not enabled:
        raise PolicyError("capability_disabled", "tool capability is disabled by the runtime profile")


def _check_destination(destination: str | None, allowed_hosts: Sequence[str]) -> None:
    if not destination:
        raise PolicyError("destination_required", "network tool requires an explicit destination")
    parsed = urlparse(destination)
    if parsed.scheme not in {"https", "http"} or not parsed.hostname or parsed.username or parsed.password:
        raise PolicyError("destination_not_allowed", "network destination is malformed or contains credentials")
    if parsed.hostname.casefold() not in {host.casefold() for host in allowed_hosts}:
        raise PolicyError("destination_not_allowed", "network destination is not allowlisted")
