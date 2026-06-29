from __future__ import annotations

from dataclasses import dataclass

from ..skills import MetadataSkillRetriever, RecalledSkill, SkillCatalog
from .ranking import split_reasons
from .schema import RetrievalCandidate, RetrievalResult, RetrievalStage, RetrievalTrace


@dataclass(frozen=True)
class SkillRetrievalOutput:
    recalled: list[RecalledSkill]
    trace: RetrievalTrace


class SkillToolRetriever:
    """Tool-facing skill retrieval adapter.

    This keeps the existing skill scoring policy intact while exposing the
    shared retrieval trace shape used by all retrieval tools.
    """

    def __init__(self, catalog: SkillCatalog):
        self.catalog = catalog

    def retrieve(self, query: str, limit: int) -> SkillRetrievalOutput:
        recalled = MetadataSkillRetriever(self.catalog, top_k=limit).retrieve(query)
        results = [_to_retrieval_result(item) for item in recalled]
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
                    output_count=len(results),
                    metadata={
                        "scoring_policy": "skill triggers/intents/tags/name/description; no memory decay",
                    },
                )
            ],
            results=results,
        )
        return SkillRetrievalOutput(recalled=recalled, trace=trace)


def _to_retrieval_result(item: RecalledSkill) -> RetrievalResult:
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
    )
