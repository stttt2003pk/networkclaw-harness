#!/usr/bin/env python3
"""Run the offline H0 runtime fixture suite against one Hermes source root."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
TEST_FILES = (
    "agent/test_context_compressor.py",
    "agent/test_memory_provider.py",
    "agent/test_provider_client_seam.py",
    "agent/test_provider_fallback.py",
    "agent/test_skill_utils.py",
    "agent/test_subagent_lifecycle.py",
    "plugins/browser/test_browser_provider_plugins.py",
    "tools/test_registry.py",
    "tools/test_memory_tool_schema.py",
    "tools/test_mcp_schema_cache.py",
    "tools/test_delegate_subagent_timeout_diagnostic.py",
    "test_mcp_serve.py",
)
SUMMARY_PATTERN = re.compile(r"(?P<count>\d+) passed")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("runtime_root", type=Path)
    parser.add_argument("tests_root", type=Path)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--source-kind", choices=("pinned_source", "vendor_snapshot"), required=True)
    args = parser.parse_args()
    runtime_root = args.runtime_root.resolve(strict=True)
    tests_root = args.tests_root.resolve(strict=True)
    environment = os.environ.copy()
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    environment["PYTHONPATH"] = str(runtime_root)
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", *(tests_root / test for test in TEST_FILES)],
        text=True,
        capture_output=True,
        env=environment,
    )
    if result.returncode != 0:
        raise SystemExit(result.stderr or result.stdout)
    match = SUMMARY_PATTERN.search(result.stdout)
    if not match:
        raise SystemExit("pytest completed without a passed-test summary")

    source = json.loads((REPO_ROOT / "upstream/hermes-source.json").read_text(encoding="utf-8"))
    report = {
        "probe_id": "H0-P02-P08-fixture-suite",
        "source_commit": source["commit"],
        "source_kind": args.source_kind,
        "test_files": list(TEST_FILES),
        "tests_passed": int(match.group("count")),
        "result": "passed",
        "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    }
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"H0 suite passed: {report['tests_passed']} tests")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
