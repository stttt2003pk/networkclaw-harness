"""Explicit construction boundary for the vendored Hermes runtime.

The upstream runtime has a process-wide home and several convenience defaults.  Those
defaults are useful for the interactive Hermes application, but are unsafe in a
multi-session headless host.  This module is the only place where NetworkClaw creates
vendored Hermes objects: every durable path is derived from the host-assigned
``SessionWorkspace`` and no cwd/environment fallback is permitted.
"""

from __future__ import annotations

import importlib
import sys
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Any, Callable

from networkclaw_harness.workspace import SessionWorkspace


class HermesBootstrapError(RuntimeError):
    """The pinned runtime could not be constructed inside the assigned workspace."""


@dataclass(frozen=True, slots=True)
class HermesRunState:
    """Per-run objects whose lifetime must not leak into another session."""

    run_id: str
    workspace: SessionWorkspace
    iteration_budget: Any
    provider_transport: Any
    compressor: Any


@dataclass(frozen=True, slots=True)
class HermesRuntimeBootstrap:
    """Host-owned Hermes resources for one session/run lifecycle."""

    workspace: SessionWorkspace
    session_db: Any
    session_db_path: Path
    vendor_root: Path

    @classmethod
    def open(cls, workspace: SessionWorkspace, *, read_only: bool = False) -> "HermesRuntimeBootstrap":
        if not isinstance(workspace, SessionWorkspace):
            raise HermesBootstrapError("a host-assigned SessionWorkspace is required")
        db_path = workspace.path_for("session-state/hermes-session.db")
        vendor = Path(__file__).resolve().parents[3] / "vendor" / "hermes"
        if not vendor.is_dir():
            raise HermesBootstrapError("vendored Hermes runtime is unavailable")
        _activate_vendor(vendor)
        try:
            session_db_type = importlib.import_module("hermes_state").SessionDB
            database = session_db_type(db_path=db_path, read_only=read_only)
        except Exception as error:  # normalize upstream implementation details at the boundary
            raise HermesBootstrapError("could not open Hermes SessionDB in assigned workspace") from error
        return cls(workspace, database, db_path, vendor)

    def close(self) -> None:
        close = getattr(self.session_db, "close", None)
        if close is not None:
            close()

    def module(self, name: str) -> ModuleType:
        """Import an allowlisted vendor module without exposing arbitrary imports."""
        if not name or name.startswith(".") or ".." in name:
            raise HermesBootstrapError("invalid Hermes module name")
        return importlib.import_module(name)

    def create_run(
        self, *, run_id: str, max_iterations: int,
        provider_factory: Callable[[SessionWorkspace], Any],
        compressor_factory: Callable[[SessionWorkspace], Any],
    ) -> HermesRunState:
        """Create isolated per-run dependencies from explicit host factories."""
        if not run_id or max_iterations <= 0:
            raise HermesBootstrapError("run id and positive iteration budget are required")
        iteration_type = self.module("agent.iteration_budget").IterationBudget
        try:
            provider = provider_factory(self.workspace)
            compressor = compressor_factory(self.workspace)
        except Exception as error:
            raise HermesBootstrapError("could not create workspace-bound run dependencies") from error
        return HermesRunState(
            run_id, self.workspace, iteration_type(max_iterations), provider, compressor,
        )


def _activate_vendor(vendor: Path) -> None:
    vendor_text = str(vendor.resolve())
    if vendor_text in sys.path:
        sys.path.remove(vendor_text)
    sys.path.insert(0, vendor_text)


def open_hermes_runtime(workspace: SessionWorkspace, *, read_only: bool = False) -> HermesRuntimeBootstrap:
    """Open the pinned runtime using only the workspace supplied by the host."""
    return HermesRuntimeBootstrap.open(workspace, read_only=read_only)
