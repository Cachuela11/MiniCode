from __future__ import annotations

from dataclasses import dataclass

from ..memory import FileMemoryStore, MemorySearchResult, NullMemory
from .ranking import split_reasons
from .schema import RetrievalCandidate, RetrievalResult, RetrievalStage, RetrievalTrace


@dataclass(frozen=True)
class MemoryRetrievalOutput:
    results: list[MemorySearchResult]
    trace: RetrievalTrace


class MemoryToolRetriever:
    """Tool-facing memory retrieval adapter.

    The first version preserves the current memory scoring policy. Dynamic
    weighting, decay, usage feedback, and diversity rerank will be added here.
    """

    def __init__(self, store: FileMemoryStore | NullMemory):
        self.store = store

    def retrieve(self, query: str, limit: int) -> MemoryRetrievalOutput:
        scanned_count = len(self.store.all())
        results = self.store.search(query, limit=limit)
        retrieval_results = [_to_retrieval_result(item) for item in results]
        trace = RetrievalTrace(
            tool_name="search_memory",
            query=query,
            limit=limit,
            candidate_kind="memory",
            returned_count=len(retrieval_results),
            stages=[
                RetrievalStage(
                    name="lexical_recall",
                    strategy="memory_title_tag_body_rules",
                    input_count=scanned_count,
                    output_count=len(retrieval_results),
                    metadata={
                        "scoring_policy": "current lexical memory score; dynamic memory weighting not yet applied",
                    },
                )
            ],
            results=retrieval_results,
        )
        return MemoryRetrievalOutput(results=results, trace=trace)


def _to_retrieval_result(result: MemorySearchResult) -> RetrievalResult:
    item = result.item
    return RetrievalResult(
        candidate=RetrievalCandidate(
            candidate_id=item.memory_id,
            kind="memory",
            title=item.title,
            body=item.body,
            tags=item.tags,
            metadata={
                "memory_type": item.memory_type,
                "subtype": item.subtype,
                "source_path": item.source_path,
                "source_run_id": item.source_run_id,
                "parent_memory_ids": item.parent_memory_ids,
                "created_at": item.created_at,
                "updated_at": item.updated_at,
            },
        ),
        score=float(result.score),
        reasons=split_reasons(result.reason),
    )
