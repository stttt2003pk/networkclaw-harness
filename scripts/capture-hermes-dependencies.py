#!/usr/bin/env python3
"""Capture the resolved H0 Hermes dependency inventory and source-lock identities."""

from __future__ import annotations

import argparse
import json
import platform
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
EXCLUDED_PACKAGES = {"pip", "setuptools", "wheel", "hermes-agent"}


def git_blob_sha(commit: str, path: str) -> str:
    return subprocess.check_output(
        ["git", "-C", str(REPO_ROOT), "rev-parse", f"{commit}:{path}"], text=True
    ).strip()


def git_file(commit: str, path: str) -> bytes:
    return subprocess.check_output(["git", "-C", str(REPO_ROOT), "show", f"{commit}:{path}"])


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--output", type=Path, default=REPO_ROOT / "upstream/hermes-dependencies.json"
    )
    parser.add_argument(
        "--sbom-output", type=Path, default=REPO_ROOT / "offline/hermes-sbom.json"
    )
    parser.add_argument(
        "--source-pyproject", type=Path, default=REPO_ROOT / "upstream/hermes-pyproject.toml"
    )
    parser.add_argument(
        "--source-uv-lock", type=Path, default=REPO_ROOT / "upstream/hermes-uv.lock"
    )
    args = parser.parse_args()

    source = json.loads((REPO_ROOT / "upstream/hermes-source.json").read_text(encoding="utf-8"))
    frozen = subprocess.check_output([sys.executable, "-m", "pip", "freeze", "--all"], text=True)
    components = []
    for line in frozen.splitlines():
        if "==" not in line or line.startswith("-e "):
            continue
        name, version = line.split("==", 1)
        if name.lower() not in EXCLUDED_PACKAGES:
            components.append({"name": name, "version": version})
    components.sort(key=lambda component: component["name"].lower())

    inventory = {
        "schema_version": 1,
        "source_commit": source["commit"],
        "source_pyproject_blob": git_blob_sha(source["commit"], "pyproject.toml"),
        "source_uv_lock_blob": git_blob_sha(source["commit"], "uv.lock"),
        "python": platform.python_version(),
        "platform": platform.platform(),
        "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "components": components,
    }
    args.source_pyproject.write_bytes(git_file(source["commit"], "pyproject.toml"))
    args.source_uv_lock.write_bytes(git_file(source["commit"], "uv.lock"))
    args.output.write_text(json.dumps(inventory, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    sbom = {
        "bomFormat": "CycloneDX",
        "specVersion": "1.6",
        "version": 1,
        "metadata": {
            "component": {
                "type": "library",
                "name": "hermes-agent-runtime-snapshot",
                "version": source["commit"],
            }
        },
        "components": [
            {"type": "library", "name": component["name"], "version": component["version"]}
            for component in components
        ],
        "properties": [
            {"name": "networkclaw:source-commit", "value": source["commit"]},
            {"name": "networkclaw:source-uv-lock-blob", "value": inventory["source_uv_lock_blob"]},
        ],
    }
    args.sbom_output.write_text(json.dumps(sbom, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"captured {len(components)} Hermes dependencies")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
