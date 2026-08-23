"""Skill library: knowledge documents an agent can load on demand.

A skill is a folder under ``skills/`` containing a ``SKILL.md`` whose first
non-empty line is a one-line description; the rest is the body. Skills are
*knowledge, not configuration* — they teach the agent how to do work it is
already allowed to do (a provider's portal flow, what to ask for a record
type, a firm's org chart). The tool allow-list stays the capability boundary.

Three flavors, by location:
  - ``skills/<name>/``            shared work-kind / system-operation skills
  - ``skills/firms/<firm>/``      firm-context skills (auto-visible to that
                                  firm's runs, private to the firm)

Progressive disclosure is a hard requirement: the agent's context message
lists only name + one-line description; the body loads explicitly through the
platform-level ``load_skill`` tool, and every load is audited.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from app.config import settings

log = logging.getLogger("skills")


@dataclass
class SkillMeta:
    key: str            # lookup key: "name" or "firms/<firm>/<name>"
    name: str
    description: str    # first non-empty line of SKILL.md
    body: str           # full SKILL.md content
    path: str
    firm_key: str | None = None   # set for firm-scoped skills


_CACHE: dict[str, SkillMeta] | None = None


def _load_tree(root: Path, prefix: str = "", firm_key: str | None = None) -> dict[str, SkillMeta]:
    found: dict[str, SkillMeta] = {}
    if not root.is_dir():
        return found
    for child in sorted(root.iterdir()):
        skill_md = child / "SKILL.md" if child.is_dir() else None
        if skill_md is None or not skill_md.is_file():
            continue
        try:
            text = skill_md.read_text(encoding="utf-8")
        except OSError:
            continue
        lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
        description = lines[0].lstrip("# ").strip() if lines else child.name
        key = f"{prefix}{child.name}"
        found[key] = SkillMeta(
            key=key, name=child.name, description=description,
            body=text, path=str(skill_md), firm_key=firm_key,
        )
    return found


def load_all() -> dict[str, SkillMeta]:
    """Scan the skill root (cached). Key = 'name' for shared skills,
    'firms/<firm_key>/<name>' for firm skills."""
    global _CACHE
    if _CACHE is not None:
        return _CACHE
    root = Path(settings.skills_root)
    skills = _load_tree(root)
    firms_dir = root / "firms"
    if firms_dir.is_dir():
        for firm_dir in sorted(firms_dir.iterdir()):
            if firm_dir.is_dir():
                skills.update(_load_tree(firm_dir, prefix=f"firms/{firm_dir.name}/",
                                         firm_key=firm_dir.name))
    _CACHE = skills
    log.info("loaded %d skill(s) from %s", len(skills), root)
    return skills


def reset_cache() -> None:
    global _CACHE
    _CACHE = None


def describe(keys: list[str]) -> list[SkillMeta]:
    """The skill *index* for an agent: metadata only, no bodies."""
    all_skills = load_all()
    return [all_skills[k] for k in keys if k in all_skills]


def get(key: str) -> SkillMeta | None:
    return load_all().get(key)
