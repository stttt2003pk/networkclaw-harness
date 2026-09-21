#!/usr/bin/env python3
"""Start a live Harness using a narrowly allowlisted development .env.

This helper never forwards the source file as a protocol payload and never prints
its values. Production deployments must inject secrets through their secret manager.
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import shlex
import subprocess
import sys
import stat

ALLOWED = {
    "OPENAI_API_KEY", "OPENAI_BASE_URL", "OPENAI_MODEL", "OPENAI_MODELS",
    "NETWORKCLAW_HARNESS_ALLOWED_MODELS", "NETWORKCLAW_HARNESS_PROVIDER",
    "NETWORKCLAW_HARNESS_PROVIDER_TIMEOUT",
    "NETWORKCLAW_HARNESS_PROVIDER_RETRIES", "NETWORKCLAW_HARNESS_REASONING_EFFORT",
    "NETWORKCLAW_HARNESS_PROVIDER_STREAM", "NETWORKCLAW_HARNESS_ALLOWED_EGRESS_HOSTS",
    "NETWORKCLAW_HARNESS_PROVIDER_CONFIG_VERSION", "NETWORKCLAW_HARNESS_PROFILE",
    "NETWORKCLAW_HARNESS_RUN_TIMEOUT_SECONDS",
    "NETWORKCLAW_HARNESS_HOST_BASH_READ_ROOTS", "NETWORKCLAW_HARNESS_HOST_BASH_EGRESS_HOSTS",
}


def read_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if key not in ALLOWED:
            continue
        value = value.strip()
        try:
            parsed = shlex.split(value, comments=True)
        except ValueError as error:
            raise SystemExit(f"invalid value for {key}") from error
        values[key] = parsed[0] if parsed else ""
    return values


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--env-file", type=Path, required=True)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    if not args.command:
        raise SystemExit("a Harness command is required")
    if not args.env_file.is_file():
        raise SystemExit("env file does not exist")
    if args.env_file.stat().st_mode & (stat.S_IWGRP | stat.S_IWOTH):
        raise SystemExit("env file must not be writable by group/other")
    mode = read_env(args.env_file)
    if not mode.get("OPENAI_API_KEY") or not mode.get("OPENAI_MODEL"):
        raise SystemExit("OPENAI_API_KEY and OPENAI_MODEL are required")
    allowed = mode.get("NETWORKCLAW_HARNESS_ALLOWED_MODELS", "")
    if not allowed:
        allowed = mode.get("OPENAI_MODELS", mode["OPENAI_MODEL"])
        mode["NETWORKCLAW_HARNESS_ALLOWED_MODELS"] = allowed
    if mode["OPENAI_MODEL"] not in {item.strip() for item in allowed.split(",") if item.strip()}:
        raise SystemExit("OPENAI_MODEL is not in the configured model allowlist")
    environment = os.environ.copy()
    environment.update(mode)
    environment["NETWORKCLAW_HARNESS_PROVIDER_MODE"] = "live"
    return subprocess.call(args.command, env=environment)


if __name__ == "__main__":
    raise SystemExit(main())
