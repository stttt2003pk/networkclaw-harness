#!/usr/bin/env python3
"""Build the Python 3.12 wheelhouse and bind it to the current source commit."""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> int:
    if sys.version_info[:2] != (3, 12):
        raise SystemExit("offline releases must be built with CPython 3.12")

    dirty = subprocess.check_output(
        ["git", "-C", str(REPO_ROOT), "status", "--porcelain"], text=True
    ).strip()
    if dirty:
        raise SystemExit("offline releases require a clean, committed source tree")

    vendor_manifest = REPO_ROOT / "upstream/hermes-vendor-manifest.json"
    if not vendor_manifest.exists():
        raise SystemExit("Hermes vendor snapshot is not initialized")

    wheelhouse = REPO_ROOT / "offline/wheels"
    wheelhouse.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            sys.executable,
            "-m",
            "pip",
            "download",
            "--require-hashes",
            "--dest",
            str(wheelhouse),
            "--requirement",
            str(REPO_ROOT / "requirements.lock"),
        ],
        check=True,
    )

    commit = subprocess.check_output(
        ["git", "-C", str(REPO_ROOT), "rev-parse", "HEAD"], text=True
    ).strip()
    wheels = {
        path.name: file_sha256(path)
        for path in sorted(wheelhouse.iterdir())
        if path.is_file() and path.name != ".gitkeep"
    }
    manifest = {
        "schema_version": 1,
        "source_commit": commit,
        "python": f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}",
        "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "requirements_lock_sha256": file_sha256(REPO_ROOT / "requirements.lock"),
        "hermes_vendor_manifest_sha256": file_sha256(vendor_manifest),
        "wheels": wheels,
    }
    (REPO_ROOT / "offline/release-manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n"
    )
    print(f"wrote offline manifest for {len(wheels)} wheels")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
