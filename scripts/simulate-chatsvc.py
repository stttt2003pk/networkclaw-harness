#!/usr/bin/env python3
"""Drive a Harness launcher with a JSONL fixture and validate its response stream."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("fixture", type=Path)
    parser.add_argument("--python", default=sys.executable)
    args = parser.parse_args()
    fixture = args.fixture.read_bytes()
    process = subprocess.run(
        [args.python, "-m", "networkclaw_harness.host"], input=fixture,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
    )
    sequences: dict[str | None, int] = {}
    terminal: set[str | None] = set()
    for line_number, line in enumerate(process.stdout.splitlines(), 1):
        try:
            frame = json.loads(line)
        except json.JSONDecodeError as error:
            raise SystemExit(f"stdout line {line_number} is not JSON: {error}") from error
        request_id = frame.get("request_id")
        expected = sequences.get(request_id, 0) + 1
        if frame.get("sequence") != expected:
            raise SystemExit(f"request {request_id!r} expected sequence {expected}")
        sequences[request_id] = expected
        if frame.get("type") in {"end", "error"} and frame.get("end") is True:
            terminal.add(request_id)
    missing = set(sequences) - terminal
    if missing:
        raise SystemExit(f"requests without terminal frames: {sorted(missing)}")
    sys.stdout.buffer.write(process.stdout)
    return process.returncode


if __name__ == "__main__":
    raise SystemExit(main())
