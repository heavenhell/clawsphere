from __future__ import annotations

import re
import threading
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class SkillDocument:
    id: str
    title: str
    version: int
    tags: list[str]
    permission: str
    applicable_roles: list[str]
    one_liner: str
    summary: str
    detail: str


SKILLS_DIR = Path(__file__).resolve().parent


def _field(text: str, name: str, default: str = "") -> str:
    match = re.search(rf"^{re.escape(name)}:\s*(.+)$", text, re.MULTILINE)
    return match.group(1).strip() if match else default


def _list_field(text: str, name: str) -> list[str]:
    value = _field(text, name, "[]").strip("[]")
    return [item.strip() for item in value.split(",") if item.strip()]


def _section(text: str, heading: str) -> str:
    match = re.search(rf"^##\s+{re.escape(heading)}[^\n]*\n(.*?)(?=^##\s+|\Z)", text, re.MULTILINE | re.DOTALL)
    return match.group(1).strip() if match else ""


def load_skill(path: Path) -> SkillDocument:
    text = path.read_text(encoding="utf-8")
    title_match = re.search(r"^#\s+SKILL:\s*(.+)$", text, re.MULTILINE)
    if not title_match:
        raise ValueError(f"Invalid skill title: {path}")
    skill_id = title_match.group(1).strip()
    version_text = _field(text, "version", "1").split(".")[0]
    return SkillDocument(
        id=skill_id,
        title=skill_id.replace("_", " "),
        version=int(version_text),
        tags=_list_field(text, "tags"),
        permission=_field(text, "permission", "public"),
        applicable_roles=_list_field(text, "applicable_roles") or ["readonly", "ops", "admin"],
        one_liner=_section(text, "一句话"),
        summary=_section(text, "摘要"),
        detail=_section(text, "详细步骤"),
    )


def load_all_skills() -> list[SkillDocument]:
    return list(_load_index().values())


def startup_skill_summaries() -> str:
    return "\n".join(f"- {skill.id}: {skill.one_liner}" for skill in load_all_skills())


_index_lock = threading.Lock()
_SKILL_INDEX: dict[str, SkillDocument] | None = None
_CATALOG_VERSION = 0


def _build_index() -> dict[str, SkillDocument]:
    return {
        skill.id: skill
        for skill in (load_skill(path) for path in sorted(SKILLS_DIR.glob("*.md")))
    }


def _load_index() -> dict[str, SkillDocument]:
    global _SKILL_INDEX, _CATALOG_VERSION
    if _SKILL_INDEX is None:
        with _index_lock:
            if _SKILL_INDEX is None:
                _SKILL_INDEX = _build_index()
                _CATALOG_VERSION = 1
    return _SKILL_INDEX


def reload_skills() -> int:
    """Force a rebuild of the skill index and bump the catalog version.

    No hot-reload HTTP endpoint exists yet; this exists so
    authorization_epoch() has something real to react to, and so tests can
    simulate a mid-flight catalog change."""
    global _SKILL_INDEX, _CATALOG_VERSION
    with _index_lock:
        _SKILL_INDEX = _build_index()
        _CATALOG_VERSION += 1
    return _CATALOG_VERSION


def skill_catalog_version() -> int:
    _load_index()
    return _CATALOG_VERSION


def load_skill_by_id(skill_id: str) -> SkillDocument | None:
    return _load_index().get(skill_id)


def list_skills_for_roles(skills: list[SkillDocument], roles: list[str]) -> list[SkillDocument]:
    return [skill for skill in skills if any(role in skill.applicable_roles for role in roles)]


def skill_catalog_tier1(roles: list[str]) -> str:
    applicable = list_skills_for_roles(load_all_skills(), roles)
    return "\n".join(f"- {skill.id}: {skill.one_liner}" for skill in applicable)
