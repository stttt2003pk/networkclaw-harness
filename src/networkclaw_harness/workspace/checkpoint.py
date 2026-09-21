"""Epoch-bound atomic checkpoint storage."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Mapping

from .atomic import atomic_write
from .epoch import EpochGuard, FenceToken
from .session import SessionWorkspace, WorkspaceError

CHECKPOINT_FORMAT_VERSION = 1


@dataclass(frozen=True, slots=True)
class Checkpoint:
    format_version: int
    durable_cursor: str
    owner_id: str
    execution_epoch: int
    lease_id: str
    hermes_snapshot_hash: str
    artifact_manifest_hash: str
    state: Mapping[str, Any]


class CheckpointStore:
    def __init__(self, workspace: SessionWorkspace, guard: EpochGuard) -> None:
        self._workspace = workspace
        self._guard = guard
        self._path = workspace.path_for("session-state/checkpoint.json")

    def write(self, token: FenceToken, *, durable_cursor: str,
              hermes_snapshot_hash: str, artifact_manifest_hash: str,
              state: Mapping[str, Any]) -> Checkpoint:
        lease = self._guard.check(token)
        if not durable_cursor or not hermes_snapshot_hash or not artifact_manifest_hash:
            raise WorkspaceError("checkpoint cursor and hashes are required", code="invalid_checkpoint")
        checkpoint = Checkpoint(
            format_version=CHECKPOINT_FORMAT_VERSION,
            durable_cursor=durable_cursor, owner_id=lease.owner_id,
            execution_epoch=lease.execution_epoch, lease_id=lease.lease_id,
            hermes_snapshot_hash=hermes_snapshot_hash,
            artifact_manifest_hash=artifact_manifest_hash, state=dict(state),
        )
        content = json.dumps(asdict(checkpoint), sort_keys=True, separators=(",", ":")).encode("utf-8")
        self._guard.check(token)
        atomic_write(self._path, content)
        return checkpoint

    def load(self, *, durable_cursor: str, owner_id: str, execution_epoch: int) -> Checkpoint | None:
        if not self._path.exists():
            return None
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
            checkpoint = Checkpoint(**raw)
        except (OSError, json.JSONDecodeError, TypeError) as error:
            raise WorkspaceError("checkpoint is corrupt", code="checkpoint_corrupt") from error
        if checkpoint.format_version != CHECKPOINT_FORMAT_VERSION:
            raise WorkspaceError("checkpoint format is not supported", code="checkpoint_incompatible")
        if (checkpoint.durable_cursor, checkpoint.owner_id, checkpoint.execution_epoch) != (
            durable_cursor, owner_id, execution_epoch
        ):
            raise WorkspaceError("checkpoint does not match durable cursor or execution owner", code="checkpoint_stale")
        return checkpoint

    @property
    def path(self) -> Path:
        return self._path
