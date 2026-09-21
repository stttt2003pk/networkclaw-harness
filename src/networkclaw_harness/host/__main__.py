"""CLI entry point for the headless Harness."""

import argparse
import os
import sys

from networkclaw_harness.lifecycle.guard import install_parent_death_guard
from networkclaw_harness.observability import configure_stderr_logging
from networkclaw_harness.runtime.hermes_host_adapter import HermesHostAdapter
from .server import JsonlHost


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="networkclaw-harness")
    parser.add_argument("--parent-pid", type=int, default=os.getppid())
    parser.add_argument("--log-level", choices=("DEBUG", "INFO", "WARNING", "ERROR"), default="INFO")
    parser.add_argument("--provider-mode", choices=("live",), default=None,
                        help="declare that the host configured a live provider route")
    parser.add_argument("--runtime-mode", choices=("hermes_adapter",), default="hermes_adapter",
                        help="native Hermes runtime (the only supported execution mode)")
    args = parser.parse_args(argv)
    import logging
    configure_stderr_logging(level=getattr(logging, args.log_level))
    if args.provider_mode is not None:
        os.environ["NETWORKCLAW_HARNESS_PROVIDER_MODE"] = args.provider_mode
    install_parent_death_guard(args.parent_pid)
    runtime = HermesHostAdapter()
    return JsonlHost(sys.stdin, sys.stdout, runtime=runtime).serve()


if __name__ == "__main__":
    raise SystemExit(main())
