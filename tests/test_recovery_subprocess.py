from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
from pathlib import Path

from networkclaw_harness.runtime import (
    FactKind, InvocationStatus, RecoveryClassification, RecoveryReconciler,
)
from networkclaw_harness.runtime.workspace_persistence import WorkspaceDurableSessionStore
from networkclaw_harness.workspace import (
    EpochGuard, LeasePolicy, ReferenceLeaseAuthority, SessionWorkspace,
)


REPO_ROOT = Path(__file__).resolve().parents[1]
WORKER = REPO_ROOT / "tests" / "fixtures" / "recovery_crash_worker.py"


def _launch(mode: str, workspace: Path) -> subprocess.Popen[str]:
    process = subprocess.Popen(
        [sys.executable, str(WORKER), mode, str(workspace)],
        cwd=REPO_ROOT,
        env={**os.environ, "PYTHONPATH": str(REPO_ROOT / "src")},
        text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    assert process.stdout is not None
    marker = process.stdout.readline().strip()
    assert marker in {"READY", "EFFECT_SENT"}, process.stderr.read() if process.stderr else ""
    return process


def _kill(process: subprocess.Popen[str]) -> None:
    os.kill(process.pid, signal.SIGKILL)
    assert process.wait(timeout=5) == -signal.SIGKILL


def _recovery(workspace_path: Path):
    workspace = SessionWorkspace.open(workspace_path, tenant_id="tenant", session_id="session")
    durable = WorkspaceDurableSessionStore(workspace)
    authority = ReferenceLeaseAuthority()
    lease = authority.acquire(
        session_id="session", owner_id="replacement",
        policy=LeasePolicy(ttl_ms=60_000, renew_interval_ms=20_000, grace_ms=5_000),
        expected_lease_version=0, expected_execution_epoch=0,
    )
    snapshot = RecoveryReconciler(durable, EpochGuard(authority)).recover(authority.token(lease))
    return durable, snapshot


def test_sigkill_during_provider_request_clears_active_run_marker(tmp_path: Path):
    workspace = tmp_path / "provider"
    process = _launch("provider", workspace)
    _kill(process)
    durable, snapshot = _recovery(workspace)
    run = next(item for item in snapshot.items if item.kind is FactKind.RUN and item.entity_id == "run-provider")
    assert run.classification is RecoveryClassification.UNKNOWN
    assert run.state == "interrupted"
    assert any(
        stored.fact.kind is FactKind.RUN and stored.fact.entity_id == "run-provider"
        and stored.fact.state == "interrupted"
        for stored in durable.load("session")
    )


def test_sigkill_after_tool_effect_before_commit_is_unknown_and_not_replayed(tmp_path: Path):
    workspace = tmp_path / "tool"
    process = _launch("tool", workspace)
    _kill(process)
    durable, snapshot = _recovery(workspace)
    invocation = next(
        item for item in snapshot.items
        if item.kind is FactKind.INVOCATION and item.entity_id == "inv-tool"
    )
    assert invocation.classification is RecoveryClassification.UNKNOWN
    assert invocation.payload["replay_allowed"] is False
    effect = json.loads((workspace / "session-state" / "effect-observed.json").read_text())
    assert effect == {"effect": "sent", "committed": False}
    latest = [
        stored.fact for stored in durable.load("session")
        if stored.fact.kind is FactKind.INVOCATION and stored.fact.entity_id == "inv-tool"
    ][-1]
    assert latest.state == InvocationStatus.UNKNOWN.value
    assert latest.payload["replay_allowed"] is False
