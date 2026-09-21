#!/usr/bin/env python3
"""Offline dependency, container definition, and advisory gate."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SEVERITY_ORDER = {"unknown": 0, "low": 1, "medium": 2, "high": 3, "critical": 4}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--advisories", type=Path, default=REPO_ROOT / "offline/security-advisories.json")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--fail-on", choices=tuple(SEVERITY_ORDER), default="high")
    args = parser.parse_args()

    advisories = json.loads(args.advisories.read_text(encoding="utf-8"))
    components = _components()
    docker = (REPO_ROOT / "Dockerfile").read_text(encoding="utf-8")
    base = re.search(r"^FROM\s+(\S+)", docker, re.MULTILINE)
    failures: list[dict[str, str]] = []
    threshold = SEVERITY_ORDER[args.fail_on]
    for finding in advisories.get("package_findings", []):
        identity = (str(finding.get("name", "")).casefold(), str(finding.get("version", "")))
        if identity in components and SEVERITY_ORDER.get(str(finding.get("severity", "unknown")).casefold(), 0) >= threshold:
            failures.append(finding)
    for finding in advisories.get("image_findings", []):
        if SEVERITY_ORDER.get(str(finding.get("severity", "unknown")).casefold(), 0) >= threshold:
            failures.append(finding)

    checks = {
        "docker_non_root_user": bool(re.search(r"^USER\s+(?!0(?:\D|$)|root(?:\D|$))", docker, re.MULTILINE)),
        "docker_offline_install": "pip install --no-index" in docker,
        "docker_no_remote_fetch": not re.search(r"\b(curl|wget)\b", docker),
        "base_image_reviewed": bool(base and base.group(1) in advisories.get("reviewed_base_images", [])),
        "sbom_components_present": bool(components),
        "advisory_snapshot_versioned": advisories.get("schema_version") == 1 and bool(advisories.get("as_of")),
    }
    report = {
        "schema_version": 1, "advisory_as_of": advisories.get("as_of"),
        "advisory_source": advisories.get("source"),
        "base_image": base.group(1) if base else None,
        "component_count": len(components), "checks": checks,
        "fail_on": args.fail_on, "blocking_findings": failures,
        "status": "passed" if all(checks.values()) and not failures else "failed",
    }
    encoded = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(encoded, encoding="utf-8")
    else:
        print(encoded, end="")
    return 0 if report["status"] == "passed" else 1


def _components() -> set[tuple[str, str]]:
    result: set[tuple[str, str]] = set()
    for name in ("offline/sbom-template.json", "offline/hermes-sbom.json"):
        path = REPO_ROOT / name
        if not path.exists():
            continue
        document = json.loads(path.read_text(encoding="utf-8"))
        for component in document.get("components", []):
            package = str(component.get("name", "")).casefold()
            version = str(component.get("version", ""))
            if package and version:
                result.add((package, version))
    return result


if __name__ == "__main__":
    raise SystemExit(main())
