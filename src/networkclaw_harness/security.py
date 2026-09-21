"""Fail-closed security guards shared by tools, integrations, and diagnostics."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, Mapping, Sequence
from urllib.parse import urlparse

from .policies import RuntimeProfile
from .projection import project_mapping
from .workspace import SessionWorkspace


class SecurityError(PermissionError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class ThreatBoundary(StrEnum):
    TENANT = "tenant"
    USER = "user"
    SESSION = "session"
    WORKSPACE = "workspace"
    SECRET = "secret"


@dataclass(frozen=True, slots=True)
class SecurityContext:
    tenant_id: str
    user_id: str
    session_id: str
    workspace: SessionWorkspace
    profile: RuntimeProfile


@dataclass(frozen=True, slots=True)
class EgressPolicy:
    allowed_hosts: tuple[str, ...] = ()
    allowed_schemes: tuple[str, ...] = ("https",)
    allow_private_addresses: bool = False


@dataclass(frozen=True, slots=True)
class ResourceSecurityLimits:
    max_subprocesses: int = 8
    max_browser_contexts: int = 2
    max_mcp_servers: int = 8
    max_open_files: int = 256
    max_request_bytes: int = 1024 * 1024

    def __post_init__(self) -> None:
        if min(self.max_subprocesses, self.max_browser_contexts, self.max_mcp_servers,
               self.max_open_files, self.max_request_bytes) <= 0:
            raise ValueError("security resource limits must be positive")


class SecurityGuard:
    """Centralizes identity, workspace, egress, command, and secret boundaries."""

    def __init__(self, context: SecurityContext, *, egress: EgressPolicy = EgressPolicy(),
                 limits: ResourceSecurityLimits = ResourceSecurityLimits(),
                 secret_values: Sequence[str] = ()) -> None:
        self.context = context
        self.egress = egress
        self.limits = limits
        self._secrets = tuple(value for value in secret_values if value)

    @property
    def secret_values(self) -> tuple[str, ...]:
        return self._secrets

    def validate_identity(self, *, tenant_id: str, user_id: str, session_id: str) -> None:
        if (tenant_id, user_id, session_id) != (
                self.context.tenant_id, self.context.user_id, self.context.session_id):
            raise SecurityError("identity_mismatch", "tenant, user, or session is outside the security context")

    def workspace_path(self, relative: str, *, write: bool = False) -> Path:
        try:
            path = self.context.workspace.path_for(relative)
        except Exception as error:
            raise SecurityError("workspace_escape", "path is outside the assigned workspace") from error
        if write and not self.context.workspace.root.exists():
            raise SecurityError("workspace_unavailable", "assigned workspace is unavailable")
        return path

    def authorize_egress(self, url: str) -> None:
        parsed = urlparse(url)
        if (parsed.scheme.casefold() not in {item.casefold() for item in self.egress.allowed_schemes}
                or not parsed.hostname or parsed.username or parsed.password):
            raise SecurityError("egress_denied", "URL scheme, host, or credentials are not allowed")
        allowed = {host.casefold() for host in self.egress.allowed_hosts}
        if parsed.hostname.casefold() not in allowed:
            raise SecurityError("egress_denied", "destination is not allowlisted")
        if not self.egress.allow_private_addresses and _is_private_hostname(parsed.hostname):
            raise SecurityError("egress_private_denied", "private or loopback destinations are denied")

    def authorize_command(self, argv: Sequence[str], allowlist: Sequence[str]) -> None:
        if not argv or not all(isinstance(item, str) and item for item in argv):
            raise SecurityError("command_denied", "command argv must be non-empty strings")
        executable = Path(argv[0]).name
        if executable not in set(allowlist):
            raise SecurityError("command_denied", "command executable is not allowlisted")
        if any(any(marker in item for marker in (";", "&&", "||", "|", "$(", "`", "\n", "\r", ">", "<"))
               for item in argv):
            raise SecurityError("command_denied", "shell metacharacters are not accepted")

    def redact(self, value: Any) -> Any:
        return project_mapping({"value": value}, secret_values=self._secrets)["value"]

    def safe_hash(self, value: Mapping[str, Any]) -> str:
        encoded = json.dumps(self.redact(value), sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
        return hashlib.sha256(encoded).hexdigest()


def _is_private_hostname(hostname: str) -> bool:
    host = hostname.casefold().rstrip(".")
    if host in {"localhost", "localhost.localdomain", "::1", "0.0.0.0"}:
        return True
    if host.startswith(("127.", "10.", "192.168.", "169.254.")):
        return True
    if host.startswith("172.") and host.count(".") == 3:
        try:
            return 16 <= int(host.split(".")[1]) <= 31
        except ValueError:
            return False
    return False
