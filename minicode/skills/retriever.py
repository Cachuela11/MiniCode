from __future__ import annotations

from .catalog import SkillCatalog
from .matching import contains, normalize
from .schema import RecalledSkill, Skill


class MetadataSkillRetriever:
    def __init__(self, catalog: SkillCatalog, top_k: int = 8):
        self.catalog = catalog
        self.top_k = max(0, top_k)

    def retrieve(self, task: str) -> list[RecalledSkill]:
        if self.top_k == 0:
            return []

        recalled: list[RecalledSkill] = []
        for skill in self.catalog.all():
            score, reason = _recall_score(task, skill)
            if score > 0:
                recalled.append(RecalledSkill(skill=skill, score=score, reason=reason))

        recalled = sorted(recalled, key=lambda item: (-item.score, item.skill.name))
        return recalled[: self.top_k]


def _recall_score(task: str, skill: Skill) -> tuple[int, str]:
    task_text = normalize(task)
    matches: list[str] = []
    score = 0

    for trigger in skill.triggers:
        if contains(task_text, trigger):
            score += 5
            matches.append(f"trigger:{trigger}")

    for intent in skill.intents:
        if contains(task_text, intent.replace("_", " ")):
            score += 3
            matches.append(f"intent:{intent}")

    for tag in skill.tags:
        if contains(task_text, tag):
            score += 2
            matches.append(f"tag:{tag}")

    for word in skill.name.replace("_", " ").split():
        if contains(task_text, word):
            score += 1
            matches.append(f"name:{word}")

    for word in normalize(skill.description).split():
        if len(word) >= 4 and word in task_text:
            score += 1
            matches.append(f"description:{word}")
            break

    return score, ", ".join(matches)
