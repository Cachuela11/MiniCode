from __future__ import annotations

from dataclasses import asdict, dataclass, field


@dataclass(frozen=True)
class Skill:
    name: str
    description: str
    tags: list[str] = field(default_factory=list)
    intents: list[str] = field(default_factory=list)
    tools: list[str] = field(default_factory=list)
    triggers: list[str] = field(default_factory=list)
    body: str = ""
    source_path: str = ""


@dataclass(frozen=True)
class SelectedSkill:
    skill: Skill
    score: int
    reason: str

    def to_log_dict(self) -> dict:
        data = asdict(self)
        data["skill"] = {
            "name": self.skill.name,
            "description": self.skill.description,
            "tags": self.skill.tags,
            "intents": self.skill.intents,
            "tools": self.skill.tools,
            "source_path": self.skill.source_path,
        }
        return data


@dataclass(frozen=True)
class SkillRoute:
    intent: str
    selected: list[SelectedSkill] = field(default_factory=list)
    rejected: list[str] = field(default_factory=list)

    def to_log_dict(self) -> dict:
        return {
            "intent": self.intent,
            "selected": [item.to_log_dict() for item in self.selected],
            "rejected": self.rejected,
        }
