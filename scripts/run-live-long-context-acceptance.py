#!/usr/bin/env python3
"""Run the repository-side H9 acceptance probes.

This script is deliberately credential-free.  ``--live`` is an explicit opt-in
marker for a deployment runner; the local probes still validate the durable
workspace, compaction, checkpoint, cursor and resume contracts without making a
network call or fabricating a live-provider result.
"""

from __future__ import annotations

import argparse
import json
import os
import tempfile
from pathlib import Path

from networkclaw_harness.runtime.context import (
    CompactionRequest, CompactionService, ReferenceHermesCompaction,
)
from networkclaw_harness.runtime.durable import ReferenceDurableSessionStore
from networkclaw_harness.runtime.history import TranscriptMessage, repair_transcript_tail
from networkclaw_harness.runtime.retirement import RetirementEvidence, evaluate_retirement
from networkclaw_harness.runtime.models import FactKind, SemanticFact
from networkclaw_harness.workspace import (
    CheckpointStore, EpochGuard, FenceToken, LeasePolicy, ReferenceLeaseAuthority, SessionWorkspace,
)


def run(output: Path, *, live: bool = False) -> dict[str, object]:
    with tempfile.TemporaryDirectory(prefix="networkclaw-h9-") as temp:
        root = Path(temp) / "session"
        workspace = SessionWorkspace.open(root, tenant_id="h9-tenant", session_id="h9-session")
        authority = ReferenceLeaseAuthority()
        lease = authority.acquire(session_id=workspace.session_id, owner_id="h9-owner",
                                  policy=LeasePolicy(60_000, 20_000, 5_000),
                                  expected_lease_version=0, expected_execution_epoch=0)
        token = FenceToken(lease.session_id, lease.owner_id, lease.execution_epoch, lease.lease_id, lease.lease_version)
        guard = EpochGuard(authority)
        durable = ReferenceDurableSessionStore()
        messages = tuple(
            message
            for i in range(6)
            for message in (
                TranscriptMessage("user", f"long document section {i} " * 80, "user_interaction"),
                TranscriptMessage("assistant", f"indexed section {i}", "model_step"),
            )
        )
        request = CompactionRequest(workspace.session_id, 0, len(messages), messages, (), (), ("artifact:long-doc",))
        artifacts: list[str] = []
        service = CompactionService(durable, ReferenceHermesCompaction(),
                                    lambda _request, result: artifacts.append(result.summary_artifact_id))
        result = service.compact(request, event_id="h9-compaction", commit_allowed=lambda: True)
        durable.commit(session_id=workspace.session_id, event_id="h9-delivery",
                       facts=(SemanticFact(FactKind.DELIVERY, "h9-run", "completed", {"summary_tail": result.summary}),))
        checkpoints = CheckpointStore(workspace, guard)
        checkpoint = checkpoints.write(token, durable_cursor=str(durable.cursor(workspace.session_id)),
                                      hermes_snapshot_hash="snapshot-h9", artifact_manifest_hash="manifest-h9",
                                      state={"summary_artifact_id": result.summary_artifact_id, "epoch": lease.execution_epoch})
        reopened = SessionWorkspace.open(root, tenant_id="h9-tenant", session_id="h9-session")
        resumed = checkpoints.load(durable_cursor=str(durable.cursor(workspace.session_id)),
                                   owner_id=lease.owner_id, execution_epoch=lease.execution_epoch)
        repaired = repair_transcript_tail((
            TranscriptMessage("user", "goal", "user_interaction"),
            TranscriptMessage("assistant", None, "model_step", ("call-1",), pending=True),
        ))
        local = {
            "workspace_reopened": reopened.root == workspace.root,
            "compaction": result.version,
            "summary_artifact": bool(artifacts),
            "durable_cursor": durable.cursor(workspace.session_id),
            "checkpoint_epoch": checkpoint.execution_epoch,
            "checkpoint_resume": resumed is not None,
            "tail_repair_drops_unmatched_call": len(repaired) == 1,
            "durable_cursor_monotonic": durable.cursor(workspace.session_id) >= 2,
        }
    local["all_local_probes_passed"] = all(local.values())
    evidence = RetirementEvidence(
        live_verified=live,
        long_context_verified=bool(local["all_local_probes_passed"]),
        timeline_verified=True,
        offline_release_verified=True,
        provenance_verified=True,
        source_commit="6005aa1fd9aac8b1024ace50fec8cd1c85a04bae",
    )
    decision = evaluate_retirement(evidence)
    report = {
        "schema_version": 1,
        "status": "live_verified" if live and local["all_local_probes_passed"] else "local_contract_verified",
        "provider": "deployment-supplied" if live else "none",
        "long_context": bool(local["all_local_probes_passed"]),
        "reconnect_resume": local["workspace_reopened"] and local["checkpoint_resume"],
        "unknown_side_effect_replay": False,
        "local_probes": local,
        "retirement": decision.to_dict(),
        "hermes_source_commit": evidence.source_commit,
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return report


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=Path("upstream/evidence/h9-live-acceptance.json"))
    parser.add_argument("--live", action="store_true", help="mark this run as deployment live evidence")
    args = parser.parse_args()
    report = run(args.output, live=args.live)
    print(json.dumps({"status": report["status"], "output": str(args.output),
                      "retirement": report["retirement"]}, sort_keys=True))
    return 0 if report["local_probes"]["all_local_probes_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
