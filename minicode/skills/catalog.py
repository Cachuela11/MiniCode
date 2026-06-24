from __future__ import annotations

from pathlib import Path

from .loader import load_skills
from .schema import Skill


class SkillCatalog:
    def __init__(self, skills: list[Skill]):
        self._skills = {skill.name: skill for skill in skills}

    @classmethod
    def load(cls, directory: Path) -> "SkillCatalog":
        return cls(load_skills(directory))

    @classmethod
    def empty(cls) -> "SkillCatalog":
        return cls([])

    def all(self) -> list[Skill]:
        return list(self._skills.values())

    def get(self, name: str) -> Skill | None:
        return self._skills.get(name)

    def names(self) -> list[str]:
        return sorted(self._skills)
