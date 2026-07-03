from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from typing import Any, Protocol

from .observability import TokenUsage


@dataclass(frozen=True)
class TaskModeDecision:
    mode: str = "default"
    reason: str = ""
    confidence: float = 0.0
    source: str = "none"
    planning_hints: list[str] = field(default_factory=list)
    token_usage: TokenUsage = field(default_factory=TokenUsage)
    error: str = ""

    @property
    def use_subagents(self) -> bool:
        return self.mode == "subagents"

    def to_log_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["token_usage"] = asdict(self.token_usage)
        return data


class ChatClient(Protocol):
    def chat_response(self, model: str, messages: list[dict[str, str]]):
        ...


class TaskModeRouter:
    def __init__(self, *, llm: ChatClient, model: str, mode: str = "auto"):
        self.llm = llm
        self.model = model
        self.mode = mode

    def decide(self, query: str) -> TaskModeDecision:
        if self.mode == "off":
            return TaskModeDecision(mode="default", reason="subagent mode disabled", source="disabled")
        if self.mode == "on":
            return TaskModeDecision(
                mode="subagents",
                reason="subagent mode forced by configuration",
                confidence=1.0,
                source="manual",
                planning_hints=_fallback_hints(query),
            )
        try:
            return self._llm_decide(query)
        except Exception as exc:
            fallback = _rule_fallback(query)
            return TaskModeDecision(
                mode=fallback.mode,
                reason=f"{fallback.reason}; LLM classifier failed: {exc}",
                confidence=fallback.confidence,
                source="rule_fallback",
                planning_hints=fallback.planning_hints,
                error=str(exc),
            )

    def _llm_decide(self, query: str) -> TaskModeDecision:
        messages = [
            {
                "role": "system",
                "content": (
                    "You classify MiniCode user tasks before the main agent loop. "
                    "Return one JSON object only. Choose mode=default for simple, single-file, "
                    "direct answer, or narrow tasks. Choose mode=subagents only when parallel "
                    "read-only investigation would materially help: multi-file debugging, broad "
                    "review, architecture analysis, unknown failure localization, or complex "
                    "implementation planning. You only decide whether the main agent should plan "
                    "subagents; do not create the final subagent task list."
                ),
            },
            {
                "role": "user",
                "content": json.dumps(
                    {
                        "query": query,
                        "schema": {
                            "mode": "default|subagents",
                            "reason": "short reason",
                            "confidence": 0.0,
                            "planning_hints": ["optional focus area for the main agent planner"],
                        },
                        "rules": [
                            "Return planning_hints=[] when mode=default.",
                            "When mode=subagents, return 1-4 short hints for what the main agent should consider while planning.",
                            "Do not return final subagent tasks.",
                        ],
                    },
                    ensure_ascii=False,
                ),
            },
        ]
        response = self.llm.chat_response(model=self.model, messages=messages)
        data = _parse_json_object(response.content)
        mode = "subagents" if str(data.get("mode") or "").lower() == "subagents" else "default"
        planning_hints = _as_string_list(data.get("planning_hints")) if mode == "subagents" else []
        if mode == "subagents" and not planning_hints:
            planning_hints = _fallback_hints(query)
        return TaskModeDecision(
            mode=mode,
            reason=str(data.get("reason") or ""),
            confidence=_as_float(data.get("confidence"), default=0.7),
            source="llm",
            planning_hints=planning_hints,
            token_usage=response.token_usage,
        )


def _rule_fallback(query: str) -> TaskModeDecision:
    text = " ".join(query.lower().split())
    complex_terms = [
        "review",
        "架构",
        "整体",
        "复杂",
        "多文件",
        "全局",
        "协作",
        "设计实现",
        "debug",
        "排查",
        "定位",
        "重构",
    ]
    use_subagents = len(query) > 120 or any(term in text for term in complex_terms)
    if not use_subagents:
        return TaskModeDecision(mode="default", reason="rule fallback classified task as simple", confidence=0.55)
    return TaskModeDecision(
        mode="subagents",
        reason="rule fallback detected broad or complex task signals",
        confidence=0.55,
        planning_hints=_fallback_hints(query),
    )


def _fallback_hints(query: str) -> list[str]:
    return [
        "先让主 Agent 根据用户目标拆出 1-4 个只读调查子任务。",
        "为每个子任务设置明确 path_scope、allowed_tools、max_steps 和 context。",
        f"用户任务：{query}",
    ]


def _as_string_list(value: Any) -> list[str]:
    if not isinstance(value, list) or not value:
        return []
    return [str(item).strip() for item in value if str(item).strip()][:4]


def _parse_json_object(text: str) -> dict[str, Any]:
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
        raise ValueError("task mode response must be a JSON object")
    return data


def _as_float(value: Any, default: float) -> float:
    try:
        return max(0.0, min(1.0, float(value)))
    except (TypeError, ValueError):
        return default
