from __future__ import annotations

from .catalog import SkillCatalog
from .ranker import LlmSkillRanker, RuleBasedSkillRanker
from .retriever import MetadataSkillRetriever
from .schema import SkillRoute


class TwoStageSkillRouter:
    def __init__(
        self,
        catalog: SkillCatalog,
        max_skills: int = 2,
        recall_k: int = 8,
        llm=None,
        model: str = "",
    ):
        self.catalog = catalog
        self.max_skills = max(0, max_skills)
        self.recall_k = max(0, recall_k)
        self.llm = llm
        self.model = model

    def route(self, task: str) -> SkillRoute:
        if self.max_skills == 0:
            return SkillRoute(intent="none", rejected=self.catalog.names())

        recalled = MetadataSkillRetriever(self.catalog, top_k=self.recall_k).retrieve(task)
        rank_result = self._rerank(task, recalled)
        selected = rank_result.selected
        selected_names = {item.skill.name for item in selected}
        recalled_names = {item.skill.name for item in recalled}
        rerank_rejected = sorted(recalled_names - selected_names)
        not_recalled = sorted(set(self.catalog.names()) - recalled_names)
        rejected = [f"rerank_rejected:{name}" for name in rerank_rejected]
        intent = rank_result.intent
        if not recalled:
            rejected = self.catalog.names()
        else:
            rejected.extend(f"not_recalled:{name}" for name in not_recalled)
        return SkillRoute(
            intent=intent,
            recalled=recalled,
            selected=selected,
            rejected=rejected,
            reranker=rank_result.reranker,
            rerank_token_usage=rank_result.token_usage,
            rerank_error=rank_result.error,
        )

    def _rerank(self, task: str, recalled):
        if self.llm is None:
            selected = RuleBasedSkillRanker(max_skills=self.max_skills).rank(task, recalled)
            intent = selected[0].skill.intents[0] if selected and selected[0].skill.intents else "general"
            from .schema import RankResult

            return RankResult(intent=intent, selected=selected, reranker="rule")
        return LlmSkillRanker(self.llm, model=self.model, max_skills=self.max_skills).rank(task, recalled)


class RuleBasedSkillRouter(TwoStageSkillRouter):
    def __init__(self, catalog: SkillCatalog, max_skills: int = 2, recall_k: int = 8):
        super().__init__(catalog=catalog, max_skills=max_skills, recall_k=recall_k)
