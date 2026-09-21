from __future__ import annotations

import io
import json
import logging
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from networkclaw_harness.capacity import CapacityError, CapacityLedger, CapacityLimits
from networkclaw_harness.host import JsonlHost
from networkclaw_harness.observability import (
    HARNESS_METRICS,
    AlertRule,
    DiagnosticService,
    JsonLogFormatter,
    MetricsRegistry,
    StructuredLogger,
    TraceContext,
)
from networkclaw_harness.policies import (
    ApprovalSnapshot,
    PolicyError,
    PolicyRequest,
    SideEffect,
    ToolInvocationIdentity,
    ToolPolicy,
    ToolPolicyEngine,
    customer_profile,
    development_profile,
    hash_arguments,
)
from networkclaw_harness.security import EgressPolicy, SecurityContext, SecurityError, SecurityGuard
from networkclaw_harness.tools import IntegrationError, SessionResourceManager
from networkclaw_harness.workspace import EpochGuard, LeasePolicy, ReferenceLeaseAuthority, SessionWorkspace


REPO_ROOT = Path(__file__).resolve().parents[1]


def security_fixture(tmp_path: Path, *, session_id: str = "session"):
    workspace = SessionWorkspace.open(
        tmp_path / session_id, tenant_id="tenant", session_id=session_id,
    )
    context = SecurityContext("tenant", "user", session_id, workspace, customer_profile())
    return workspace, SecurityGuard(context, secret_values=("super-secret",))


def invocation_fixture(tmp_path: Path):
    authority = ReferenceLeaseAuthority()
    lease = authority.acquire(
        session_id="session", owner_id="owner",
        policy=LeasePolicy(60_000, 20_000, 5_000),
        expected_lease_version=0, expected_execution_epoch=0,
    )
    workspace = SessionWorkspace.open(tmp_path / "session", tenant_id="tenant", session_id="session")
    identity = ToolInvocationIdentity(
        "tenant", "user", "session", workspace, authority.token(lease),
    )
    return authority, EpochGuard(authority), identity


def test_security_guard_fences_identity_workspace_egress_commands_and_secrets(tmp_path: Path):
    workspace, guard = security_fixture(tmp_path)
    guard.validate_identity(tenant_id="tenant", user_id="user", session_id="session")
    for changed in (
        {"tenant_id": "other", "user_id": "user", "session_id": "session"},
        {"tenant_id": "tenant", "user_id": "other", "session_id": "session"},
        {"tenant_id": "tenant", "user_id": "user", "session_id": "other"},
    ):
        with pytest.raises(SecurityError) as rejected:
            guard.validate_identity(**changed)
        assert rejected.value.code == "identity_mismatch"

    with pytest.raises(SecurityError) as escaped:
        guard.workspace_path("../outside")
    assert escaped.value.code == "workspace_escape"
    outside = tmp_path / "outside"
    outside.mkdir()
    (workspace.root / "linked").symlink_to(outside, target_is_directory=True)
    with pytest.raises(SecurityError):
        guard.workspace_path("linked/file")

    with pytest.raises(SecurityError) as default_deny:
        guard.authorize_egress("https://api.example.test/v1")
    assert default_deny.value.code == "egress_denied"
    allowed = SecurityGuard(
        guard.context, egress=EgressPolicy(("api.example.test",)),
    )
    allowed.authorize_egress("https://api.example.test/v1")
    for url in (
        "http://api.example.test/v1",
        "https://user:password@api.example.test/v1",
        "https://localhost/v1",
        "https://127.0.0.1/v1",
    ):
        with pytest.raises(SecurityError):
            SecurityGuard(
                guard.context,
                egress=EgressPolicy(("api.example.test", "localhost", "127.0.0.1")),
            ).authorize_egress(url)

    guard.authorize_command(("python", "-V"), ("python",))
    for argv in (("sh", "-c", "true"), ("python", "ok; rm"), ("python", "$(id)")):
        with pytest.raises(SecurityError) as command:
            guard.authorize_command(argv, ("python",))
        assert command.value.code == "command_denied"
    redacted = guard.redact({"items": ["super-secret", {"password": "value"}]})
    assert redacted == {"items": ["[redacted]", {"password": "[redacted]"}]}
    assert "super-secret" not in guard.safe_hash({"value": "super-secret"})


def test_approval_binds_host_identity_arguments_schema_and_epoch(tmp_path: Path):
    authority, epoch_guard, identity = invocation_fixture(tmp_path)
    arguments = {"value": 1}
    approval = ApprovalSnapshot(
        "approval", "tenant", "session", "write", hash_arguments(arguments),
        datetime.now(timezone.utc) + timedelta(minutes=1), True,
        tool_schema_hash="schema-v1",
    )
    approved = ToolInvocationIdentity(
        identity.tenant_id, identity.user_id, identity.session_id,
        identity.workspace, identity.fence_token, approval,
    )
    engine = ToolPolicyEngine((ToolPolicy("write", SideEffect.EXTERNAL, False, True),))
    request = PolicyRequest(
        "write", "write", "core", arguments, approved, tool_schema_hash="schema-v1",
    )
    engine.authorize(request, customer_profile())
    for changed in (
        PolicyRequest("write", "write", "core", {"value": 2}, approved, tool_schema_hash="schema-v1"),
        PolicyRequest("write", "write", "core", arguments, approved, tool_schema_hash="schema-v2"),
    ):
        with pytest.raises(PolicyError) as rejected:
            engine.authorize(changed, customer_profile())
        assert rejected.value.code in {"approval_mismatch", "approval_schema_mismatch"}

    forged = ToolInvocationIdentity(
        "other-tenant", "user", "session", identity.workspace, identity.fence_token, approval,
    )
    with pytest.raises(PolicyError) as cross_tenant:
        engine.authorize(
            PolicyRequest("write", "write", "core", arguments, forged, tool_schema_hash="schema-v1"),
            customer_profile(),
        )
    assert cross_tenant.value.code == "identity_mismatch"
    replacement = authority.acquire(
        session_id="session", owner_id="other-owner",
        policy=LeasePolicy(60_000, 20_000, 5_000),
        expected_lease_version=identity.fence_token.lease_version,
        expected_execution_epoch=identity.fence_token.execution_epoch,
    )
    assert replacement.execution_epoch > identity.fence_token.execution_epoch
    with pytest.raises(Exception) as stale_epoch:
        epoch_guard.check(identity.fence_token)
    assert getattr(stale_epoch.value, "code", "") == "stale_epoch"


def test_json_logs_are_correlated_redacted_and_metrics_stay_low_cardinality():
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonLogFormatter(secret_values=("super-secret",)))
    logger = logging.getLogger("networkclaw_harness.test.security")
    logger.handlers[:] = [handler]
    logger.propagate = False
    logger.setLevel(logging.INFO)
    context = TraceContext.create(session_id="session", run_id="run", invocation_id="invocation")
    StructuredLogger(logger, context).event(
        "tool.completed", result={"token": "raw-token", "message": "super-secret"},
        event="forged", session_id="forged", timestamp="forged",
    )
    payload = json.loads(stream.getvalue())
    assert payload["trace_id"] == context.trace_id
    assert payload["session_id"] == "session" and payload["run_id"] == "run"
    assert payload["invocation_id"] == "invocation"
    assert payload["event"] == "tool.completed" and payload["level"] == "info"
    assert payload["timestamp"] != "forged"
    assert "super-secret" not in stream.getvalue() and "raw-token" not in stream.getvalue()

    metrics = MetricsRegistry(allowed_names=HARNESS_METRICS)
    metrics.inc("health_queries_total")
    metrics.gauge("queue_depth", 3)
    metrics.observe("protocol_latency_ms", 2.5)
    with pytest.raises(ValueError):
        metrics.inc("session_session-123_total")
    diagnostics = DiagnosticService(
        metrics, (AlertRule("queue_pressure", "queue_depth", 2),),
    ).snapshot(health="ready", readiness=True, resources={"rss_bytes": 1024})
    assert diagnostics["alerts"] == [{
        "name": "queue_pressure", "metric": "queue_depth", "value": 3.0, "threshold": 2,
    }]
    assert diagnostics["resources"] == {"rss_bytes": 1024}


def test_capacity_profiles_enforce_backpressure_and_lazy_resources():
    customer = customer_profile().capacity
    development = development_profile().capacity
    assert customer.concurrent_sessions == 64 and customer.concurrent_runs == 8
    assert customer.cpu_cores == 2 and customer.memory_bytes == 2 * 1024**3
    assert customer.open_files == 512 and customer.subprocesses == 16
    assert customer.browser_contexts == 1 and customer.mcp_servers == 4
    assert all((customer.lazy_start, customer.lazy_provider, customer.lazy_browser, customer.lazy_mcp))
    assert development.concurrent_sessions > customer.concurrent_sessions

    ledger = CapacityLedger(CapacityLimits(concurrent_runs=1, queue_depth=2, token_budget=3))
    ledger.reserve_run()
    with pytest.raises(CapacityError) as run_pressure:
        ledger.reserve_run()
    assert run_pressure.value.dimension == "concurrent_runs"
    ledger.reserve("queue_depth", 2)
    with pytest.raises(CapacityError) as queue_pressure:
        ledger.reserve("queue_depth")
    assert queue_pressure.value.dimension == "queue_depth"
    ledger.reserve_tokens(3)
    with pytest.raises(CapacityError):
        ledger.reserve_tokens(1)
    ledger.release_run()
    assert ledger.usage()["concurrent_runs"] == 0


class ResourceHandle:
    def __init__(self, session_id: str):
        self.session_id = session_id
        self.closed = False

    def discover(self):
        return ({"name": "read"},)

    def call(self, name, arguments):
        return {"status": "ok"}

    def navigate(self, url):
        return {"url": url}

    def close(self):
        self.closed = True


def test_mcp_and_browser_are_disabled_by_default_and_capacity_bounded():
    disabled = SessionResourceManager(
        mcp_factory=lambda session, server: ResourceHandle(session),
        browser_factory=ResourceHandle,
    )
    with pytest.raises(IntegrationError) as mcp:
        disabled.discover_mcp("session", "server")
    assert mcp.value.code == "mcp_disabled"
    with pytest.raises(IntegrationError) as browser:
        disabled.browser("session", enabled=False)
    assert browser.value.code == "browser_disabled"

    bounded = SessionResourceManager(
        mcp_factory=lambda session, server: ResourceHandle(session),
        browser_factory=ResourceHandle, mcp_enabled=True,
        max_mcp_servers=1, max_browser_contexts=1,
    )
    bounded.discover_mcp("session-1", "server")
    with pytest.raises(IntegrationError) as mcp_limit:
        bounded.discover_mcp("session-2", "server")
    assert mcp_limit.value.code == "mcp_capacity_exhausted"
    bounded.browser("session-1", enabled=True)
    with pytest.raises(IntegrationError) as browser_limit:
        bounded.browser("session-2", enabled=True)
    assert browser_limit.value.code == "browser_capacity_exhausted"


def test_host_rejects_cross_session_tenant_and_user_identity(tmp_path: Path):
    def command(kind: str, request_id: str, **extra):
        return {"protocol_version": "1.0", "type": kind, "request_id": request_id, **extra}

    issued = datetime(2026, 9, 18, tzinfo=timezone.utc)
    lease = {
        "session_id": "session", "owner_id": "chatsvc-a", "lease_id": "lease",
        "issued_at": issued.isoformat(),
        "renew_by": (issued + timedelta(seconds=30)).isoformat(),
        "expires_at": (issued + timedelta(seconds=60)).isoformat(),
        "grace_expires_at": (issued + timedelta(seconds=70)).isoformat(),
        "lease_version": 1, "execution_epoch": 1,
        "ttl_ms": 60_000, "renew_interval_ms": 30_000, "grace_ms": 10_000,
    }
    identity = {"tenant_id": "tenant", "user_id": "user", "session_id": "session"}
    frames = [
        command("session.open", "open", **identity, payload={
            "workspace_root": str(tmp_path / "session"), "owner_id": "chatsvc-a",
            "execution_epoch": 1, "lease": lease,
        }),
        command("user.input", "tenant-attack", tenant_id="other", user_id="user",
                session_id="session", turn_id="turn-1", payload={"text": "attack"}),
        command("user.input", "user-attack", tenant_id="tenant", user_id="other",
                session_id="session", turn_id="turn-2", payload={"text": "attack"}),
    ]
    output = io.StringIO()
    JsonlHost(io.StringIO("".join(json.dumps(item) + "\n" for item in frames)), output).serve()
    events = [json.loads(line) for line in output.getvalue().splitlines()]
    attacks = {item["request_id"]: item for item in events if item["type"] == "error"}
    assert attacks["tenant-attack"]["payload"]["code"] == "session_identity_mismatch"
    assert attacks["user-attack"]["payload"]["code"] == "session_identity_mismatch"


def test_repeatable_security_and_performance_reports(tmp_path: Path):
    security_report = tmp_path / "security.json"
    subprocess.run(
        [sys.executable, "scripts/security-scan.py", "--output", str(security_report)],
        cwd=REPO_ROOT, check=True,
    )
    security = json.loads(security_report.read_text(encoding="utf-8"))
    assert security["status"] == "passed"
    assert all(security["checks"].values()) and security["blocking_findings"] == []

    performance_report = tmp_path / "performance.json"
    subprocess.run(
        [sys.executable, "scripts/benchmark-h5.py", "--iterations", "3", "--output", str(performance_report)],
        cwd=REPO_ROOT, check=True,
    )
    performance = json.loads(performance_report.read_text(encoding="utf-8"))
    assert performance["status"] == "passed" and all(performance["checks"].values())
    assert performance["measurements"]["concurrent_sessions"] == 64
    assert performance["measurements"]["fairness_max_lead"] <= 1
    assert performance["ownership"] == "one Harness remains exclusively owned by one chatsvc"


def test_security_scan_fails_closed_on_blocking_offline_advisory(tmp_path: Path):
    sbom = json.loads((REPO_ROOT / "offline/sbom-template.json").read_text(encoding="utf-8"))
    component = next(item for item in sbom["components"] if item.get("version"))
    advisories = {
        "schema_version": 1, "as_of": "2026-09-18", "source": "test",
        "reviewed_base_images": [
            "python:3.12.11-slim-bookworm@sha256:519591d6871b7bc437060736b9f7456b8731f1499a57e22e6c285135ae657bf7"
        ],
        "package_findings": [{
            "id": "TEST-1", "name": component["name"], "version": component["version"],
            "severity": "high",
        }],
        "image_findings": [],
    }
    source = tmp_path / "advisories.json"
    report = tmp_path / "report.json"
    source.write_text(json.dumps(advisories), encoding="utf-8")
    completed = subprocess.run(
        [sys.executable, "scripts/security-scan.py", "--advisories", str(source),
         "--output", str(report)],
        cwd=REPO_ROOT, check=False,
    )
    assert completed.returncode == 1
    result = json.loads(report.read_text(encoding="utf-8"))
    assert result["status"] == "failed" and result["blocking_findings"][0]["id"] == "TEST-1"
