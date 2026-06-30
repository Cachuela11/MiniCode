from __future__ import annotations

from .catalog import SkillCatalog
from .schema import SkillRoute
from ..retrieval.skill import SkillToolRetriever


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

        retrieval = SkillToolRetriever(
            self.catalog,
            llm=self.llm,
            model=self.model,
            recall_k=self.recall_k,
        ).retrieve(task, limit=self.max_skills)
        return SkillRoute(
            intent=retrieval.intent,
            recalled=retrieval.recalled,
            selected=retrieval.selected,
            rejected=retrieval.rejected,
            reranker=retrieval.reranker,
            rerank_token_usage=retrieval.rerank_token_usage,
            rerank_error=retrieval.rerank_error,
            retrieval_trace=retrieval.trace.to_log_dict(),
        )


class RuleBasedSkillRouter(TwoStageSkillRouter):
    def __init__(self, catalog: SkillCatalog, max_skills: int = 2, recall_k: int = 8):
        super().__init__(catalog=catalog, max_skills=max_skills, recall_k=recall_k)
