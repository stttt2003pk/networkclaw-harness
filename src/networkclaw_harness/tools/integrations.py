"""Session-scoped MCP/browser lifecycles and NetworkClaw business API boundary."""

from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Protocol, Sequence

class IntegrationError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class MCPHandle(Protocol):
    def discover(self) -> Sequence[Mapping[str, Any]]: ...
    def call(self, name: str, arguments: Mapping[str, Any]) -> Mapping[str, Any]: ...
    def close(self) -> None: ...


class BrowserHandle(Protocol):
    def navigate(self, url: str) -> Mapping[str, Any]: ...
    def close(self) -> None: ...


class SessionResourceManager:
    def __init__(self, *, mcp_factory: Callable[[str, str], MCPHandle] | None = None,
                 browser_factory: Callable[[str], BrowserHandle] | None = None,
                 mcp_enabled: bool = False, max_mcp_servers: int = 8,
                 max_browser_contexts: int = 2) -> None:
        if max_mcp_servers <= 0 or max_browser_contexts <= 0:
            raise ValueError("integration resource limits must be positive")
        self._mcp_factory = mcp_factory
        self._browser_factory = browser_factory
        self._mcp_enabled = mcp_enabled
        self._max_mcp_servers = max_mcp_servers
        self._max_browser_contexts = max_browser_contexts
        self._mcp: dict[tuple[str, str], MCPHandle] = {}
        self._browsers: dict[str, BrowserHandle] = {}
        self._lock = threading.RLock()

    def discover_mcp(self, session_id: str, server_ref: str) -> tuple[Mapping[str, Any], ...]:
        handle = self._mcp_handle(session_id, server_ref)
        try:
            return tuple(handle.discover())
        except Exception as error:
            self._discard_mcp(session_id, server_ref)
            raise IntegrationError("mcp_unavailable", type(error).__name__) from error

    def call_mcp(self, session_id: str, server_ref: str, name: str,
                 arguments: Mapping[str, Any]) -> Mapping[str, Any]:
        handle = self._mcp_handle(session_id, server_ref)
        try:
            return handle.call(name, arguments)
        except Exception as error:
            self._discard_mcp(session_id, server_ref)
            raise IntegrationError("mcp_call_failed", type(error).__name__) from error

    def browser(self, session_id: str, *, enabled: bool) -> BrowserHandle:
        if not enabled or self._browser_factory is None:
            raise IntegrationError("browser_disabled", "browser is disabled by the runtime profile")
        with self._lock:
            handle = self._browsers.get(session_id)
            if handle is None:
                if len(self._browsers) >= self._max_browser_contexts:
                    raise IntegrationError("browser_capacity_exhausted", "browser context capacity is exhausted")
                handle = self._browser_factory(session_id)
                self._browsers[session_id] = handle
            return handle

    def browser_download(self, session_id: str, *, enabled: bool, url: str,
                         destination_authorizer: Callable[[str], None],
                         artifact_writer: Callable[[bytes, Mapping[str, str]], str]) -> str:
        destination_authorizer(url)
        handle = self.browser(session_id, enabled=enabled)
        try:
            response = handle.navigate(url)
            content = response.get("download")
            if not isinstance(content, bytes):
                raise IntegrationError("browser_download_missing", "browser response has no download")
            return artifact_writer(content, {"url": url, "session_id": session_id})
        except IntegrationError:
            raise
        except Exception as error:
            self._discard_browser(session_id)
            raise IntegrationError("browser_crashed", type(error).__name__) from error

    def close_session(self, session_id: str) -> None:
        with self._lock:
            mcp = [(key, value) for key, value in self._mcp.items() if key[0] == session_id]
            browser = self._browsers.pop(session_id, None)
            for key, _ in mcp:
                self._mcp.pop(key, None)
        for _, handle in mcp:
            handle.close()
        if browser is not None:
            browser.close()

    def _mcp_handle(self, session_id: str, server_ref: str) -> MCPHandle:
        if not self._mcp_enabled or self._mcp_factory is None:
            raise IntegrationError("mcp_disabled", "MCP runtime is unavailable")
        key = (session_id, server_ref)
        with self._lock:
            handle = self._mcp.get(key)
            if handle is None:
                if len(self._mcp) >= self._max_mcp_servers:
                    raise IntegrationError("mcp_capacity_exhausted", "MCP server capacity is exhausted")
                handle = self._mcp_factory(session_id, server_ref)
                self._mcp[key] = handle
            return handle

    def _discard_mcp(self, session_id: str, server_ref: str) -> None:
        with self._lock:
            handle = self._mcp.pop((session_id, server_ref), None)
        if handle is not None:
            handle.close()

    def _discard_browser(self, session_id: str) -> None:
        with self._lock:
            handle = self._browsers.pop(session_id, None)
        if handle is not None:
            handle.close()


class BusinessAPIClient(Protocol):
    def call(self, operation: str, payload: Mapping[str, Any], *, credential_ref: str,
             idempotency_key: str) -> Mapping[str, Any]: ...


@dataclass(frozen=True, slots=True)
class BusinessAuditRecord:
    tenant_id: str
    session_id: str
    user_id: str
    operation: str
    idempotency_key: str
    authorization_hash: str
    backend_status: str


class NetworkClawBusinessAdapter:
    """Formal RPC client boundary; no Lobby/chatrtmgr/database imports are allowed here."""

    def __init__(self, client: BusinessAPIClient, audit: Callable[[BusinessAuditRecord], None]) -> None:
        self._client = client
        self._audit = audit

    def invoke(self, *, tenant_id: str, session_id: str, user_id: str,
               operation: str, payload: Mapping[str, Any], credential_ref: str,
               idempotency_key: str, authorization_hash: str) -> Mapping[str, Any]:
        if not credential_ref or not idempotency_key:
            raise IntegrationError("business_identity_required", "credential reference and idempotency key are required")
        result = self._client.call(
            operation, payload, credential_ref=credential_ref, idempotency_key=idempotency_key,
        )
        self._audit(BusinessAuditRecord(
            tenant_id, session_id, user_id, operation, idempotency_key,
            authorization_hash, str(result.get("status", "unknown")),
        ))
        return result
