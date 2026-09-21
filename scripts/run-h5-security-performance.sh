#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
PYTHON="$REPO_ROOT/.venv/bin/python"

"$REPO_ROOT/scripts/run_tests.sh" \
  tests/test_security_observability_performance.py \
  tests/test_tools_policy_runtime.py \
  tests/test_host.py \
  tests/test_lifecycle.py \
  tests/test_workspace.py \
  tests/test_interaction_control.py \
  tests/test_recovery.py
"$PYTHON" "$REPO_ROOT/scripts/security-scan.py" \
  --output "$REPO_ROOT/upstream/evidence/h5-security-scan.json"
"$PYTHON" "$REPO_ROOT/scripts/benchmark-h5.py" \
  --output "$REPO_ROOT/upstream/evidence/h5-performance-baseline.json"
