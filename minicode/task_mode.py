from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from typing import Any, Protocol

from .observability import TokenUsage


@dataclass(frozen=True)
class SubAgentTaskPlan:
    name: str
    task: str
    allowed_tools: list[str] = field(default_factory=lambda: ["list_files", "read_file", "grep_files"])
    path_scope: list[str] = field(default_factory=lambda: ["."])
    max_steps: int = 4

    def to_action_args(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class TaskModeDecision:
    mode: str = "default"
    reason: str = ""
    confidence: float = 0.0
    source: str = "none"
    tasks: list[SubAgentTaskPlan] = field(default_factory=list)
    token_usage: TokenUsage = field(default_factory=TokenUsage)
    error: str = ""

    @property
    def use_subagents(self) -> bool:
        return self.mode == "subagents" and bool(self.tasks)

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
                tasks=_fallback_tasks(query),
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
                tasks=fallback.tasks,
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
                    "implementation planning. Subagents are read-only investigators. They cannot "
                    "write files, run shell, run tests, or spawn subagents."
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
                            "tasks": [
                                {
                                    "name": "short_snake_case",
                                    "task": "bounded investigation task",
                                    "allowed_tools": ["list_files", "read_file", "grep_files"],
                                    "path_scope": ["relative/path"],
                                    "max_steps": 4,
                                }
                            ],
                        },
                        "rules": [
                            "Return tasks=[] when mode=default.",
                            "Return 1-4 tasks when mode=subagents.",
                            "Prefer path_scope ['.'] only when the relevant area is unknown.",
                            "Use only read-only tools: list_files, read_file, grep_files, search_skills, load_skill, search_memory, load_memory.",
                        ],
                    },
                    ensure_ascii=False,
                ),
            },
        ]
        response = self.llm.chat_response(model=self.model, messages=messages)
        data = _parse_json_object(response.content)
        mode = "subagents" if str(data.get("mode") or "").lower() == "subagents" else "default"
        tasks = _parse_tasks(data.get("tasks")) if mode == "subagents" else []
        if mode == "subagents" and not tasks:
            tasks = _fallback_tasks(query)
        return TaskModeDecision(
            mode=mode,
            reason=str(data.get("reason") or ""),
            confidence=_as_float(data.get("confidence"), default=0.7),
            source="llm",
            tasks=tasks,
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
        tasks=_fallback_tasks(query),
    )


def _fallback_tasks(query: str) -> list[SubAgentTaskPlan]:
    return [
        SubAgentTaskPlan(
            name="inspect_relevant_files",
            task=(
                "调查当前任务相关的文件、符号和实现线索，返回关键发现、证据路径和建议下一步。"
                f" 用户任务：{query}"
            ),
            allowed_tools=["list_files", "read_file", "grep_files"],
            path_scope=["."],
            max_steps=4,
        )
    ]


def _parse_tasks(value: Any) -> list[SubAgentTaskPlan]:
    if not isinstance(value, list):
        return []
    tasks: list[SubAgentTaskPlan] = []
    for index, item in enumerate(value[:4], start=1):
        if not isinstance(item, dict):
            continue
        task = str(item.get("task") or "").strip()
        if not task:
            continue
        name = _slugify(str(item.get("name") or f"subagent_{index}"))
        allowed_tools = _allowed_tools(item.get("allowed_tools"))
        path_scope = _path_scope(item.get("path_scope"))
        max_steps = _as_int(item.get("max_steps"), default=4, minimum=1, maximum=8)
        tasks.append(
            SubAgentTaskPlan(
                name=name,
                task=task,
                allowed_tools=allowed_tools,
                path_scope=path_scope,
                max_steps=max_steps,
            )
        )
    return tasks


def _allowed_tools(value: Any) -> list[str]:
    default = ["list_files", "read_file", "grep_files"]
    allowed = {
        "list_files",
        "read_file",
        "grep_files",
        "search_skills",
        "load_skill",
        "search_memory",
        "load_memory",
    }
    if not isinstance(value, list) or not value:
        return default
    result = [str(item).strip() for item in value if str(item).strip() in allowed]
    return result or default


def _path_scope(value: Any) -> list[str]:
    if not isinstance(value, list) or not value:
        return ["."]
    result = [str(item).strip().replace("\\", "/") for item in value if str(item).strip()]
    return result or ["."]


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


def _as_int(value: Any, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, min(maximum, parsed))


def _slugify(value: str) -> str:
    slug = re.sub(r"[^0-9A-Za-z_.-]+", "_", value.strip()).strip("_.-").lower()
    return slug or "subagent"
