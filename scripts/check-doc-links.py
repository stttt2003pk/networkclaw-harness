#!/usr/bin/env python3
"""Fail when published Harness Markdown links do not resolve locally."""

from __future__ import annotations

import re
import sys
from pathlib import Path
from urllib.parse import unquote


LINK_PATTERN = re.compile(r"(?<!!)\[[^]]*\]\(([^)\s]+)(?:\s+[^)]*)?\)")
HEADING_PATTERN = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$", re.MULTILINE)
EXTERNAL_SCHEMES = ("http:", "https:", "mailto:", "tel:", "data:")


def slugify(heading: str) -> str:
    cleaned = re.sub(r"[`*_]", "", heading.strip().lower())
    return re.sub(r"[^\w\-\u4e00-\u9fff]+", "-", cleaned).strip("-")


def anchors(path: Path) -> set[str]:
    return {slugify(match.group(2)) for match in HEADING_PATTERN.finditer(path.read_text())}


def check_document(path: Path, docs_root: Path) -> list[str]:
    errors: list[str] = []
    for match in LINK_PATTERN.finditer(path.read_text()):
        target = unquote(match.group(1).strip("<>"))
        if target.startswith(EXTERNAL_SCHEMES):
            continue

        file_target, separator, anchor = target.partition("#")
        destination = path if not file_target else (path.parent / file_target).resolve()
        if not destination.is_relative_to(docs_root.resolve()):
            errors.append(f"{path}: link escapes docs tree: {target}")
            continue
        if not destination.exists():
            errors.append(f"{path}: missing link target: {target}")
            continue
        if separator and destination.suffix == ".md" and anchor not in anchors(destination):
            errors.append(f"{path}: missing anchor in {target}")
    return errors


def main() -> int:
    repository_root = Path(__file__).resolve().parents[1]
    docs_root = repository_root / "src" / "networkclaw_harness" / "docs"
    errors = [error for path in docs_root.rglob("*.md") for error in check_document(path, docs_root)]
    if errors:
        print("\n".join(errors), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
