from .catalog import SkillCatalog
from .prompt import render_skill_prompt
from .schema import RecalledSkill, SelectedSkill, Skill, SkillRoute

__all__ = [
    "RecalledSkill",
    "SelectedSkill",
    "Skill",
    "SkillCatalog",
    "SkillRoute",
    "MetadataSkillRetriever",
    "LlmSkillRanker",
    "RuleBasedSkillRanker",
    "RuleBasedSkillRouter",
    "TwoStageSkillRouter",
    "render_skill_prompt",
]


def __getattr__(name: str):
    if name == "MetadataSkillRetriever":
        from .retriever import MetadataSkillRetriever

        return MetadataSkillRetriever
    if name == "LlmSkillRanker":
        from .ranker import LlmSkillRanker

        return LlmSkillRanker
    if name == "RuleBasedSkillRanker":
        from .ranker import RuleBasedSkillRanker

        return RuleBasedSkillRanker
    if name == "RuleBasedSkillRouter":
        from .router import RuleBasedSkillRouter

        return RuleBasedSkillRouter
    if name == "TwoStageSkillRouter":
        from .router import TwoStageSkillRouter

        return TwoStageSkillRouter
    raise AttributeError(name)
