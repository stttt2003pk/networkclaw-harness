#!/usr/bin/env python3
"""Generate vendor/hermes from a pinned full fork and an explicit allowlist."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_allowlist(path: Path) -> list[Path]:
    entries = []
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        relative = Path(line)
        if relative.is_absolute() or ".." in relative.parts:
            raise SystemExit(f"unsafe allowlist entry: {line}")
        entries.append(relative)
    if not entries:
        raise SystemExit("runtime allowlist is empty; H0 tracing must run before vendor sync")
    return entries


def git_output(source: Path, *args: str) -> str:
    return subprocess.check_output(
        ["git", "-C", str(source), *args], text=True, stderr=subprocess.STDOUT
    ).strip()


def display_path(path: Path) -> str:
    resolved = path.resolve(strict=False)
    try:
        return resolved.relative_to(REPO_ROOT).as_posix()
    except ValueError:
        return str(resolved)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path, help="full networkclaw-hermes-fork checkout")
    parser.add_argument(
        "--allowlist", type=Path, default=REPO_ROOT / "upstream/hermes-runtime-files.txt"
    )
    parser.add_argument("--destination", type=Path, default=REPO_ROOT / "vendor/hermes")
    parser.add_argument(
        "--manifest", type=Path, default=REPO_ROOT / "upstream/hermes-vendor-manifest.json"
    )
    args = parser.parse_args()

    source = args.source.resolve(strict=True)
    expected = json.loads((REPO_ROOT / "upstream/hermes-source.json").read_text())
    actual_commit = git_output(source, "rev-parse", "HEAD")
    if actual_commit != expected["commit"]:
        raise SystemExit(f"source commit {actual_commit} != pinned {expected['commit']}")
    if git_output(source, "status", "--porcelain"):
        raise SystemExit("source checkout must be clean")

    entries = load_allowlist(args.allowlist)
    patch_paths = sorted((REPO_ROOT / "upstream/patches").glob("*.patch"))

    with tempfile.TemporaryDirectory(prefix="networkclaw-hermes-sync-") as temporary:
        staging = Path(temporary) / "hermes"
        staging.mkdir()
        for relative in entries:
            source_path = (source / relative).resolve(strict=True)
            if not source_path.is_relative_to(source):
                raise SystemExit(f"allowlist entry escapes source: {relative}")
            destination_path = staging / relative
            destination_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source_path, destination_path)

        for patch_path in patch_paths:
            subprocess.run(
                ["git", "apply", "--whitespace=error-all", str(patch_path.resolve())],
                cwd=staging,
                check=True,
            )

        files = {
            path.relative_to(staging).as_posix(): sha256(path)
            for path in sorted(staging.rglob("*"))
            if path.is_file()
        }
        destination = args.destination.resolve(strict=False)
        if destination.exists():
            shutil.rmtree(destination)
        shutil.copytree(staging, destination)

    manifest = {
        "schema_version": 1,
        "source_repository": expected["repository"],
        "source_commit": actual_commit,
        "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "allowlist": display_path(args.allowlist),
        "patches": [path.name for path in patch_paths],
        "files": files,
    }
    args.manifest.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    print(f"synced {len(files)} files from {actual_commit}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
