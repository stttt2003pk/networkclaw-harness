"""Small, repeatable probe for using Hermes' native AIAgent as the loop owner.

This module deliberately does not provide a second agent loop.  It checks that the vendored
Hermes constructor can be opened with a host-assigned workspace and exposes the callbacks that
the headless adapter must translate.  Network calls are opt-in through ``run_live_probe``.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping

from networkclaw_harness.workspace import SessionWorkspace


class HermesProbeError(RuntimeError):
    """The native Hermes loop cannot be safely probed in the current environment."""


@dataclass(slots=True)
class ProbeEvents:
    deltas: list[str] = field(default_factory=list)
    tools_started: list[str] = field(default_factory=list)
    tools_completed: list[str] = field(default_factory=list)
    statuses: list[tuple[str, str]] = field(default_factory=list)
    events: list[tuple[str, Mapping[str, Any]]] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class NativeProbeReport:
    import_ok: bool
    constructor_ok: bool
    callback_surface_ok: bool
    workspace: str
    session_id: str
    notes: tuple[str, ...] = ()

    @property
    def passed(self) -> bool:
        return self.import_ok and self.constructor_ok and self.callback_surface_ok

    def as_dict(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "import_ok": self.import_ok,
            "constructor_ok": self.constructor_ok,
            "callback_surface_ok": self.callback_surface_ok,
            "workspace": self.workspace,
            "session_id": self.session_id,
            "notes": list(self.notes),
        }


def _vendor_root() -> Path:
    return Path(__file__).resolve().parents[3] / "vendor" / "hermes"


def load_agent_class(vendor_root: Path | None = None):
    """Import the pinned AIAgent without changing the host process cwd or environment."""
    root = (vendor_root or _vendor_root()).resolve()
    if not root.is_dir():
        raise HermesProbeError("vendored Hermes runtime is unavailable")
    text = str(root)
    if text in sys.path:
        sys.path.remove(text)
    sys.path.insert(0, text)
    try:
        from run_agent import AIAgent  # type: ignore
    except Exception as error:
        raise HermesProbeError("could not import vendored Hermes AIAgent") from error
    return AIAgent


def callback_bundle(events: ProbeEvents) -> dict[str, Callable[..., None]]:
    """Return only safe callback sinks; callbacks never print or expose raw provider data."""
    def on_delta(value: Any, *args: Any, **kwargs: Any) -> None:
        if isinstance(value, str) and value:
            events.deltas.append(value)

    def on_tool_start(*args: Any, **kwargs: Any) -> None:
        name = kwargs.get("name") or (args[0] if args else "unknown")
        events.tools_started.append(str(name))

    def on_tool_complete(*args: Any, **kwargs: Any) -> None:
        name = kwargs.get("name") or (args[0] if args else "unknown")
        events.tools_completed.append(str(name))

    def on_status(kind: Any, message: Any = "", *args: Any, **kwargs: Any) -> None:
        events.statuses.append((str(kind), str(message)))

    def on_event(name: Any, payload: Any = None, *args: Any, **kwargs: Any) -> None:
        events.events.append((str(name), payload if isinstance(payload, Mapping) else {}))

    return {
        "stream_delta_callback": on_delta,
        "tool_start_callback": on_tool_start,
        "tool_complete_callback": on_tool_complete,
        "status_callback": on_status,
        "event_callback": on_event,
    }


def configure_headless_agent(agent: Any) -> Any:
    """Disable Hermes' CLI renderers so stdout remains reserved for host JSONL."""
    agent.suppress_status_output = True
    agent._print_fn = lambda *args, **kwargs: None
    return agent


def probe_constructor(workspace: SessionWorkspace, *, session_db: Any,
                      vendor_root: Path | None = None,
                      agent_factory: Callable[..., Any] | None = None) -> NativeProbeReport:
    """Construct one quiet native agent and close it; no provider request is made."""
    events = ProbeEvents()
    try:
        agent_type = agent_factory or load_agent_class(vendor_root)
    except HermesProbeError as error:
        return NativeProbeReport(False, False, False, str(workspace.root), workspace.session_id, (str(error),))
    callbacks = callback_bundle(events)
    try:
        agent = configure_headless_agent(agent_type(
            model="gpt-5", provider="openai", base_url="https://api.openai.com/v1",
            api_key="probe-key", session_id=workspace.session_id, session_db=session_db,
            cwd=str(workspace.root), max_iterations=1, quiet_mode=True,
            skip_context_files=True, skip_memory=True, skip_background_review=True,
            **callbacks,
        ))
    except Exception as error:
        return NativeProbeReport(True, False, False, str(workspace.root), workspace.session_id,
                                 (f"constructor failed: {type(error).__name__}",))
    try:
        surface_ok = all(callable(getattr(agent, name, None)) for name in ("run_conversation", "interrupt", "steer", "close"))
        callback_ok = all(getattr(agent, name, None) is callback for name, callback in callbacks.items())
    finally:
        close = getattr(agent, "close", None)
        if callable(close):
            close()
    return NativeProbeReport(True, True, surface_ok and callback_ok, str(workspace.root),
                             workspace.session_id, () if surface_ok and callback_ok else ("native callback/control surface incomplete",))


def run_live_probe(agent: Any, user_message: str) -> Mapping[str, Any]:
    """Run one explicitly supplied native agent; callers own credentials and network consent."""
    if not user_message.strip():
        raise HermesProbeError("probe user message must not be empty")
    try:
        result = agent.run_conversation(user_message)
    except Exception as error:
        raise HermesProbeError("native Hermes conversation failed") from error
    if not isinstance(result, Mapping):
        raise HermesProbeError("native Hermes conversation returned a non-mapping result")
    return {
        "completed": bool(result.get("completed")),
        "failed": bool(result.get("failed")),
        "interrupted": bool(result.get("interrupted")),
        "has_final_response": bool(result.get("final_response")),
        "api_calls": int(result.get("api_calls", 0) or 0),
    }
