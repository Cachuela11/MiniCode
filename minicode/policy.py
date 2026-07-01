from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass(frozen=True)
class RequiredAction:
    action: str
    args: dict[str, Any]
    reason: str


@dataclass(frozen=True)
class PolicyDecision:
    intent: str = "general"
    rules: list[str] = field(default_factory=list)
    required_first_action: RequiredAction | None = None
    answer_hints: list[str] = field(default_factory=list)

    def to_log_dict(self) -> dict[str, Any]:
        return asdict(self)

    @property
    def has_directives(self) -> bool:
        return bool(self.rules or self.required_first_action or self.answer_hints)


class PolicyEngine:
    """Builds turn-level intervention directives before the model is called."""

    def decide(self, user_message: str) -> PolicyDecision:
        text = _normalize(user_message)
        rules: list[str] = []
        hints: list[str] = []
        required: RequiredAction | None = None
        intent = "general"

        if _matches(text, WORKSPACE_STRUCTURE_TERMS):
            intent = "workspace_structure"
            required = RequiredAction(
                action="list_files",
                args={"path": ".", "max_depth": 2, "limit": 200},
                reason="The user asked about the current project or directory structure.",
            )
            rules.extend(
                [
                    "Call list_files as the next action before finish or any other action.",
                    "Use fresh tool output even if Initial context already contains a file index.",
                    "Do not invent files that were not returned by tools.",
                ]
            )
            hints.append("Format the final answer as a readable short tree or bullet list.")

        elif _matches(text, WORKSPACE_INSPECTION_TERMS):
            intent = "workspace_inspection"
            rules.extend(
                [
                    "Use file tools before answering because the request depends on the current workspace.",
                    "Read relevant files before summarizing implementation details.",
                    "Do not rely only on memory or Initial context for file contents.",
                ]
            )

        if _matches(text, CODE_CHANGE_TERMS):
            intent = "code_change" if intent == "general" else intent
            rules.extend(
                [
                    "Keep edits focused on the user request.",
                    "After changing code, run a relevant test command when one is available, or explain why no test was run.",
                ]
            )

        if _matches(text, TEST_TERMS):
            intent = "test_or_debug" if intent == "general" else intent
            rules.append("Prefer run_tests for test commands; use run_shell only when run_tests is not suitable.")

        return PolicyDecision(
            intent=intent,
            rules=_dedupe(rules),
            required_first_action=required,
            answer_hints=_dedupe(hints),
        )


def render_policy_prompt(policy: PolicyDecision) -> str:
    if not policy.has_directives:
        return "No extra policy directives for this turn."

    lines = ["Policy directives for this turn:", f"- Detected intent: {policy.intent}"]
    if policy.required_first_action is not None:
        action = {
            "action": policy.required_first_action.action,
            "args": policy.required_first_action.args,
        }
        lines.extend(
            [
                "- Required first action:",
                f"  {json.dumps(action, ensure_ascii=False, separators=(',', ':'))}",
                f"  Reason: {policy.required_first_action.reason}",
            ]
        )
    if policy.rules:
        lines.append("- Rules:")
        lines.extend(f"  - {rule}" for rule in policy.rules)
    if policy.answer_hints:
        lines.append("- Answer hints:")
        lines.extend(f"  - {hint}" for hint in policy.answer_hints)
    return "\n".join(lines)


def requires_workspace_inspection(value: str) -> bool:
    text = _normalize(value)
    return _matches(text, WORKSPACE_STRUCTURE_TERMS) or _matches(text, WORKSPACE_INSPECTION_TERMS)


def required_first_action_prompt(value: str) -> str:
    policy = PolicyEngine().decide(value)
    if policy.required_first_action is None:
        return ""
    return render_policy_prompt(policy)


WORKSPACE_STRUCTURE_TERMS = [
    "项目结构",
    "目录结构",
    "文件结构",
    "当前项目",
    "当前目录",
    "查看项目",
    "查看一下当前项目",
    "看一下当前项目",
    "列出文件",
    "有哪些文件",
    "project structure",
    "workspace structure",
    "directory structure",
    "list files",
    "summarize this project",
    "summarize project",
]

WORKSPACE_INSPECTION_TERMS = [
    "查看",
    "看一下",
    "读一下",
    "打开文件",
    "分析项目",
    "分析代码",
    "review",
    "inspect",
    "read file",
    "analyze",
    "summarize",
]

CODE_CHANGE_TERMS = [
    "实现",
    "修改",
    "新增",
    "修复",
    "优化",
    "重构",
    "添加",
    "删除",
    "改一下",
    "implement",
    "change",
    "add",
    "fix",
    "refactor",
    "update",
    "delete",
]

TEST_TERMS = [
    "测试",
    "单元测试",
    "跑测试",
    "test",
    "pytest",
    "unittest",
]


def _normalize(value: str) -> str:
    return " ".join(value.lower().split())


def _matches(text: str, terms: list[str]) -> bool:
    return any(term.lower() in text for term in terms)


def _dedupe(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result
