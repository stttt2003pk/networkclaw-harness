#!/usr/bin/env python3
"""Verify every vendored file against the generated post-patch manifest."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--vendor", type=Path, default=REPO_ROOT / "vendor/hermes")
    parser.add_argument(
        "--manifest", type=Path, default=REPO_ROOT / "upstream/hermes-vendor-manifest.json"
    )
    args = parser.parse_args()

    if not args.manifest.exists():
        raise SystemExit("vendor manifest is absent; run sync-hermes-runtime.py after H0 tracing")
    expected = json.loads(args.manifest.read_text(encoding="utf-8"))["files"]
    actual = {
        path.relative_to(args.vendor).as_posix(): sha256(path)
        for path in sorted(args.vendor.rglob("*"))
        if path.is_file() and path.name != ".gitkeep"
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

