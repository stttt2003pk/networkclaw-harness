#!/usr/bin/env python3
"""Populate the controlled CPython 3.12 wheelhouse from hashed lock files."""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
LOCKS = ("requirements.lock", "requirements-build.lock", "requirements-dev.lock")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--destination", type=Path, default=REPO_ROOT / "offline/wheels")
    parser.add_argument("--platform", default="manylinux2014_x86_64")
    args = parser.parse_args()
    if sys.version_info[:2] != (3, 12):
        raise SystemExit("wheelhouse preparation requires CPython 3.12")
    args.destination.mkdir(parents=True, exist_ok=True)
    for lock_name in LOCKS:
        subprocess.run([
            sys.executable, "-m", "pip", "download", "--require-hashes",
            "--only-binary=:all:", "--python-version=312", "--implementation=cp",
            f"--platform={args.platform}", "--dest", str(args.destination),
            "--requirement", str(REPO_ROOT / lock_name),
        ], check=True)
    print(f"prepared {len(tuple(args.destination.glob('*.whl')))} wheels")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
