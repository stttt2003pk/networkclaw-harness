"""A host-assigned, identity-bound session workspace."""

from __future__ import annotations

import json
import fcntl
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePath

from .atomic import atomic_write

WORKSPACE_FORMAT_VERSION = 1
IDENTITY_FILE = ".networkclaw-workspace.json"
REQUIRED_DIRECTORIES = (
    "session-state",
    "artifacts/raw",
    "artifacts/normalized",
    "artifacts/evidence",
    "artifacts/generated",
    "summaries",
    "indexes",
    "tmp",
)


class WorkspaceError(ValueError):
    """The assigned workspace or requested child path is unsafe."""

    def __init__(self, message: str, *, code: str = "invalid_workspace") -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class SessionWorkspace:
    root: Path
    tenant_id: str
    session_id: str
    format_version: int = WORKSPACE_FORMAT_VERSION

    @classmethod
    def open(cls, assigned_root: str | Path, *, tenant_id: str, session_id: str) -> "SessionWorkspace":
        if not tenant_id or not tenant_id.isascii() or not session_id or not session_id.isascii():
            raise WorkspaceError("tenant_id and session_id must be non-empty ASCII strings")
        raw_root = Path(assigned_root)
        if not raw_root.is_absolute():
            raise WorkspaceError("workspace root must be an absolute host-assigned path")
        if raw_root.is_symlink():
            raise WorkspaceError("workspace root cannot be a symbolic link")
        raw_root.mkdir(parents=True, exist_ok=True)
        root = raw_root.resolve(strict=True)
        if not root.is_dir():
            raise WorkspaceError("workspace root must be a directory")

        workspace = cls(root=root, tenant_id=tenant_id, session_id=session_id)
        for relative in REQUIRED_DIRECTORIES:
            workspace.path_for(relative, create_directory=True)
        workspace._bind_identity()
        return workspace

    def path_for(self, relative: str | PurePath, *, create_directory: bool = False) -> Path:
        candidate_relative = Path(relative)
        if not candidate_relative.parts or candidate_relative.is_absolute() or ".." in candidate_relative.parts:
            raise WorkspaceError("workspace path must be relative, non-empty, and cannot contain '..'")
        candidate = self.root / candidate_relative
        self._reject_symlink_components(candidate_relative)
        resolved = candidate.resolve(strict=False)
        if not resolved.is_relative_to(self.root):
            raise WorkspaceError("workspace path escapes the assigned session root")
        if create_directory:
            candidate.mkdir(parents=True, exist_ok=True)
            self._reject_symlink_components(candidate_relative)
            if not candidate.resolve(strict=True).is_relative_to(self.root):
                raise WorkspaceError("workspace directory resolves outside the assigned root")
        return candidate

    @property
    def identity_path(self) -> Path:
        return self.root / IDENTITY_FILE

    def _bind_identity(self) -> None:
        lock_path = self.root / ".networkclaw-workspace.lock"
        with lock_path.open("a+b") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            self._bind_identity_locked()

    def _bind_identity_locked(self) -> None:
        if self.identity_path.exists():
            try:
                current = json.loads(self.identity_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as error:
                raise WorkspaceError("workspace identity metadata is unreadable", code="workspace_corrupt") from error
            expected = {
                "format_version": self.format_version,
                "tenant_id": self.tenant_id,
                "session_id": self.session_id,
            }
            if any(current.get(key) != value for key, value in expected.items()):
                raise WorkspaceError("workspace tenant, session, or format identity does not match", code="workspace_identity_mismatch")
            return
        metadata = {
            "format_version": self.format_version,
            "tenant_id": self.tenant_id,
            "session_id": self.session_id,
            "created_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        }
        atomic_write(self.identity_path, json.dumps(metadata, sort_keys=True, separators=(",", ":")).encode("utf-8"))

    def _reject_symlink_components(self, relative: Path) -> None:
        current = self.root
        for part in relative.parts:
            current = current / part
            if current.is_symlink():
                raise WorkspaceError("workspace path contains a symbolic link")
