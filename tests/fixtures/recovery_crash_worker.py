"""Subprocess fixture that dies with durable provider/tool work in flight."""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

from networkclaw_harness.runtime.models import FactKind, InvocationStatus, SemanticFact
from networkclaw_harness.runtime.workspace_persistence import WorkspaceDurableSessionStore
from networkclaw_harness.workspace import SessionWorkspace


def main() -> None:
    mode, workspace_text = sys.argv[1:3]
    workspace = SessionWorkspace.open(Path(workspace_text), tenant_id="tenant", session_id="session")
    durable = WorkspaceDurableSessionStore(workspace)
    if mode == "provider":
        durable.commit(
            session_id="session", event_id="provider-active",
            facts=(SemanticFact(FactKind.RUN, "run-provider", "running", {
                "phase": "provider_request", "replay_allowed": False,
            }),),
        )
        print("READY", flush=True)
    elif mode == "tool":
        durable.commit(
            session_id="session", event_id="tool-intent",
            facts=(SemanticFact(FactKind.INVOCATION, "inv-tool", InvocationStatus.RUNNING, {
                "run_id": "run-tool", "tool_name": "network.change", "read_only": False,
                "idempotent": True, "retryable": True, "side_effecting": True,
                "idempotency_key": "operation-1", "event_dedupe_key": "event-1",
                "status_query_key": "change-42", "replay_allowed": False,
            }),),
        )
        workspace.path_for("session-state/effect-observed.json").write_text(
            json.dumps({"effect": "sent", "committed": False}), encoding="utf-8",
        )
        print("EFFECT_SENT", flush=True)
    else:
        raise SystemExit(f"unknown mode: {mode}")
    while True:
        time.sleep(1)


if __name__ == "__main__":
    main()
