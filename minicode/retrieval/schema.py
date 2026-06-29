from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass(frozen=True)
class RetrievalCandidate:
    candidate_id: str
    kind: str
    title: str
    body: str = ""
    tags: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_log_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class RetrievalResult:
    candidate: RetrievalCandidate
    score: float
    reasons: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_log_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class RetrievalStage:
    name: str
    strategy: str
    input_count: int
    output_count: int
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_log_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class RetrievalTrace:
    tool_name: str
    query: str
    limit: int
    candidate_kind: str
    returned_count: int
    stages: list[RetrievalStage] = field(default_factory=list)
    results: list[RetrievalResult] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_log_dict(self) -> dict[str, Any]:
        return {
            "tool_name": self.tool_name,
            "query": self.query,
            "limit": self.limit,
            "candidate_kind": self.candidate_kind,
            "returned_count": self.returned_count,
            "stages": [stage.to_log_dict() for stage in self.stages],
            "results": [result.to_log_dict() for result in self.results],
            "metadata": self.metadata,
        }
