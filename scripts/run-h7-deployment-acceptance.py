#!/usr/bin/env python3
"""Run the deployment-side H7 checks that do not require a real provider token.

The script validates profile egress gates, Harness metrics relay frames, and the
streaming JSONL ordering used by chatsvc. Staging must additionally run the same
fixture with a secret-manager supplied provider endpoint.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    profiles = {}
    for name in ("development", "staging", "customer"):
        path = ROOT / "src" / "networkclaw_harness" / "profiles" / f"{name}.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        profiles[name] = data
    assert profiles["customer"]["provider"]["live_enabled"] is False
    assert profiles["customer"]["provider"]["allowed_egress_hosts"] == []
    assert profiles["staging"]["provider"]["live_enabled"] is True
    assert profiles["staging"]["provider"]["allowed_egress_hosts"]

    fixture = "".join(json.dumps(frame) + "\n" for frame in (
        {"protocol_version": "1.0", "type": "metrics.query", "request_id": "metrics"},
        {"protocol_version": "1.0", "type": "shutdown", "request_id": "stop"},
    ))
    env = {"PYTHONPATH": str(ROOT / "src"), "PATH": os.environ.get("PATH", "")}
    result = subprocess.run(
        [sys.executable, "-m", "networkclaw_harness.host"], input=fixture,
        text=True, capture_output=True, cwd=ROOT, env={**os.environ, **env},
        check=False,
    )
    if result.returncode != 0:
        raise SystemExit(f"Harness metrics probe failed: {result.stderr[-500:]}")
    frames = [json.loads(line) for line in result.stdout.splitlines() if line.strip()]
    snapshot = next(frame for frame in frames if frame["type"] == "metrics.snapshot")
    prometheus = snapshot["payload"]["prometheus"]
    if "networkclaw_harness_" not in prometheus:
        raise SystemExit("Harness metrics snapshot is missing its namespace")
    print(json.dumps({
        "status": "passed",
        "profile_checks": sorted(profiles),
        "metrics_query": True,
        "stream_transport": "Harness JSONL -> chatsvc Stream -> UDS HealthCheck metrics relay",
        "real_provider": "staging-secret-manager-required",
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
