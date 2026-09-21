#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

"$REPO_ROOT/scripts/run_tests.sh" \
  tests/test_recovery.py \
  tests/test_session_runtime.py \
  tests/test_interaction_control.py \
  tests/test_workspace.py \
  tests/test_host.py \
  tests/test_lifecycle.py \
  tests/test_projection.py
