"""Workspace-bound shell and file adapters used by registered manifests."""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path
from typing import Any, Mapping, Sequence
from urllib.parse import urlparse

from networkclaw_harness.workspace import SessionWorkspace

from .executor import CancellationToken, ToolExecutionError


class WorkspaceFileAdapter:
    def __init__(self, workspace: SessionWorkspace, *, write: bool = False) -> None:
        self._workspace = workspace
        self._write = write

    def __call__(self, arguments: Mapping[str, Any], cancellation: CancellationToken) -> Mapping[str, Any]:
        cancellation.raise_if_cancelled()
        path = self._workspace.path_for(str(arguments["path"]))
        if self._write:
            content = str(arguments["content"])
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
            return {"status": "written", "bytes": len(content.encode())}
        return {"status": "read", "content": path.read_text(encoding="utf-8")}


class WorkspaceShellAdapter:
    """No shell interpolation: argv executes directly with cwd fixed to the session root."""

    def __init__(self, workspace: SessionWorkspace, *, environment: Mapping[str, str] | None = None,
                 exact_shell_commands: Sequence[Sequence[str]] = (),
                 read_roots: Sequence[str | Path] = (),
                 allowed_network_hosts: Sequence[str] = ()) -> None:
        self._workspace = workspace
        base = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8"}
        self._environment = base | dict(environment or {})
        self._exact_shell_commands = {tuple(command) for command in exact_shell_commands}
        self._read_roots = tuple(Path(root).resolve(strict=True) for root in read_roots)
        self._allowed_network_hosts = {host.casefold() for host in allowed_network_hosts if host}

    def __call__(self, arguments: Mapping[str, Any], cancellation: CancellationToken) -> Mapping[str, Any]:
        argv = arguments.get("argv")
        if not isinstance(argv, (list, tuple)) or not argv or not all(isinstance(item, str) and item for item in argv):
            raise ToolExecutionError("invalid_command", "shell tool requires a non-empty argv array")
        if argv[0] in {"sh", "/bin/sh"} and tuple(argv) not in self._exact_shell_commands:
            raise ToolExecutionError("invalid_command", "shell scripts must match a host-defined diagnostic")
        self._validate_command(tuple(argv))
        process = subprocess.Popen(
            list(argv), cwd=self._workspace.root, env=self._environment,
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        try:
            while process.poll() is None:
                if cancellation.cancelled:
                    process.terminate()
                    try:
                        process.wait(timeout=1)
                    except subprocess.TimeoutExpired:
                        process.kill()
                    raise ToolExecutionError("tool_cancelled", "shell command was cancelled")
                time.sleep(0.01)
            stdout, stderr = process.communicate()
            return {
                "exit_code": process.returncode,
                "stdout": stdout.decode("utf-8", errors="replace"),
                "stderr": stderr.decode("utf-8", errors="replace"),
            }
        finally:
            if process.poll() is None:
                process.kill()

    def _validate_command(self, argv: tuple[str, ...]) -> None:
        command = Path(argv[0]).name
        if any(".." in Path(value).parts for value in argv[1:] if not value.startswith("-")):
            raise ToolExecutionError("path_not_allowed", "parent path traversal is not allowed")
        if command == "curl":
            self._validate_curl(argv[1:])
            return
        dangerous = {"-delete", "-exec", "-execdir", "-ok", "-okdir", "-fprint", "-fprintf"}
        if command == "find" and dangerous.intersection(argv[1:]):
            raise ToolExecutionError("invalid_command", "find mutation and command execution are not allowed")
        if command == "sed" and any(value == "-i" or value.startswith("-i") for value in argv[1:]):
            raise ToolExecutionError("invalid_command", "in-place sed writes are not allowed")
        for value in argv[1:]:
            if value.startswith("/"):
                self._validate_read_path(Path(value))

    def _validate_read_path(self, value: Path) -> None:
        resolved = value.resolve(strict=False)
        allowed = (self._workspace.root, *self._read_roots)
        if not any(resolved == root or resolved.is_relative_to(root) for root in allowed):
            raise ToolExecutionError("path_not_allowed", "command path is outside configured read roots")

    def _validate_curl(self, arguments: tuple[str, ...]) -> None:
        flags_without_value = {"-L", "--location", "--fail", "--silent", "--show-error"}
        flags_with_value = {"-o", "--output", "--max-time"}
        urls = [value for value in arguments if value.startswith(("https://", "http://"))]
        if len(urls) != 1:
            raise ToolExecutionError("invalid_command", "curl requires exactly one HTTP(S) URL")
        host = (urlparse(urls[0]).hostname or "").casefold()
        if host not in self._allowed_network_hosts:
            raise ToolExecutionError("network_not_allowed", "curl destination is not allowlisted")
        output: str | None = None
        index = 0
        while index < len(arguments):
            value = arguments[index]
            if value in flags_without_value or value in urls:
                index += 1
                continue
            if value in flags_with_value and index + 1 < len(arguments):
                if value in {"-o", "--output"}:
                    output = arguments[index + 1]
                index += 2
                continue
            if value.startswith("--output="):
                output = value.split("=", 1)[1]
                index += 1
                continue
            raise ToolExecutionError("invalid_command", "curl option is outside the development allowlist")
        if not output:
            raise ToolExecutionError("invalid_command", "curl must write to a workspace-relative output file")
        if output.startswith("-"):
            raise ToolExecutionError("invalid_command", "curl output path is invalid")
        self._workspace.path_for(output)
