from __future__ import annotations

import json
from pathlib import Path

import pytest

from networkclaw_harness.runtime import HermesBoundary, HermesBootstrapError, open_hermes_runtime
from networkclaw_harness.workspace import SessionWorkspace


ROOT = Path(__file__).resolve().parents[1]


def test_capability_manifest_has_provenance_and_frozen_boundaries() -> None:
    manifest = json.loads((ROOT / "upstream/hermes-runtime-capabilities.json").read_text())
    assert manifest["source_commit"] == json.loads((ROOT / "upstream/hermes-source.json").read_text())["commit"]
    capabilities = {item["capability"]: item for item in manifest["entries"]}
    assert set(manifest["boundary_categories"]) == {item.value for item in HermesBoundary}
    assert {item["boundary"] for item in manifest["entries"]} >= {
        HermesBoundary.DIRECT, HermesBoundary.ADAPTER, HermesBoundary.DISABLED,
    }
    assert all(item["owner"] and item["lifecycle"] for item in capabilities.values())
    assert capabilities["state.session_db"]["boundary"] == HermesBoundary.ADAPTER


def test_bootstrap_uses_only_assigned_workspace(tmp_path: Path) -> None:
    workspace = SessionWorkspace.open(tmp_path / "session", tenant_id="tenant", session_id="s")
    runtime = open_hermes_runtime(workspace)
    try:
        assert runtime.session_db_path == workspace.path_for("session-state/hermes-session.db")
        assert runtime.session_db_path.exists()
        run = runtime.create_run(
            run_id="run-1", max_iterations=2,
            provider_factory=lambda assigned: ("provider", assigned.root),
            compressor_factory=lambda assigned: ("compressor", assigned.root),
        )
        assert run.workspace is workspace
        assert run.provider_transport[1] == workspace.root
        assert run.compressor[1] == workspace.root
        assert run.iteration_budget.max_total == 2
    finally:
        runtime.close()


def test_bootstrap_rejects_non_workspace_objects() -> None:
    with pytest.raises(HermesBootstrapError):
        open_hermes_runtime(Path("/tmp/implicit"))  # type: ignore[arg-type]
