from __future__ import annotations

import re
import unicodedata

from .catalog import SkillCatalog
from .schema import SelectedSkill, Skill, SkillRoute


class RuleBasedSkillRouter:
    def __init__(self, catalog: SkillCatalog, max_skills: int = 2):
        self.catalog = catalog
        self.max_skills = max(0, max_skills)

    def route(self, task: str) -> SkillRoute:
        if self.max_skills == 0:
            return SkillRoute(intent="none", rejected=self.catalog.names())

        selected: list[SelectedSkill] = []
        rejected: list[str] = []
        for skill in self.catalog.all():
            score, reason = _score_skill(task, skill)
            if score > 0:
                selected.append(SelectedSkill(skill=skill, score=score, reason=reason))
            else:
                rejected.append(skill.name)

        selected = sorted(selected, key=lambda item: (-item.score, item.skill.name))
        selected = selected[: self.max_skills]
        intent = selected[0].skill.intents[0] if selected and selected[0].skill.intents else "general"
        return SkillRoute(intent=intent, selected=selected, rejected=rejected)


def _score_skill(task: str, skill: Skill) -> tuple[int, str]:
    task_text = _normalize(task)
    matches: list[str] = []
    score = 0

    for trigger in skill.triggers:
        if _contains(task_text, trigger):
            score += 5
            matches.append(f"trigger:{trigger}")

    for tag in skill.tags:
        if _contains(task_text, tag):
            score += 2
            matches.append(f"tag:{tag}")

    for intent in skill.intents:
        if _contains(task_text, intent.replace("_", " ")):
            score += 3
            matches.append(f"intent:{intent}")

    for word in skill.name.replace("_", " ").split():
        if _contains(task_text, word):
            score += 1
            matches.append(f"name:{word}")

    for word in _normalize(skill.description).split():
        if len(word) >= 4 and word in task_text:
            score += 1
            matches.append(f"description:{word}")
            break

    return score, ", ".join(matches) if matches else ""


def _contains(text: str, needle: str) -> bool:
    normalized = _normalize(needle)
    return bool(normalized and normalized in text)


def _normalize(value: str) -> str:
    text = unicodedata.normalize("NFKC", value).casefold()
    return re.sub(r"\s+", " ", text).strip()
