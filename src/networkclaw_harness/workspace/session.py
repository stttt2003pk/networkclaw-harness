"""A host-assigned session workspace with escape-resistant child paths."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path, PurePath

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


@dataclass(frozen=True, slots=True)
class SessionWorkspace:
    root: Path

    @classmethod
    def open(cls, assigned_root: str | Path) -> "SessionWorkspace":
        raw_root = Path(assigned_root)
        if not raw_root.is_absolute():
            raise WorkspaceError("workspace root must be an absolute host-assigned path")

        raw_root.mkdir(parents=True, exist_ok=True)
        root = raw_root.resolve(strict=True)
        if not root.is_dir():
            raise WorkspaceError("workspace root must be a directory")

        workspace = cls(root=root)
        for relative in REQUIRED_DIRECTORIES:
            workspace.path_for(relative, create_directory=True)
        return workspace

    def path_for(self, relative: str | PurePath, *, create_directory: bool = False) -> Path:
        candidate_relative = Path(relative)
        if candidate_relative.is_absolute() or ".." in candidate_relative.parts:
            raise WorkspaceError("workspace path must be relative and cannot contain '..'")

        candidate = (self.root / candidate_relative).resolve(strict=False)
        if not candidate.is_relative_to(self.root):
            raise WorkspaceError("workspace path escapes the assigned session root")

        if create_directory:
            candidate.mkdir(parents=True, exist_ok=True)
            if not candidate.resolve(strict=True).is_relative_to(self.root):
                raise WorkspaceError("workspace directory resolves outside the assigned root")
        return candidate

