#!/usr/bin/env python3
"""Verify every vendored file against the generated post-patch manifest."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
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


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--vendor", type=Path, default=REPO_ROOT / "vendor/hermes")
    parser.add_argument(
        "--manifest", type=Path, default=REPO_ROOT / "upstream/hermes-vendor-manifest.json"
    )
    parser.add_argument(
        "--allowlist", type=Path, default=REPO_ROOT / "upstream/hermes-runtime-files.txt"
    )
    parser.add_argument(
        "--patch-directory", type=Path, default=REPO_ROOT / "upstream/patches"
    )
    args = parser.parse_args()

    if not args.manifest.exists():
        raise SystemExit("vendor manifest is absent; run sync-hermes-runtime.py after H0 tracing")
    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    source = json.loads((REPO_ROOT / "upstream/hermes-source.json").read_text(encoding="utf-8"))
    if manifest.get("schema_version") not in {1, 2}:
        raise SystemExit("unsupported vendor manifest schema")
    if manifest.get("source_commit") != source["commit"]:
        raise SystemExit("vendor manifest source commit does not match pinned source")
    if manifest.get("allowlist_sha256") != sha256(args.allowlist):
        raise SystemExit("vendor manifest allowlist hash does not match current allowlist")
    expected_patches = [
        {"name": path.name, "sha256": sha256(path)}
        for path in sorted(args.patch_directory.glob("*.patch"))
    ]
    if manifest.get("patches") != expected_patches:
        raise SystemExit("vendor manifest patch series does not match current patches")
    if manifest.get("patch_series_sha256") not in {None, patch_series_hash(sorted(args.patch_directory.glob("*.patch")))}:
        raise SystemExit("vendor manifest patch series hash does not match current patches")
    license_record = manifest.get("license")
    if license_record and license_record.get("sha256"):
        license_path = args.vendor / "LICENSE"
        if not license_path.is_file() or sha256(license_path) != license_record["sha256"]:
            raise SystemExit("vendor license provenance does not match manifest")
    capability = manifest.get("capability_manifest")
    if capability and capability.get("sha256"):
        capability_path = REPO_ROOT / capability["path"]
        if not capability_path.is_file() or sha256(capability_path) != capability["sha256"]:
            raise SystemExit("runtime capability manifest does not match vendor manifest")
    expected = manifest["files"]
    actual = {
        path.relative_to(args.vendor).as_posix(): sha256(path)
        for path in sorted(args.vendor.rglob("*"))
        if path.is_file() and path.name != ".gitkeep"
        and "__pycache__" not in path.relative_to(args.vendor).parts
        and path.suffix not in {".pyc", ".pyo"}
    }
    if actual != expected:
        missing = sorted(set(expected) - set(actual))
        unexpected = sorted(set(actual) - set(expected))
        changed = sorted(path for path in set(actual) & set(expected) if actual[path] != expected[path])
        raise SystemExit(
            f"vendor verification failed: missing={missing}, unexpected={unexpected}, changed={changed}"
        )
    print(f"verified {len(actual)} vendored files")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
