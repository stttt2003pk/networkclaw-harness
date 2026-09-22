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


def patch_series_hash(paths: list[Path]) -> str:
    digest = hashlib.sha256()
    for path in paths:
        digest.update(path.name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
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


def git_output(*args: str) -> str:
    return subprocess.check_output(
        ["git", "-C", str(REPO_ROOT), *args], text=True, stderr=subprocess.STDOUT
    ).strip()


def verified_vendored_source(expected_commit: str) -> Path:
    vendor = REPO_ROOT / "vendor/hermes"
    manifest_path = REPO_ROOT / "upstream/hermes-vendor-manifest.json"
    if not vendor.is_dir() or not manifest_path.is_file():
        raise SystemExit("pinned Git history is absent and no vendored source snapshot is available")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("source_commit") != expected_commit:
        raise SystemExit("vendored source snapshot does not match the pinned commit")
    actual = {
        path.relative_to(vendor).as_posix(): sha256(path)
        for path in sorted(vendor.rglob("*")) if path.is_file() and path.name != ".gitkeep"
        and "__pycache__" not in path.relative_to(vendor).parts
        and path.suffix not in {".pyc", ".pyo"}
    }
    if actual != manifest.get("files"):
        raise SystemExit("vendored source snapshot failed its file hash manifest")
    return vendor


def display_path(path: Path) -> str:
    resolved = path.resolve(strict=False)
    try:
        return resolved.relative_to(REPO_ROOT).as_posix()
    except ValueError:
        return str(resolved)


def copy_allowlisted_path(source: Path, staging: Path, relative: Path) -> None:
    source_path = source / relative
    if source_path.is_dir():
        shutil.copytree(source_path, staging / relative, dirs_exist_ok=True)
    elif source_path.is_file():
        destination_path = staging / relative
        destination_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source_path, destination_path)
    else:
        raise SystemExit(f"allowlist entry does not exist in source: {relative}")


def patch_targets(path: Path) -> tuple[Path, ...]:
    targets = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith("+++ b/"):
            targets.append(Path(line[6:]))
    return tuple(targets)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--allowlist", type=Path, default=REPO_ROOT / "upstream/hermes-runtime-files.txt"
    )
    parser.add_argument("--destination", type=Path, default=REPO_ROOT / "vendor/hermes")
    parser.add_argument(
        "--manifest", type=Path, default=REPO_ROOT / "upstream/hermes-vendor-manifest.json"
    )
    parser.add_argument(
        "--capability-manifest", type=Path,
        default=REPO_ROOT / "upstream/hermes-runtime-capabilities.json",
    )
    parser.add_argument(
        "--patch-directory", type=Path, default=REPO_ROOT / "upstream/patches",
        help="directory containing the ordered Hermes patch series",
    )
    parser.add_argument(
        "--source",
        type=Path,
        help="optional clean checkout of the pinned commit; default extracts from this repository history",
    )
    args = parser.parse_args()

    expected = json.loads((REPO_ROOT / "upstream/hermes-source.json").read_text())
    actual_commit = expected["commit"]
    repository_history_available = True
    try:
        if git_output("rev-parse", expected["commit"]) != expected["commit"]:
            raise subprocess.CalledProcessError(1, "git rev-parse")
    except (subprocess.CalledProcessError, FileNotFoundError):
        repository_history_available = False

    entries = load_allowlist(args.allowlist)
    patch_paths = sorted(args.patch_directory.glob("*.patch"))

    with tempfile.TemporaryDirectory(prefix="networkclaw-hermes-sync-") as temporary:
        temporary_root = Path(temporary)
        source = temporary_root / "source"
        source_is_post_patch = False
        if args.source:
            source = args.source.resolve(strict=True)
            source_commit = subprocess.check_output(
                ["git", "-C", str(source), "rev-parse", "HEAD"], text=True
            ).strip()
            if source_commit != actual_commit:
                raise SystemExit(f"source commit {source_commit} != pinned {actual_commit}")
            if subprocess.check_output(
                ["git", "-C", str(source), "status", "--porcelain"], text=True
            ).strip():
                raise SystemExit("source checkout must be clean")
        elif repository_history_available:
            archive = subprocess.run(
                ["git", "-C", str(REPO_ROOT), "archive", "--format=tar", actual_commit],
                check=True,
                stdout=subprocess.PIPE,
            )
            source.mkdir()
            subprocess.run(["tar", "-xf", "-", "-C", str(source)], input=archive.stdout, check=True)
        else:
            source = verified_vendored_source(actual_commit)
            source_is_post_patch = True

        staging = Path(temporary) / "hermes"
        staging.mkdir()
        for relative in entries:
            copy_allowlisted_path(source, staging, relative)

        if not source_is_post_patch:
            for patch_path in patch_paths:
                targets = patch_targets(patch_path)
                if targets and any(not (staging / target).exists() for target in targets):
                    continue
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
        replacement = temporary_root / "replacement"
        shutil.copytree(staging, replacement)
        if destination.exists():
            shutil.rmtree(destination)
        shutil.move(str(replacement), str(destination))

    manifest = {
        "schema_version": 2,
        "source_repository": expected["repository"],
        "source_commit": actual_commit,
        "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "allowlist": display_path(args.allowlist),
        "allowlist_sha256": sha256(args.allowlist),
        "patches": [
            {"name": path.name, "sha256": sha256(path)} for path in patch_paths
        ],
        "patch_series_sha256": patch_series_hash(patch_paths),
        "license": {"path": "vendor/hermes/LICENSE", "sha256": sha256(destination / "LICENSE")
                    if (destination / "LICENSE").is_file() else None},
        "sbom": {"path": "offline/hermes-sbom.json",
                 "sha256": sha256(REPO_ROOT / "offline/hermes-sbom.json")
                 if (REPO_ROOT / "offline/hermes-sbom.json").is_file() else None},
        "capability_manifest": {
            "path": display_path(args.capability_manifest),
            "sha256": sha256(args.capability_manifest)
            if args.capability_manifest.is_file() else None,
        },
        "files": files,
    }
    args.manifest.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    print(f"synced {len(files)} files from {actual_commit}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
