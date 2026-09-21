"""Shared deterministic release helpers; standard-library only."""

from __future__ import annotations

import gzip
import hashlib
import json
import os
import tarfile
from pathlib import Path
from typing import Iterable

EXCLUDED_PARTS = frozenset({
    ".git", ".idea", ".pytest_cache", ".ruff_cache", ".venv", "venv",
    "__pycache__", "dist", "build", "htmlcov",
})
SOURCE_ROOT_FILES = (
    ".dockerignore", ".gitignore", ".python-version", "AGENTS.md", "Dockerfile", "Makefile",
    "README.md", "THIRD_PARTY_NOTICES.md", "poetry.lock", "pyproject.toml",
    "requirements.lock", "requirements-build.lock", "requirements-dev.lock",
)
SOURCE_ROOT_DIRS = ("LICENSES", "deploy", "offline", "scripts", "src", "tests", "upstream", "vendor")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def canonical_json(value: object) -> bytes:
    return (json.dumps(value, indent=2, sort_keys=True, ensure_ascii=True) + "\n").encode("utf-8")


def source_files(root: Path) -> tuple[Path, ...]:
    files: list[Path] = []
    for name in SOURCE_ROOT_FILES:
        path = root / name
        if not path.is_file():
            raise ValueError(f"required source file is missing: {name}")
        files.append(path)
    for name in SOURCE_ROOT_DIRS:
        directory = root / name
        if not directory.is_dir():
            raise ValueError(f"required source directory is missing: {name}")
        for path in directory.rglob("*"):
            relative = path.relative_to(root)
            if any(part in EXCLUDED_PARTS for part in relative.parts):
                continue
            if relative.parts[:2] == ("offline", "wheels") and path.name != ".gitkeep":
                continue
            if path.is_symlink():
                raise ValueError(f"source release cannot contain a symlink: {relative}")
            if path.is_file() and path.suffix not in {".pyc", ".pyo"}:
                files.append(path)
    return tuple(sorted(set(files), key=lambda item: item.relative_to(root).as_posix()))


def tree_hash(root: Path, files: Iterable[Path]) -> str:
    digest = hashlib.sha256()
    for path in files:
        relative = path.relative_to(root).as_posix()
        digest.update(relative.encode("utf-8") + b"\0")
        digest.update(sha256_file(path).encode("ascii") + b"\n")
    return digest.hexdigest()


def deterministic_tar(source: Path, destination: Path, *, prefix: str, epoch: int) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open("wb") as raw:
        with gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=epoch) as compressed:
            with tarfile.open(fileobj=compressed, mode="w", format=tarfile.PAX_FORMAT) as archive:
                for path in sorted(source.rglob("*"), key=lambda item: item.relative_to(source).as_posix()):
                    if path.is_symlink():
                        raise ValueError(f"archive cannot contain a symlink: {path}")
                    relative = path.relative_to(source).as_posix()
                    info = archive.gettarinfo(str(path), arcname=f"{prefix}/{relative}")
                    info.uid = info.gid = 0
                    info.uname = info.gname = "root"
                    info.mtime = epoch
                    info.mode = 0o755 if path.is_dir() or os.access(path, os.X_OK) else 0o644
                    if path.is_file():
                        with path.open("rb") as payload:
                            archive.addfile(info, payload)
                    else:
                        archive.addfile(info)


def safe_extract(archive_path: Path, destination: Path) -> Path:
    with tarfile.open(archive_path, "r:gz") as archive:
        members = archive.getmembers()
        for member in members:
            target = (destination / member.name).resolve()
            if not target.is_relative_to(destination.resolve()) or member.issym() or member.islnk():
                raise ValueError("release archive contains an unsafe path")
        archive.extractall(destination, filter="data")
    roots = tuple(path for path in destination.iterdir() if path.is_dir())
    if len(roots) != 1:
        raise ValueError("release archive must contain exactly one root directory")
    return roots[0]
