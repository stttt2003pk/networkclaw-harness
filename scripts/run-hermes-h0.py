#!/usr/bin/env python3
"""Run and record the import-safe H0-P01 Hermes extraction probe."""

from __future__ import annotations

import argparse
import importlib
import json
import os
import platform
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
MODULES = ("agent", "tools", "hermes_constants")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--vendor", type=Path, default=REPO_ROOT / "vendor/hermes")
    parser.add_argument(
        "--archive-source",
        action="store_true",
        help="import the pinned source commit from a temporary git archive instead of vendor",
    )
    parser.add_argument(
        "--report", type=Path, default=REPO_ROOT / "upstream/evidence/h0-p01-vendor.json"
    )
    args = parser.parse_args()
    os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
    sys.dont_write_bytecode = True
    source = json.loads((REPO_ROOT / "upstream/hermes-source.json").read_text(encoding="utf-8"))
    temporary = tempfile.TemporaryDirectory(prefix="networkclaw-hermes-h0-") if args.archive_source else None
    try:
        if temporary:
            root = Path(temporary.name)
            archive = subprocess.run(
                ["git", "-C", str(REPO_ROOT), "archive", "--format=tar", source["commit"]],
                check=True,
                stdout=subprocess.PIPE,
            )
            subprocess.run(["tar", "-xf", "-", "-C", str(root)], input=archive.stdout, check=True)
            probe_root = root.resolve(strict=True)
            source_kind = "pinned_git_archive"
        else:
            probe_root = args.vendor.resolve(strict=True)
            source_kind = "vendor_snapshot"
        sys.path.insert(0, str(probe_root))

        imports: dict[str, str] = {}
        for name in MODULES:
            module = importlib.import_module(name)
            module_path = Path(module.__file__ or "").resolve(strict=True)
            if not module_path.is_relative_to(probe_root):
                raise SystemExit(f"{name} resolved outside the probe root: {module_path}")
            imports[name] = module_path.relative_to(probe_root).as_posix()
    finally:
        if temporary:
            temporary.cleanup()

    report = {
        "probe_id": "H0-P01",
        "source_commit": source["commit"],
        "python": platform.python_version(),
        "profiles": ["development", "customer"],
        "fixture": "import-safe headless boundary; no provider, UI, network, or runtime download",
        "source_kind": source_kind,
        "imports": imports,
        "result": "passed",
        "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    }
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"H0-P01 passed for {len(imports)} imports")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
