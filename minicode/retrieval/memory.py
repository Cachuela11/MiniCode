from __future__ import annotations

import json
import math
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Protocol

from ..memory import FileMemoryStore, MemoryItem, MemorySearchResult, NullMemory
from ..observability import TokenUsage
from .ranking import normalize, split_reasons
from .schema import RetrievalCandidate, RetrievalResult, RetrievalStage, RetrievalTrace


@dataclass(frozen=True)
class MemoryRetrievalOutput:
    results: list[MemorySearchResult]
    trace: RetrievalTrace


class ChatClient(Protocol):
    def chat_response(self, model: str, messages: list[dict[str, str]]):
        ...


@dataclass(frozen=True)
class WeightedMemoryResult:
    result: MemorySearchResult
    base_score: float
    final_score: float
    weights: dict[str, float]
    reasons: list[str]


class MemoryToolRetriever:
    """Tool-facing memory retrieval adapter.

    Pipeline:
    lexical coarse recall -> local dynamic rerank/decay -> optional LLM rerank.
    """

    def __init__(
        self,
        store: FileMemoryStore | NullMemory,
        llm: ChatClient | None = None,
        model: str = "",
        coarse_limit: int = 30,
        llm_candidate_limit: int = 8,
    ):
        self.store = store
        self.llm = llm
        self.model = model
        self.coarse_limit = max(1, min(coarse_limit, 100))
        self.llm_candidate_limit = max(1, min(llm_candidate_limit, 20))

    def retrieve(self, query: str, limit: int) -> MemoryRetrievalOutput:
        scanned_count = len(self.store.all())
        coarse_limit = max(limit, self.coarse_limit)
        coarse_results = self.store.search(query, limit=coarse_limit)
        weighted_results = [_weight_result(query, result) for result in coarse_results]
        weighted_results = sorted(weighted_results, key=lambda item: (-item.final_score, item.result.item.memory_id))

        llm_error = ""
        llm_ranked_ids: list[str] = []
        rerank_candidates = weighted_results[: max(limit, self.llm_candidate_limit)]
        if self.llm is not None and self.model and rerank_candidates:
            try:
                llm_ranked_ids, llm_usage = _llm_rerank(
                    llm=self.llm,
                    model=self.model,
                    query=query,
                    candidates=rerank_candidates,
                    limit=limit,
                )
            except Exception as exc:
                llm_ranked_ids = []
                llm_usage = TokenUsage()
                llm_error = str(exc)
        else:
            llm_usage = TokenUsage()

        if llm_ranked_ids:
            by_id = {item.result.item.memory_id: item for item in rerank_candidates}
            selected_weighted = [by_id[memory_id] for memory_id in llm_ranked_ids if memory_id in by_id]
            selected_weighted.extend(item for item in rerank_candidates if item.result.item.memory_id not in llm_ranked_ids)
            selected_weighted = selected_weighted[:limit]
            reranker = "llm"
        else:
            selected_weighted = weighted_results[:limit]
            reranker = "local_weighted"

        results = [
            MemorySearchResult(
                item=item.result.item,
                score=max(1, int(round(item.final_score))),
                reason=", ".join(item.reasons[:12]),
            )
            for item in selected_weighted
        ]
        retrieval_results = [_to_retrieval_result(item, weighted=_find_weighted(item, selected_weighted)) for item in results]
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
                    output_count=len(coarse_results),
                    metadata={
                        "coarse_limit": coarse_limit,
                    },
                ),
                RetrievalStage(
                    name="weighted_rerank",
                    strategy="memory_type_confidence_importance_recency_usage_intent",
                    input_count=len(coarse_results),
                    output_count=len(rerank_candidates),
                    metadata={
                        "llm_candidate_limit": self.llm_candidate_limit,
                    },
                ),
                RetrievalStage(
                    name="llm_rerank",
                    strategy=reranker,
                    input_count=len(rerank_candidates),
                    output_count=len(results),
                    metadata={
                        "enabled": self.llm is not None and bool(self.model),
                        "error": llm_error,
                        "token_usage": {
                            "prompt_tokens": llm_usage.prompt_tokens,
                            "completion_tokens": llm_usage.completion_tokens,
                            "total_tokens": llm_usage.total_tokens,
                        },
                    },
                ),
            ],
            results=retrieval_results,
        )
        return MemoryRetrievalOutput(results=results, trace=trace)


def _to_retrieval_result(result: MemorySearchResult, weighted: WeightedMemoryResult | None = None) -> RetrievalResult:
    item = result.item
    metadata = {
        "memory_type": item.memory_type,
        "subtype": item.subtype,
        "source_path": item.source_path,
        "source_run_id": item.source_run_id,
        "parent_memory_ids": item.parent_memory_ids,
        "created_at": item.created_at,
        "updated_at": item.updated_at,
        "confidence": item.confidence,
        "importance": item.importance,
        "usage_count": item.usage_count,
        "last_used_at": item.last_used_at,
    }
    result_metadata = {}
    if weighted is not None:
        result_metadata = {
            "base_score": weighted.base_score,
            "final_score": weighted.final_score,
            "weights": weighted.weights,
        }
    return RetrievalResult(
        candidate=RetrievalCandidate(
            candidate_id=item.memory_id,
            kind="memory",
            title=item.title,
            body=item.body,
            tags=item.tags,
            metadata=metadata,
        ),
        score=float(result.score),
        reasons=split_reasons(result.reason),
        metadata=result_metadata,
    )


def _weight_result(query: str, result: MemorySearchResult) -> WeightedMemoryResult:
    item = result.item
    weights = {
        "type": _type_weight(item),
        "confidence": 0.5 + 0.5 * _clamp(item.confidence or 0.7),
        "importance": 0.7 + 0.3 * _clamp(item.importance),
        "recency": _recency_weight(item),
        "usage": 1.0 + min(math.log1p(item.usage_count) * 0.08, 0.3),
        "intent": _intent_weight(query, item),
    }
    final_score = float(result.score)
    for weight in weights.values():
        final_score *= weight
    reasons = split_reasons(result.reason)
    reasons.extend(f"weight:{name}:{value:.2f}" for name, value in weights.items())
    return WeightedMemoryResult(
        result=result,
        base_score=float(result.score),
        final_score=final_score,
        weights=weights,
        reasons=reasons,
    )


def _llm_rerank(
    llm: ChatClient,
    model: str,
    query: str,
    candidates: list[WeightedMemoryResult],
    limit: int,
) -> tuple[list[str], TokenUsage]:
    payload = {
        "query": query,
        "limit": limit,
        "candidates": [
            {
                "memory_id": item.result.item.memory_id,
                "type": item.result.item.memory_type,
                "subtype": item.result.item.subtype,
                "title": item.result.item.title,
                "tags": item.result.item.tags,
                "preview": _preview(item.result.item.body, limit=700),
                "base_score": item.base_score,
                "weighted_score": round(item.final_score, 4),
                "weights": item.weights,
                "reasons": item.reasons[:10],
            }
            for item in candidates
        ],
    }
    messages = [
        {
            "role": "system",
            "content": (
                "You rerank MiniCode memory search candidates. Return JSON only. "
                "Choose the most useful memories for the query. Prefer durable, specific, non-duplicate memories. "
                'Schema: {"memory_ids":["id"],"reason":"short reason"}.'
            ),
        },
        {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
    ]
    response = llm.chat_response(model=model, messages=messages)
    data = _parse_json_object(response.content)
    memory_ids = data.get("memory_ids", [])
    if not isinstance(memory_ids, list):
        memory_ids = []
    allowed = {item.result.item.memory_id for item in candidates}
    ranked_ids = [str(memory_id) for memory_id in memory_ids if str(memory_id) in allowed]
    return ranked_ids[:limit], response.token_usage


def _find_weighted(result: MemorySearchResult, weighted_results: list[WeightedMemoryResult]) -> WeightedMemoryResult | None:
    for weighted in weighted_results:
        if weighted.result.item.memory_id == result.item.memory_id:
            return weighted
    return None


def _type_weight(item: MemoryItem) -> float:
    if item.memory_type == "experience_memory":
        return 1.25
    if item.memory_type == "procedural_memory":
        return 1.15
    if item.memory_type == "project_memory":
        return 1.10
    if item.memory_type == "session_memory" and item.subtype == "session_summary":
        return 0.75
    if item.memory_type == "session_memory":
        return 0.55
    return 1.0


def _recency_weight(item: MemoryItem) -> float:
    age_days = _age_days(item.created_at)
    if item.memory_type == "session_memory" and item.subtype != "session_summary":
        return max(0.15, 0.5 ** (age_days / 3))
    if item.memory_type == "session_memory":
        return max(0.35, 0.5 ** (age_days / 14))
    if item.memory_type == "project_memory":
        return max(0.65, 0.5 ** (age_days / 180))
    if item.memory_type == "procedural_memory":
        return max(0.65, 0.5 ** (age_days / 90))
    if item.memory_type == "experience_memory":
        return max(0.70, 0.5 ** (age_days / 365))
    return 1.0


def _intent_weight(query: str, item: MemoryItem) -> float:
    text = normalize(query)
    if item.memory_type == "project_memory" and _contains_any(text, ["架构", "项目", "文件", "设计", "结构", "architecture", "project"]):
        return 1.25
    if item.memory_type == "procedural_memory" and _contains_any(text, ["流程", "修复", "测试", "怎么", "步骤", "workflow", "test", "fix"]):
        return 1.25
    if item.memory_type == "experience_memory" and _contains_any(text, ["偏好", "不要", "希望", "以后", "风格", "prefer", "style"]):
        return 1.25
    if item.memory_type == "session_memory" and _contains_any(text, ["刚才", "最近", "上次", "session", "recent"]):
        return 1.20
    return 1.0


def _age_days(value: str) -> float:
    parsed = _parse_datetime(value)
    if parsed is None:
        return 0.0
    return max(0.0, (datetime.now(timezone.utc) - parsed).total_seconds() / 86400)


def _parse_datetime(value: str) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _contains_any(text: str, needles: list[str]) -> bool:
    return any(needle in text for needle in needles)


def _preview(value: str, limit: int) -> str:
    text = " ".join(value.split())
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."


def _parse_json_object(text: str) -> dict:
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = stripped.strip("`")
        if stripped.startswith("json"):
            stripped = stripped[4:].strip()
    try:
        data = json.loads(stripped)
    except json.JSONDecodeError:
        start = stripped.find("{")
        end = stripped.rfind("}")
        if start == -1 or end == -1 or end <= start:
            raise
        data = json.loads(stripped[start : end + 1])
    if not isinstance(data, dict):
        raise ValueError("memory rerank response must be a JSON object")
    return data


def _clamp(value: float) -> float:
    return max(0.0, min(1.0, value))
