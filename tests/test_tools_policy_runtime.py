from __future__ import annotations

import json
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from networkclaw_harness.policies import (
    ApprovalSnapshot,
    FileAccess,
    PolicyError,
    PolicyRequest,
    SideEffect,
    ToolInvocationIdentity,
    ToolPolicy,
    ToolPolicyEngine,
    customer_profile,
    development_profile,
    hash_arguments,
    hash_schema,
)
from networkclaw_harness.runtime import FactKind, InvocationStatus, ReferenceDurableSessionStore
from networkclaw_harness.skills import BUILTIN_SKILLS_ROOT, SkillCatalog, SkillError, SkillSessionService
from networkclaw_harness.tools import (
    CancellationToken,
    DurableToolAudit,
    IntegrationError,
    NetworkClawBusinessAdapter,
    SessionResourceManager,
    ToolExecutionError,
    ToolExecutor,
    ToolLayer,
    ToolManifest,
    ToolRegistry,
    ToolRegistryError,
    WorkspaceShellAdapter,
    capability_release_manifest,
    host_bash_manifest,
)
from networkclaw_harness.workspace import (
    EpochGuard,
    LeasePolicy,
    ReferenceLeaseAuthority,
    SessionWorkspace,
)


OBJECT_SCHEMA = {"type": "object", "properties": {}, "additionalProperties": True}


def test_host_bash_manifest_uses_workspace_bound_argv(tmp_path: Path):
    authority, guard, identity = runtime_fixture(tmp_path)
    manifest_value = host_bash_manifest(WorkspaceShellAdapter(identity.workspace))
    assert manifest_value.name == "host_bash"
    assert manifest_value.input_schema["required"] == ["argv"]
    registry = ToolRegistry()
    registry.register(manifest_value)
    policy_engine = ToolPolicyEngine((ToolPolicy(
        "host_bash.read_only", SideEffect.READ_ONLY, True, False,
        file_access=FileAccess.READ, command_allowlist=("pwd",),
    ),))
    durable = ReferenceDurableSessionStore()
    executor = ToolExecutor(
        registry.surface(profile=development_profile(), reachable_services=(), session_tools=()),
        policy_engine, development_profile(), guard, lambda *args: "artifact",
        DurableToolAudit(durable, invocation_id="host-bash"),
    )
    result = executor.execute(PolicyRequest(
        "host_bash", "host_bash.read_only", "shell", {"argv": ["pwd"]}, identity,
        command=("pwd",),
    ))
    assert result.status is InvocationStatus.SUCCEEDED
    assert str(identity.workspace.root) in str(result.output["stdout"])


def test_display_audit_redacts_secret_values_and_callback_failure_is_best_effort(tmp_path: Path):
    authority, guard, identity = runtime_fixture(tmp_path)
    registry = ToolRegistry()
    registry.register(manifest("echo", layer=ToolLayer.PROFILE, capability="shell", policy_id="echo-read", adapter=lambda args, cancel: {
        "exit_code": 0, "stdout": "Bearer top-secret", "stderr": "",
    }, input_schema=OBJECT_SCHEMA, output_schema={
        "type": "object", "properties": {
            "exit_code": {"type": "integer"}, "stdout": {"type": "string"}, "stderr": {"type": "string"},
        }, "required": ["exit_code", "stdout", "stderr"], "additionalProperties": False,
    }))
    durable = ReferenceDurableSessionStore()
    audit = DurableToolAudit(durable, invocation_id="echo-1", secret_values=("top-secret",), on_commit=lambda: (_ for _ in ()).throw(RuntimeError("socket closed")))
    executor = ToolExecutor(
        registry.surface(profile=development_profile(), reachable_services=(), session_tools=()),
        ToolPolicyEngine((ToolPolicy("echo-read", SideEffect.READ_ONLY, True, False, command_allowlist=("echo",)),)),
        development_profile(), guard, lambda *args: "artifact", audit, secret_values=("top-secret",),
    )
    result = executor.execute(PolicyRequest("echo", "echo-read", "shell", {"token": "top-secret"}, identity, command=("echo",)))
    facts = durable.load(identity.session_id)
    encoded = repr([dict(item.fact.payload) for item in facts])
    assert result.status is InvocationStatus.SUCCEEDED
    assert "top-secret" not in encoded
    assert "[REDACTED]" in encoded


def runtime_fixture(tmp_path: Path, session_id: str = "session"):
    authority = ReferenceLeaseAuthority()
    lease = authority.acquire(
        session_id=session_id, owner_id="owner", policy=LeasePolicy(60_000, 20_000, 5_000),
        expected_lease_version=0, expected_execution_epoch=0,
    )
    workspace = SessionWorkspace.open(tmp_path / session_id, tenant_id="tenant", session_id=session_id)
    identity = ToolInvocationIdentity(
        "tenant", "user", session_id, workspace, authority.token(lease),
    )
    return authority, EpochGuard(authority), identity


def manifest(name, *, layer=ToolLayer.CORE, capability="core", policy_id="read", adapter=None,
             input_schema=OBJECT_SCHEMA, output_schema=OBJECT_SCHEMA):
    return ToolManifest(
        name, "1.0.0", f"{name} fixture", input_schema, output_schema,
        policy_id, capability, layer, "networkclaw-harness", adapter or (lambda args, cancel: {"status": "ok"}),
    )


def test_registry_surface_matches_model_schema_and_hermes_registration():
    class HermesRegistry:
        def __init__(self):
            self.names = []

        def register(self, value):
            self.names.append(value.name)

    hermes = HermesRegistry()
    registry = ToolRegistry(hermes=hermes)
    registry.register(manifest("core"))
    registry.register(manifest("shell", layer=ToolLayer.PROFILE, capability="shell"))
    registry.register(manifest("browser", layer=ToolLayer.PROFILE, capability="browser"))
    registry.register(manifest("rpc", layer=ToolLayer.SERVICE, capability="business_api"))
    registry.register(manifest("optional", layer=ToolLayer.SESSION))
    development = registry.surface(
        profile=development_profile(), reachable_services=("rpc",),
        session_tools=("shell", "browser", "rpc", "optional"),
    )
    assert development.names == ("browser", "core", "optional", "rpc", "shell")
    assert tuple(item["function"]["name"] for item in development.definitions) == development.names
    customer = registry.surface(
        profile=customer_profile(), reachable_services=("rpc",), session_tools=("shell", "browser", "rpc"),
    )
    assert customer.names == ("core", "shell")
    assert hermes.names == ["core", "shell", "browser", "rpc", "optional"]
    with pytest.raises(ToolRegistryError) as hidden:
        customer.require("browser")
    assert hidden.value.code == "tool_not_available"


def test_schema_validation_happens_before_handler_or_side_effect(tmp_path: Path):
    authority, guard, identity = runtime_fixture(tmp_path)
    calls = []
    tool = manifest(
        "typed", adapter=lambda args, cancel: calls.append(args) or {"status": "ok"},
        input_schema={
            "type": "object", "properties": {"path": {"type": "string"}},
            "required": ["path"], "additionalProperties": False,
        },
    )
    registry = ToolRegistry()
    registry.register(tool)
    executor = ToolExecutor(
        registry.surface(profile=customer_profile(), reachable_services=(), session_tools=()),
        ToolPolicyEngine((ToolPolicy("read", SideEffect.READ_ONLY, True, False),)),
        customer_profile(), guard, lambda *args: "artifact", DurableToolAudit(ReferenceDurableSessionStore(), invocation_id="inv"),
    )
    with pytest.raises(ToolRegistryError) as invalid:
        executor.execute(PolicyRequest("typed", "read", "core", {}, identity))
    assert invalid.value.code == "schema_invalid" and calls == []


def test_policy_binds_identity_epoch_approval_paths_commands_and_destination(tmp_path: Path):
    authority, guard, identity = runtime_fixture(tmp_path)
    policy = ToolPolicy(
        "write-network", SideEffect.EXTERNAL, False, True,
        network_required=True, allowed_hosts=("api.example.test",),
        file_access=FileAccess.WRITE, command_allowlist=("safe",),
    )
    engine = ToolPolicyEngine((policy,))
    arguments = {"value": 1}
    approval = ApprovalSnapshot(
        "approval", "tenant", "session", "business", hash_arguments(arguments),
        datetime.now(timezone.utc) + timedelta(minutes=1), True,
    )
    approved_identity = ToolInvocationIdentity(
        identity.tenant_id, identity.user_id, identity.session_id,
        identity.workspace, identity.fence_token, approval,
    )
    request = PolicyRequest(
        "business", policy.policy_id, "business_api", arguments, approved_identity,
        network_destination="https://api.example.test/v1", paths=("artifacts/generated",),
        command=("safe", "arg"), authorization_scope=(),
    )
    decision = engine.authorize(request, development_profile())
    assert decision.arguments_hash == hash_arguments(arguments)
    assert len(decision.authorization_hash) == 64

    expired = ApprovalSnapshot(
        "approval", "tenant", "session", "business", hash_arguments(arguments),
        datetime.now(timezone.utc) - timedelta(seconds=1), True,
    )
    with pytest.raises(PolicyError) as expiry:
        engine.authorize(PolicyRequest(
            "business", policy.policy_id, "business_api", arguments,
            ToolInvocationIdentity("tenant", "user", "session", identity.workspace, identity.fence_token, expired),
            network_destination="https://api.example.test", paths=("artifacts",), command=("safe",),
        ), development_profile())
    assert expiry.value.code == "approval_expired"
    with pytest.raises(PolicyError) as mismatch:
        engine.authorize(PolicyRequest(
            "business", policy.policy_id, "business_api", {"value": 2}, approved_identity,
            network_destination="https://api.example.test", paths=("artifacts",), command=("safe",),
        ), development_profile())
    assert mismatch.value.code == "approval_mismatch"
    scoped = ApprovalSnapshot(
        "scoped", "tenant", "session", "business", hash_arguments(arguments),
        datetime.now(timezone.utc) + timedelta(minutes=1), True, 2, ("service:api",),
    )
    with pytest.raises(PolicyError) as scope_mismatch:
        engine.authorize(PolicyRequest(
            "business", policy.policy_id, "business_api", arguments,
            ToolInvocationIdentity("tenant", "user", "session", identity.workspace, identity.fence_token, scoped),
            network_destination="https://api.example.test", paths=("artifacts",),
            command=("safe",), authorization_scope=("service:worker",),
        ), development_profile())
    assert scope_mismatch.value.code == "approval_scope_mismatch"
    for changed, code in (
        ({"network_destination": "https://blocked.example"}, "destination_not_allowed"),
        ({"paths": ("../escape",)}, "path_not_allowed"),
        ({"command": ("unsafe",)}, "command_not_allowed"),
    ):
        values = {
            "network_destination": "https://api.example.test", "paths": ("artifacts",), "command": ("safe",),
        } | changed
        with pytest.raises(PolicyError) as rejected:
            engine.authorize(PolicyRequest(
                "business", policy.policy_id, "business_api", arguments, approved_identity, **values,
            ), development_profile())
        assert rejected.value.code == code
    with pytest.raises(PolicyError) as unknown:
        ToolPolicyEngine(()).authorize(request, development_profile())
    assert unknown.value.code == "unknown_policy"


def test_shell_is_workspace_bound_has_minimal_environment_and_audited_output(tmp_path: Path):
    authority, guard, identity = runtime_fixture(tmp_path)
    schema = {
        "type": "object", "properties": {"argv": {"type": "array"}},
        "required": ["argv"], "additionalProperties": False,
    }
    output_schema = {
        "type": "object", "properties": {
            "exit_code": {"type": "integer"}, "stdout": {"type": "string"}, "stderr": {"type": "string"},
        }, "required": ["exit_code", "stdout", "stderr"], "additionalProperties": False,
    }
    registry = ToolRegistry()
    registry.register(manifest(
        "shell", layer=ToolLayer.PROFILE, capability="shell", policy_id="shell-read",
        adapter=WorkspaceShellAdapter(identity.workspace, environment={"VISIBLE": "yes"}),
        input_schema=schema, output_schema=output_schema,
    ))
    durable = ReferenceDurableSessionStore()
    executor = ToolExecutor(
        registry.surface(profile=development_profile(), reachable_services=(), session_tools=("shell",)),
        ToolPolicyEngine((ToolPolicy(
            "shell-read", SideEffect.READ_ONLY, True, False, command_allowlist=(sys.executable,),
        ),)), development_profile(), guard, lambda *args: "artifact",
        DurableToolAudit(durable, invocation_id="shell-1"), secret_values=("super-secret",),
    )
    argv = [sys.executable, "-c", "import os;print(os.getcwd());print(os.getenv('VISIBLE'));print(os.getenv('SECRET'))"]
    result = executor.execute(PolicyRequest(
        "shell", "shell-read", "shell", {"argv": argv}, identity, command=tuple(argv),
    ))
    assert str(identity.workspace.root) in result.output["stdout"]
    assert "yes" in result.output["stdout"] and "super-secret" not in result.output["stdout"]
    facts = durable.load("session")
    assert [item.fact.state for item in facts] == ["running", "succeeded"]
    assert "argv" not in facts[0].fact.payload


def test_large_sensitive_output_is_redacted_and_stored_as_artifact(tmp_path: Path):
    authority, guard, identity = runtime_fixture(tmp_path)
    registry = ToolRegistry()
    registry.register(manifest("large", adapter=lambda args, cancel: {
        "status": "ok", "token": "hidden", "content": "secret-value" * 20,
    }))
    artifacts = []
    durable = ReferenceDurableSessionStore()
    executor = ToolExecutor(
        registry.surface(profile=customer_profile(), reachable_services=(), session_tools=()),
        ToolPolicyEngine((ToolPolicy("read", SideEffect.READ_ONLY, True, False, max_output_bytes=40),)),
        customer_profile(), guard,
        lambda name, content, auth: artifacts.append((name, content, auth)) or "artifact-1",
        DurableToolAudit(durable, invocation_id="large-1"), secret_values=("secret-value",),
    )
    result = executor.execute(PolicyRequest("large", "read", "core", {}, identity))
    assert result.output is None and result.artifact_id == "artifact-1"
    assert b"secret-value" not in artifacts[0][1] and b"hidden" not in artifacts[0][1]
    assert "artifact-1" in durable.load("session")[-1].fact.payload["summary"]


def test_cancellation_and_unknown_side_effect_timeout_are_not_retryable(tmp_path: Path):
    authority, guard, identity = runtime_fixture(tmp_path)
    registry = ToolRegistry()
    registry.register(manifest(
        "slow", policy_id="write", adapter=lambda args, cancel: (time.sleep(0.1) or {"status": "late"}),
    ))
    executor = ToolExecutor(
        registry.surface(profile=customer_profile(), reachable_services=(), session_tools=()),
        ToolPolicyEngine((ToolPolicy("write", SideEffect.REVERSIBLE, False, False, timeout_ms=10),)),
        customer_profile(), guard, lambda *args: "artifact",
        DurableToolAudit(ReferenceDurableSessionStore(), invocation_id="slow-1"),
    )
    result = executor.execute(PolicyRequest("slow", "write", "core", {}, identity))
    assert result.status is InvocationStatus.UNKNOWN and result.retryable is False
    cancelled = CancellationToken()
    cancelled.cancel()
    with pytest.raises(ToolExecutionError) as stopped:
        executor.execute(PolicyRequest("slow", "write", "core", {}, identity), cancellation=cancelled)
    assert stopped.value.code == "tool_cancelled"


def test_side_effect_with_invalid_result_contract_is_unknown(tmp_path: Path):
    authority, guard, identity = runtime_fixture(tmp_path)
    registry = ToolRegistry()
    registry.register(manifest(
        "write", policy_id="write", adapter=lambda args, cancel: {"wrong": True},
        output_schema={
            "type": "object", "properties": {"status": {"type": "string"}},
            "required": ["status"], "additionalProperties": False,
        },
    ))
    executor = ToolExecutor(
        registry.surface(profile=customer_profile(), reachable_services=(), session_tools=()),
        ToolPolicyEngine((ToolPolicy("write", SideEffect.REVERSIBLE, False, False),)),
        customer_profile(), guard, lambda *args: "artifact",
        DurableToolAudit(ReferenceDurableSessionStore(), invocation_id="write-invalid"),
    )
    result = executor.execute(PolicyRequest("write", "write", "core", {}, identity))
    assert result.status is InvocationStatus.UNKNOWN and not result.retryable


def test_skills_are_complete_read_only_frozen_and_enter_release_manifest():
    catalog = SkillCatalog((BUILTIN_SKILLS_ROOT,))
    durable = ReferenceDurableSessionStore()
    records = SkillSessionService(catalog, durable).load(
        session_id="session", skill_ids=("workspace-inspection",), event_id="skills-loaded",
    )
    assert "Inspect only the host-assigned session workspace" in records[0].content
    assert records[0].semantic_asset()["version"] == "1.0.0"
    assert durable.load("session")[0].fact.kind is FactKind.SKILL
    with pytest.raises(SkillError) as frozen:
        catalog.load_for_session("session", ())
    assert frozen.value.code == "skill_surface_frozen"
    tool = manifest("business-tool", layer=ToolLayer.SERVICE, capability="business_api", policy_id="business")
    release = capability_release_manifest((tool,), records)
    assert [(item.kind, item.capability_id) for item in release] == [
        ("skill", "workspace-inspection"), ("tool", "business-tool")
    ]
    assert all(len(item.content_hash) == 64 for item in release)
    assert not hasattr(catalog, "install") and not hasattr(catalog, "create") and not hasattr(catalog, "publish")
    assert not customer_profile().runtime_skill_install_enabled
    assert not development_profile().runtime_tool_install_enabled
    assert not development_profile().self_evolution_enabled
    sbom = json.loads((Path(__file__).parent.parent / "offline/sbom-template.json").read_text())
    component = next(item for item in sbom["components"] if item["name"] == "networkclaw-skill-workspace-inspection")
    assert component["hashes"][0]["content"] == records[0].content_sha256


class FakeHandle:
    def __init__(self, session_id, *, crash=False, download=b"download"):
        self.session_id = session_id
        self.crash = crash
        self.download = download
        self.closed = False

    def discover(self):
        if self.crash:
            raise RuntimeError("crash")
        return ({"name": "read"},)

    def call(self, name, arguments):
        if self.crash:
            raise RuntimeError("crash")
        return {"status": "ok", "session_id": self.session_id}

    def navigate(self, url):
        if self.crash:
            raise RuntimeError("crash")
        return {"download": self.download}

    def close(self):
        self.closed = True


def test_mcp_and_browser_resources_are_session_scoped_crash_isolated_and_closed():
    handles = {}

    def mcp_factory(session_id, server_ref):
        handle = FakeHandle(session_id, crash=server_ref == "broken")
        handles[(session_id, server_ref)] = handle
        return handle

    browsers = {}

    def browser_factory(session_id):
        handle = FakeHandle(session_id, crash=session_id == "broken")
        browsers[session_id] = handle
        return handle

    manager = SessionResourceManager(
        mcp_factory=mcp_factory, browser_factory=browser_factory, mcp_enabled=True,
    )
    assert manager.discover_mcp("a", "server") == ({"name": "read"},)
    assert manager.call_mcp("b", "server", "read", {})["session_id"] == "b"
    with pytest.raises(IntegrationError):
        manager.discover_mcp("a", "broken")
    assert handles[("a", "broken")].closed
    assert manager.call_mcp("b", "server", "read", {})["status"] == "ok"
    artifacts = []
    artifact_id = manager.browser_download(
        "a", enabled=True, url="https://allowed.example/file",
        destination_authorizer=lambda url: None,
        artifact_writer=lambda content, source: artifacts.append((content, source)) or "download-1",
    )
    assert artifact_id == "download-1" and artifacts[0][0] == b"download"
    with pytest.raises(PolicyError):
        manager.browser_download(
            "never-created", enabled=True, url="https://blocked.example/file",
            destination_authorizer=lambda url: (_ for _ in ()).throw(PolicyError("destination_not_allowed", "blocked")),
            artifact_writer=lambda content, source: "nope",
        )
    assert "never-created" not in browsers
    with pytest.raises(IntegrationError) as crashed:
        manager.browser_download(
            "broken", enabled=True, url="https://allowed.example/file",
            destination_authorizer=lambda url: None,
            artifact_writer=lambda content, source: "nope",
        )
    assert crashed.value.code == "browser_crashed" and browsers["broken"].closed
    assert manager.call_mcp("b", "server", "read", {})["status"] == "ok"
    with pytest.raises(IntegrationError) as disabled:
        manager.browser("customer", enabled=False)
    assert disabled.value.code == "browser_disabled"
    manager.close_session("a")
    assert handles[("a", "server")].closed and browsers["a"].closed
    assert not handles[("b", "server")].closed


def test_networkclaw_business_adapter_uses_rpc_credentials_idempotency_and_audit():
    class Backend:
        def __init__(self):
            self.calls = []

        def call(self, operation, payload, *, credential_ref, idempotency_key):
            self.calls.append((operation, payload, credential_ref, idempotency_key))
            return {"status": "accepted", "backend_id": "42"}

    backend = Backend()
    audit = []
    adapter = NetworkClawBusinessAdapter(backend, audit.append)
    result = adapter.invoke(
        tenant_id="tenant", session_id="session", user_id="user", operation="ticket.create",
        payload={"title": "incident"}, credential_ref="host-secret-ref",
        idempotency_key="invocation-1", authorization_hash="a" * 64,
    )
    assert result["backend_id"] == "42"
    assert backend.calls == [("ticket.create", {"title": "incident"}, "host-secret-ref", "invocation-1")]
    assert audit[0].backend_status == "accepted" and audit[0].authorization_hash == "a" * 64


def test_business_tool_end_to_end_applies_policy_without_exposing_credential(tmp_path: Path):
    class Backend:
        def __init__(self):
            self.credential_refs = []

        def call(self, operation, payload, *, credential_ref, idempotency_key):
            self.credential_refs.append(credential_ref)
            return {"status": "accepted", "backend_id": idempotency_key}

    authority, guard, identity = runtime_fixture(tmp_path)
    backend = Backend()
    business_audit = []
    adapter = NetworkClawBusinessAdapter(backend, business_audit.append)
    arguments = {"title": "incident"}
    approval = ApprovalSnapshot(
        "approval", "tenant", "session", "ticket.create", hash_arguments(arguments),
        datetime.now(timezone.utc) + timedelta(minutes=1), True,
        tool_schema_hash=hash_schema(OBJECT_SCHEMA),
    )
    identity = ToolInvocationIdentity(
        identity.tenant_id, identity.user_id, identity.session_id, identity.workspace,
        identity.fence_token, approval,
    )
    policy_engine = ToolPolicyEngine((ToolPolicy(
        "business", SideEffect.EXTERNAL, False, True, network_required=True,
        allowed_hosts=("api.example.test",),
    ),))
    policy_request = PolicyRequest(
        "ticket.create", "business", "business_api", arguments, identity,
        network_destination="https://api.example.test/tickets",
        tool_schema_hash=hash_schema(OBJECT_SCHEMA),
    )
    authorization = policy_engine.authorize(policy_request, development_profile())
    registry = ToolRegistry()
    registry.register(manifest(
        "ticket.create", layer=ToolLayer.SERVICE, capability="business_api", policy_id="business",
        adapter=lambda args, cancel: adapter.invoke(
            tenant_id="tenant", session_id="session", user_id="user", operation="ticket.create",
            payload=args, credential_ref="host-owned-reference", idempotency_key="invocation-1",
            authorization_hash=cancel.authorization_hash,
        ),
    ))
    durable = ReferenceDurableSessionStore()
    executor = ToolExecutor(
        registry.surface(
            profile=development_profile(), reachable_services=("ticket.create",),
            session_tools=("ticket.create",),
        ),
        policy_engine, development_profile(), guard, lambda *args: "artifact",
        DurableToolAudit(durable, invocation_id="invocation-1"),
    )
    result = executor.execute(policy_request)
    assert result.status is InvocationStatus.SUCCEEDED
    assert backend.credential_refs == ["host-owned-reference"]
    assert business_audit[0].authorization_hash == authorization.authorization_hash
    assert "host-owned-reference" not in str(result.output)
    assert "host-owned-reference" not in str([item.fact.payload for item in durable.load("session")])
