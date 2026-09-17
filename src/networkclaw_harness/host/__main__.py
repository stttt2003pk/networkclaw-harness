"""CLI entry point for the headless Harness."""

import logging
import sys

from .server import JsonlHost


def main() -> int:
    logging.basicConfig(
        stream=sys.stderr,
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    return JsonlHost(sys.stdin, sys.stdout).serve()


if __name__ == "__main__":
    raise SystemExit(main())

