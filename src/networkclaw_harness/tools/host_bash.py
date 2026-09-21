"""The profile-scoped host_bash capability."""

from __future__ import annotations

from typing import Any, Mapping

from .builtins import WorkspaceShellAdapter
from .registry import ToolLayer, ToolManifest

HOST_BASH_INPUT_SCHEMA = {
    "type": "object",
    "properties": {"argv": {"type": "array"}},
    "required": ["argv"],
    "additionalProperties": False,
}
HOST_BASH_OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "exit_code": {"type": "integer"},
        "stdout": {"type": "string"},
        "stderr": {"type": "string"},
    },
    "required": ["exit_code", "stdout", "stderr"],
    "additionalProperties": False,
}


def host_bash_manifest(adapter: WorkspaceShellAdapter) -> ToolManifest:
    return ToolManifest(
        name="host_bash",
        version="1.0.0",
        description=(
            "Run one allowlisted argv command in the assigned session workspace. "
            "Use direct argv without pipes or shell syntax. Development commands include pwd, ls, du, find, "
            "cat, head, tail, wc, sed, and curl. Host reads and network hosts must be pre-authorized; curl "
            "must use -o/--output with a workspace-relative file, which later calls can inspect."
        ),
        input_schema=HOST_BASH_INPUT_SCHEMA,
        output_schema=HOST_BASH_OUTPUT_SCHEMA,
        policy_id="host_bash.read_only",
        capability="shell",
        layer=ToolLayer.PROFILE,
        source="networkclaw-harness",
        adapter=adapter,
    )
