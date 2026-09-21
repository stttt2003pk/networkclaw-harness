"""Admission evidence for the single native Hermes runtime."""

from __future__ import annotations

import json
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, Mapping


class RuntimeSelectionError(RuntimeError):
    """The requested runtime cannot safely serve the selected profile."""


class RuntimeMode(StrEnum):
    HERMES_ADAPTER = "hermes_adapter"


CORE_RUNTIME_MODES = frozenset({RuntimeMode.HERMES_ADAPTER})


@dataclass(frozen=True, slots=True)
class RuntimeSelection:
    mode: RuntimeMode
    production: bool
    hermes_ready: bool
    reason: str


def select_runtime(*, requested: str | None = None, production: bool = False,
                   hermes_ready: bool = False) -> RuntimeSelection:
    """Select one whole runtime path; never silently downgrade a live run.

    The native adapter is the only selectable runtime and fails closed until the
    pinned Hermes runtime has passed its closure checks.
    """
    value = (requested or RuntimeMode.HERMES_ADAPTER).strip().lower()
    try:
        mode = RuntimeMode(value)
    except ValueError as error:
        raise RuntimeSelectionError(f"unknown runtime mode: {value}") from error
    if mode is not RuntimeMode.HERMES_ADAPTER:
        raise RuntimeSelectionError("only the native Hermes adapter is admitted; legacy/reference runtimes were removed")
    if mode is RuntimeMode.HERMES_ADAPTER and not hermes_ready:
        raise RuntimeSelectionError("pinned Hermes adapter is not ready; refusing runtime fallback")
    return RuntimeSelection(mode, production, hermes_ready, "explicit runtime admission")


@dataclass(frozen=True, slots=True)
class RetirementEvidence:
    live_verified: bool
    long_context_verified: bool
    timeline_verified: bool
    offline_release_verified: bool
    provenance_verified: bool
    source_commit: str = ""

    @property
    def complete(self) -> bool:
        return all((self.live_verified, self.long_context_verified,
                    self.timeline_verified, self.offline_release_verified,
                    self.provenance_verified, bool(self.source_commit)))

    @classmethod
    def from_directory(cls, root: str | Path) -> "RetirementEvidence":
        """Read only the public status fields from evidence JSON files."""
        base = Path(root)

        def load(name: str) -> Mapping[str, Any]:
            path = base / name
            if not path.is_file():
                return {}
            try:
                value = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                return {}
            return value if isinstance(value, dict) else {}

        live = load("h9-live-acceptance.json")
        timeline = load("h7-live-sse.json")
        release = load("h5-offline-release.json")
        provenance = load("h0-p01-vendor.json")
        return cls(
            live.get("status") == "live_verified",
            live.get("long_context") is True,
            timeline.get("sequence_monotonic") is True and timeline.get("secret_in_protocol") is False,
            release.get("status") == "passed",
            bool(provenance.get("source_commit")) and provenance.get("source_kind") == "vendor_snapshot",
            str(provenance.get("source_commit", "")),
        )


@dataclass(frozen=True, slots=True)
class RetirementDecision:
    can_retire: bool
    reasons: tuple[str, ...]
    rollback_admission: str

    def to_dict(self) -> dict[str, Any]:
        return {"can_retire": self.can_retire, "reasons": list(self.reasons),
                "rollback_admission": self.rollback_admission}


def evaluate_retirement(evidence: RetirementEvidence) -> RetirementDecision:
    missing = tuple(name for name, present in (
        ("live", evidence.live_verified),
        ("long_context", evidence.long_context_verified), ("timeline", evidence.timeline_verified),
        ("offline_release", evidence.offline_release_verified),
        ("provenance", evidence.provenance_verified), ("source_commit", bool(evidence.source_commit)),
    ) if not present)
    if missing:
        return RetirementDecision(False, tuple(f"missing_{item}_evidence" for item in missing),
                                  "Keep the native Hermes adapter as the only runtime; resolve missing evidence before release")
    return RetirementDecision(True, (),
                              "Rollback means rejecting the run or starting a new Hermes run; never dual-run or replay side effects")
