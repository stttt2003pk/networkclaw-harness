#!/usr/bin/env python3
"""Trace imports from selected Hermes runtime entry points into an allowlist."""

from __future__ import annotations

import argparse
import importlib
import json
import platform
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
ENTRY_POINTS = (
    "agent.conversation_loop",
    "agent.context_engine",
    "agent.context_compressor",
    "agent.memory_manager",
    "agent.provider_base",
    "tools.registry",
    "agent.skill_commands",
    "agent.browser_registry",
    "agent.subagent_lifecycle",
    "mcp_serve",
    "run_agent",
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path, help="clean pinned Hermes source tree")
    parser.add_argument(
        "--allowlist", type=Path, default=REPO_ROOT / "upstream/hermes-runtime-files.txt"
    )
    parser.add_argument(
        "--evidence", type=Path, default=REPO_ROOT / "upstream/evidence/h0-import-trace.json"
    )
    args = parser.parse_args()
    source = args.source.resolve(strict=True)
    sys.dont_write_bytecode = True
    sys.path.insert(0, str(source))

    accessed: set[Path] = set()

    def record_open(event: str, audit_args: tuple[object, ...]) -> None:
        if event != "open" or not audit_args or not isinstance(audit_args[0], str):
            return
        candidate = Path(audit_args[0])
        if not candidate.exists():
            return
        resolved = candidate.resolve(strict=True)
        if not resolved.is_relative_to(source):
            return
        relative = resolved.relative_to(source)
        if (
            relative.parts[0] != "venv"
            and "__pycache__" not in relative.parts
            and not relative.parts[0].endswith(".egg-info")
            and resolved.suffix != ".pyc"
            and resolved.is_file()
        ):
            accessed.add(relative)

    sys.addaudithook(record_open)

    for entry_point in ENTRY_POINTS:
        importlib.import_module(entry_point)
    from agent.skill_commands import get_skill_commands
    from hermes_cli.plugins_discovery import collect_directory_manifests

    get_skill_commands()
    collect_directory_manifests()

    paths = {Path("LICENSE"), *accessed}
    for module in sys.modules.values():
        module_file = getattr(module, "__file__", None)
        if not module_file:
            continue
        candidate = Path(module_file)
        if not candidate.exists():
            continue
        resolved = candidate.resolve(strict=True)
        if resolved.is_relative_to(source) and resolved.suffix == ".py":
            relative = resolved.relative_to(source)
            if relative.parts[0] not in {"venv", "__pycache__"}:
                paths.add(relative)

    allowlist = "\n".join(path.as_posix() for path in sorted(paths)) + "\n"
    args.allowlist.write_text(allowlist, encoding="utf-8")
    source_metadata = json.loads((REPO_ROOT / "upstream/hermes-source.json").read_text(encoding="utf-8"))
    evidence = {
        "probe_id": "H0-import-trace",
        "source_commit": source_metadata["commit"],
        "python": platform.python_version(),
        "entry_points": list(ENTRY_POINTS),
        "files": [path.as_posix() for path in sorted(paths)],
        "resource_files": [path.as_posix() for path in sorted(accessed) if path.suffix != ".py"],
        "source_files": len(paths),
        "result": "passed",
        "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    }
    args.evidence.parent.mkdir(parents=True, exist_ok=True)
    args.evidence.write_text(json.dumps(evidence, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"traced {len(paths)} Hermes source files")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
