#!/usr/bin/env python3
"""Offline smoke and policy checks for the pinned Hermes runtime closure."""

from __future__ import annotations

import argparse
import importlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VENDOR = ROOT / "vendor" / "hermes"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", type=Path, default=ROOT / "upstream/hermes-runtime-capabilities.json")
    args = parser.parse_args()
    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    source = json.loads((ROOT / "upstream/hermes-source.json").read_text(encoding="utf-8"))
    if manifest.get("source_commit") != source.get("commit"):
        raise SystemExit("capability manifest source commit does not match pinned source")
    sys.path.insert(0, str(VENDOR))
    for module in ("agent.iteration_budget", "agent.tool_guardrails", "hermes_state", "run_agent"):
        importlib.import_module(module)
    # Upstream interactive entry points legitimately contain HOME/cwd fallbacks.
    # The closure check therefore audits the NetworkClaw boundary, where those
    # fallbacks must not be reachable, instead of pretending the upstream app is
    # already a headless library.
    boundary = (ROOT / "src/networkclaw_harness/runtime/hermes_bootstrap.py").read_text(encoding="utf-8")
    if any(token in boundary for token in ("Path.home(", "os.getcwd(", "HERMES_HOME")):
        raise SystemExit("Hermes bootstrap must not infer workspace from global state")
    for entry in manifest.get("entries", ()):
        if entry.get("boundary") == "disabled" and "install" in entry.get("capability", ""):
            continue
        if not entry.get("owner") or not entry.get("lifecycle"):
            raise SystemExit("capability entries require owner and lifecycle")
    print("hermes runtime closure: imports=ok offline=ok globals=ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
