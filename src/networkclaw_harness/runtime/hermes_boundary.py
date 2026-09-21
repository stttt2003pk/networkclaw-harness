"""Reviewable classification of every Hermes integration point."""

from __future__ import annotations

from enum import StrEnum


class HermesBoundary(StrEnum):
    DIRECT = "direct"
    ADAPTER = "adapter"
    FORK = "fork"
    DISABLED = "disabled"


BOUNDARY_REASONS = {
    HermesBoundary.DIRECT: "Pinned pure/runtime capability is used without changing its semantics.",
    HermesBoundary.ADAPTER: "NetworkClaw owns identity, workspace, policy, and lifecycle; Hermes is behind a narrow port.",
    HermesBoundary.FORK: "A small compatibility fork is required where the upstream global contract conflicts with headless hosting.",
    HermesBoundary.DISABLED: "The capability is intentionally unavailable under the production security contract.",
}

