"""Release-manifest entries for source-developed tools and skills."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from networkclaw_harness.skills import SkillRecord

from .registry import ToolManifest


@dataclass(frozen=True, slots=True)
class CapabilityReleaseEntry:
    capability_id: str
    kind: str
    version: str
    source: str
    content_hash: str
    policy_id: str | None = None


def capability_release_manifest(tools: Sequence[ToolManifest], skills: Sequence[SkillRecord]) -> tuple[CapabilityReleaseEntry, ...]:
    entries = []
    for tool in tools:
        payload = json.dumps({
            "name": tool.name, "version": tool.version,
            "input_schema": dict(tool.input_schema), "output_schema": dict(tool.output_schema),
            "policy_id": tool.policy_id, "capability": tool.capability, "source": tool.source,
        }, sort_keys=True, separators=(",", ":")).encode()
        entries.append(CapabilityReleaseEntry(
            tool.name, "tool", tool.version, tool.source, hashlib.sha256(payload).hexdigest(), tool.policy_id,
        ))
    entries.extend(CapabilityReleaseEntry(
        skill.skill_id, "skill", skill.version, skill.source, skill.content_sha256,
    ) for skill in skills)
    return tuple(sorted(entries, key=lambda item: (item.kind, item.capability_id)))
