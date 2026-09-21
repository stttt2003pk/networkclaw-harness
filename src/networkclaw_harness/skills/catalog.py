"""Read-only, release-versioned skill discovery and session loading."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping, Sequence

from networkclaw_harness.runtime.durable import DurableSessionPort
from networkclaw_harness.runtime.models import FactKind, SemanticFact


class SkillError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class SkillRecord:
    skill_id: str
    version: str
    source: str
    release_hash: str
    content_sha256: str
    content: str
    path: Path

    def semantic_asset(self) -> Mapping[str, Any]:
        return MappingProxyType({
            "skill_id": self.skill_id, "version": self.version, "source": self.source,
            "release_hash": self.release_hash, "content_sha256": self.content_sha256,
        })


class SkillCatalog:
    """Only directories named in the release roots are scanned; mutation APIs do not exist."""

    def __init__(self, roots: Sequence[str | Path]) -> None:
        self._roots = tuple(Path(root).resolve(strict=True) for root in roots)
        self._records = self._discover()
        self._session_bindings: dict[str, tuple[str, ...]] = {}

    def list(self) -> tuple[SkillRecord, ...]:
        return tuple(self._records[name] for name in sorted(self._records))

    def load_for_session(self, session_id: str, skill_ids: Sequence[str]) -> tuple[SkillRecord, ...]:
        requested = tuple(skill_ids)
        current = self._session_bindings.get(session_id)
        if current is not None and current != requested:
            raise SkillError("skill_surface_frozen", "session skill selection is frozen until a new session")
        try:
            result = tuple(self._records[skill_id] for skill_id in requested)
        except KeyError as error:
            raise SkillError("skill_not_found", "skill is not part of the release catalog") from error
        for record in result:
            try:
                current = hashlib.sha256(record.path.read_bytes()).hexdigest()
            except OSError as error:
                raise SkillError("skill_source_changed", "release skill is no longer readable") from error
            if current != record.content_sha256:
                raise SkillError("skill_source_changed", "release skill changed after discovery")
        self._session_bindings[session_id] = requested
        return result

    def close_session(self, session_id: str) -> None:
        self._session_bindings.pop(session_id, None)

    def release_manifest(self) -> tuple[Mapping[str, Any], ...]:
        return tuple(record.semantic_asset() for record in self.list())

    def _discover(self) -> dict[str, SkillRecord]:
        records: dict[str, SkillRecord] = {}
        for root in self._roots:
            if root.is_symlink() or not root.is_dir():
                raise SkillError("invalid_skill_root", "skill root must be a real release directory")
            for metadata_path in sorted(root.glob("*/skill.json")):
                skill_dir = metadata_path.parent
                if skill_dir.is_symlink() or metadata_path.is_symlink():
                    raise SkillError("invalid_skill_path", "skill paths cannot be symbolic links")
                content_path = skill_dir / "SKILL.md"
                if not content_path.is_file() or content_path.is_symlink():
                    raise SkillError("invalid_skill", "release skill requires a real SKILL.md")
                metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
                content = content_path.read_text(encoding="utf-8")
                skill_id = metadata.get("skill_id")
                version = metadata.get("version")
                source = metadata.get("source")
                release_hash = metadata.get("release_hash")
                if not all(isinstance(item, str) and item and item.isascii()
                           for item in (skill_id, version, source, release_hash)):
                    raise SkillError("invalid_skill", "skill metadata is incomplete")
                if skill_id in records:
                    raise SkillError("skill_conflict", "skill id is duplicated across release roots")
                records[skill_id] = SkillRecord(
                    skill_id, version, source, release_hash,
                    hashlib.sha256(content.encode()).hexdigest(), content, content_path,
                )
        return records


class SkillSessionService:
    def __init__(self, catalog: SkillCatalog, durable: DurableSessionPort) -> None:
        self._catalog = catalog
        self._durable = durable

    def load(self, *, session_id: str, skill_ids: Sequence[str], event_id: str) -> tuple[SkillRecord, ...]:
        records = self._catalog.load_for_session(session_id, skill_ids)
        if records:
            self._durable.commit(
                session_id=session_id, event_id=event_id,
                facts=tuple(SemanticFact(
                    FactKind.SKILL, record.skill_id, "loaded", record.semantic_asset(),
                ) for record in records),
            )
        return records
