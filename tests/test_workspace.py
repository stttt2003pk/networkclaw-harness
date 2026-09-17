from pathlib import Path

import pytest

from networkclaw_harness.workspace import REQUIRED_DIRECTORIES, SessionWorkspace, WorkspaceError


def test_workspace_creates_recoverable_layout_and_rejects_escape(tmp_path: Path):
    workspace = SessionWorkspace.open(tmp_path / "tenant" / "session")

    assert all((workspace.root / relative).is_dir() for relative in REQUIRED_DIRECTORIES)
    with pytest.raises(WorkspaceError, match="cannot contain"):
        workspace.path_for("../other-session/state.json")


def test_workspace_rejects_symlink_escape(tmp_path: Path):
    assigned = tmp_path / "assigned"
    assigned.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (assigned / "escape").symlink_to(outside, target_is_directory=True)
    workspace = SessionWorkspace.open(assigned)

    with pytest.raises(WorkspaceError, match="escapes"):
        workspace.path_for("escape/result.json")

