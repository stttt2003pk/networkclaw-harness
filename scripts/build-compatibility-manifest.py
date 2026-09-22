#!/usr/bin/env python3
"""Emit the cross-repository release contract used by independent verifiers."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path


def git(root: Path, *args: str) -> str:
    return subprocess.check_output(["git", "-C", str(root), *args], text=True).strip()


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--networkclaw", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--allow-dirty", action="store_true")
    args = parser.parse_args()
    harness = Path(__file__).resolve().parents[1]
    networkclaw = args.networkclaw.resolve()
    dirty = {"harness": bool(git(harness, "status", "--porcelain")),
             "networkclaw": bool(git(networkclaw, "status", "--porcelain"))}
    if any(dirty.values()) and not args.allow_dirty:
        raise SystemExit("compatibility manifest requires clean repositories")
    vendor = json.loads((harness / "upstream/hermes-source.json").read_text(encoding="utf-8"))
    manifest = {
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "repositories": {
            "networkclaw": {"commit": git(networkclaw, "rev-parse", "HEAD"), "dirty": dirty["networkclaw"]},
            "networkclaw_harness": {"commit": git(harness, "rev-parse", "HEAD"), "dirty": dirty["harness"]},
        },
        "hermes": {"source_commit": vendor["commit"],
                   "vendor_manifest_sha256": sha256(harness / "upstream/hermes-vendor-manifest.json")},
        "host_protocol": {"version": "1.0", "terminal_types": ["turn.completed", "turn.failed", "turn.cancelled"],
                          "terminal_fields": ["outcome", "reason_code", "execution_epoch", "generation", "end"],
                          "epoch_fencing": "strict-monotonic"},
        "reason_codes": ["user_cancel", "platform_lease_lost", "epoch_takeover", "provider_interrupted",
                         "turn_timeout", "liveness_timeout", "session_closed", "harness_shutdown"],
        "profiles": {"python": "CPython 3.12", "target": "ubuntu-22.04-linux-amd64-cp312"},
        "acceptance": {
            "go_interop": "go test ./tests/integration/harnessinterop -count=1",
            "go_race": "go test -race ./internal/chatsvc/harness ./internal/chatsvc/session -count=1",
            "harness": "scripts/run_tests.sh -q",
            "offline_verifier": "scripts/verify-offline-release.py",
        },
        "publishable": not any(dirty.values()),
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
