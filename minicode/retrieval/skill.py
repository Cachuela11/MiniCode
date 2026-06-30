from __future__ import annotations

from dataclasses import dataclass

from ..skills.catalog import SkillCatalog
from ..skills.ranker import LlmSkillRanker, RuleBasedSkillRanker
from ..skills.retriever import MetadataSkillRetriever
from ..skills.schema import RecalledSkill, SelectedSkill
from .ranking import split_reasons
from .schema import RetrievalCandidate, RetrievalResult, RetrievalStage, RetrievalTrace


@dataclass(frozen=True)
class SkillRetrievalOutput:
    recalled: list[RecalledSkill]
    selected: list[SelectedSkill]
    rejected: list[str]
    intent: str
    reranker: str
    rerank_token_usage: dict[str, int]
    rerank_error: str
    trace: RetrievalTrace


class SkillToolRetriever:
    """Shared skill retrieval adapter for automatic routing and tool search."""

    def __init__(
        self,
        catalog: SkillCatalog,
        llm=None,
        model: str = "",
        recall_k: int = 8,
    ):
        self.catalog = catalog
        self.llm = llm
        self.model = model
        self.recall_k = max(0, recall_k)

    def retrieve(self, query: str, limit: int) -> SkillRetrievalOutput:
        final_limit = max(0, limit)
        if final_limit == 0:
            trace = RetrievalTrace(
                tool_name="search_skills",
                query=query,
                limit=limit,
                candidate_kind="skill",
                returned_count=0,
            )
            return SkillRetrievalOutput(
                recalled=[],
                selected=[],
                rejected=self.catalog.names(),
                intent="none",
                reranker="none",
                rerank_token_usage={},
                rerank_error="",
                trace=trace,
            )

        recall_limit = max(final_limit, self.recall_k)
        recalled = MetadataSkillRetriever(self.catalog, top_k=recall_limit).retrieve(query)
        selected, intent, reranker, token_usage, error = self._rerank(query, recalled, final_limit)
        selected_names = {item.skill.name for item in selected}
        recalled_names = {item.skill.name for item in recalled}
        rerank_rejected = sorted(recalled_names - selected_names)
        not_recalled = sorted(set(self.catalog.names()) - recalled_names)
        if recalled:
            rejected = [f"rerank_rejected:{name}" for name in rerank_rejected]
            rejected.extend(f"not_recalled:{name}" for name in not_recalled)
        else:
            rejected = self.catalog.names()

        results = [_selected_to_retrieval_result(item) for item in selected]
        trace = RetrievalTrace(
            tool_name="search_skills",
            query=query,
            limit=limit,
            candidate_kind="skill",
            returned_count=len(results),
            stages=[
                RetrievalStage(
                    name="metadata_recall",
                    strategy="skill_metadata_rules",
                    input_count=len(self.catalog.all()),
                    output_count=len(recalled),
                    metadata={
                        "recall_limit": recall_limit,
                        "scoring_policy": "skill triggers/intents/tags/name/description; no memory decay",
                    },
                ),
                RetrievalStage(
                    name="llm_rerank",
                    strategy=reranker,
                    input_count=len(recalled),
                    output_count=len(selected),
                    metadata={
                        "enabled": self.llm is not None and bool(self.model),
                        "error": error,
                        "candidate_names": [item.skill.name for item in recalled],
                        "selected_names": [item.skill.name for item in selected],
                        "token_usage": token_usage,
                    },
                ),
            ],
            results=results,
            metadata={
                "intent": intent,
                "rejected": rejected,
            },
        )
        return SkillRetrievalOutput(
            recalled=recalled,
            selected=selected,
            rejected=rejected,
            intent=intent,
            reranker=reranker,
            rerank_token_usage=token_usage,
            rerank_error=error,
            trace=trace,
        )

    def _rerank(
        self,
        query: str,
        recalled: list[RecalledSkill],
        limit: int,
    ) -> tuple[list[SelectedSkill], str, str, dict[str, int], str]:
        if not recalled:
            return [], "general", "none", {}, ""
        if self.llm is None or not self.model:
            selected = RuleBasedSkillRanker(max_skills=limit).rank(query, recalled)
            intent = selected[0].skill.intents[0] if selected and selected[0].skill.intents else "general"
            return selected, intent, "rule", {}, ""
        result = LlmSkillRanker(self.llm, model=self.model, max_skills=limit).rank(query, recalled)
        return result.selected, result.intent, result.reranker, result.token_usage, result.error


def _selected_to_retrieval_result(item: SelectedSkill) -> RetrievalResult:
    skill = item.skill
    return RetrievalResult(
        candidate=RetrievalCandidate(
            candidate_id=skill.name,
            kind="skill",
            title=skill.name,
            body=skill.description,
            tags=skill.tags,
            metadata={
                "intents": skill.intents,
                "triggers": skill.triggers,
                "recommended_tools": skill.tools,
                "source_path": skill.source_path,
            },
        ),
        score=float(item.score),
        reasons=split_reasons(item.reason),
        metadata={
            "recall_score": item.recall_score,
        },
    )
