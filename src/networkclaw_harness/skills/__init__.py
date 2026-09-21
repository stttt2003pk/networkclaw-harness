"""NetworkClaw-maintained, release-versioned, read-only skills."""

from pathlib import Path

from .catalog import SkillCatalog, SkillError, SkillRecord, SkillSessionService

BUILTIN_SKILLS_ROOT = Path(__file__).with_name("builtin")

__all__ = ["BUILTIN_SKILLS_ROOT", "SkillCatalog", "SkillError", "SkillRecord", "SkillSessionService"]
