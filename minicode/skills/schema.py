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
    recall_score: int = 0

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
class RecalledSkill:
    skill: Skill
    score: int
    reason: str

    def to_log_dict(self) -> dict:
        return {
            "skill": {
                "name": self.skill.name,
                "description": self.skill.description,
                "tags": self.skill.tags,
                "intents": self.skill.intents,
                "tools": self.skill.tools,
                "source_path": self.skill.source_path,
            },
            "score": self.score,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class SkillRoute:
    intent: str
    recalled: list[RecalledSkill] = field(default_factory=list)
    selected: list[SelectedSkill] = field(default_factory=list)
    rejected: list[str] = field(default_factory=list)
    reranker: str = "none"
    rerank_token_usage: dict[str, int] = field(default_factory=dict)
    rerank_error: str = ""
    retrieval_trace: dict | None = None

    def to_log_dict(self) -> dict:
        return {
            "intent": self.intent,
            "recalled": [item.to_log_dict() for item in self.recalled],
            "selected": [item.to_log_dict() for item in self.selected],
            "rejected": self.rejected,
            "reranker": self.reranker,
            "rerank_token_usage": self.rerank_token_usage,
            "rerank_error": self.rerank_error,
            "retrieval_trace": self.retrieval_trace,
        }


@dataclass(frozen=True)
class RankResult:
    intent: str
    selected: list[SelectedSkill] = field(default_factory=list)
    reranker: str = "none"
    token_usage: dict[str, int] = field(default_factory=dict)
    error: str = ""
