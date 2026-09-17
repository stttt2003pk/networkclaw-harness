#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

PYTHON=""
for candidate in "$REPO_ROOT/.venv/bin/python" "$REPO_ROOT/venv/bin/python"; do
  if [ -x "$candidate" ] && "$candidate" -c 'import pytest' 2>/dev/null; then
    PYTHON="$candidate"
    break
  fi
done

if [ -z "$PYTHON" ]; then
  echo "error: create a Python 3.12 venv and install .[dev]" >&2
  exit 1
fi

if [ "$($PYTHON -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')" != "3.12" ]; then
  echo "error: tests require CPython 3.12" >&2
  exit 1
fi

exec env -i \
  PATH="$PATH" \
  HOME="$HOME" \
  TZ=UTC \
  LANG=C.UTF-8 \
  LC_ALL=C.UTF-8 \
  PYTHONHASHSEED=0 \
  PYTHONPATH="$REPO_ROOT/src" \
  "$PYTHON" -m pytest "$@"

