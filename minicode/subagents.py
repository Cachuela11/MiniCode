from __future__ import annotations

import json
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Protocol

from .action_parser import extract_finish_answer, parse_action
from .observability import Timer, TokenUsage, summarize_messages


READ_ONLY_SUBAGENT_TOOLS = {
    "list_files",
    "read_file",
    "grep_files",
    "search_skills",
    "load_skill",
    "search_memory",
    "load_memory",
}
FORBIDDEN_SUBAGENT_TOOLS = {
    "write_file",
    "run_shell",
    "run_tests",
    "plan_subagents",
    "run_subagents",
    "plan_subagent_workflow",
    "run_subagent_workflow",
}


@dataclass(frozen=True)
class SubAgentTask:
    name: str
    task: str
    context: str = ""
    allowed_tools: list[str] = field(default_factory=lambda: sorted(READ_ONLY_SUBAGENT_TOOLS))
    path_scope: list[str] = field(default_factory=lambda: ["."])
    max_steps: int = 4


@dataclass
class SubAgentStep:
    step: int
    action: dict[str, Any]
    tool_name: str
    tool_args: dict[str, Any]
    ok: bool
    output_preview: str
    exit_code: int | None
    token_usage: TokenUsage
    duration_ms: int

    def to_log_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["token_usage"] = asdict(self.token_usage)
        return data


@dataclass
class SubAgentResult:
    name: str
    task: str
    context: str
    status: str
    summary: str
    allowed_tools: list[str]
    path_scope: list[str]
    steps: list[SubAgentStep] = field(default_factory=list)
    token_usage: TokenUsage = field(default_factory=TokenUsage)
    duration_ms: int = 0
    error: str = ""

    def to_observation_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "task": self.task,
            "context": self.context,
            "status": self.status,
            "summary": self.summary,
            "allowed_tools": self.allowed_tools,
            "path_scope": self.path_scope,
            "steps": len(self.steps),
            "token_usage": asdict(self.token_usage),
            "duration_ms": self.duration_ms,
            "error": self.error,
        }

    def to_log_dict(self) -> dict[str, Any]:
        data = self.to_observation_dict()
        data["trace"] = [step.to_log_dict() for step in self.steps]
        return data


@dataclass
class SubAgentBatchResult:
    status: str
    results: list[SubAgentResult]
    token_usage: TokenUsage = field(default_factory=TokenUsage)
    duration_ms: int = 0

    def to_observation_text(self) -> str:
        payload = {
            "status": self.status,
            "results": [result.to_observation_dict() for result in self.results],
            "token_usage": asdict(self.token_usage),
            "duration_ms": self.duration_ms,
        }
        return json.dumps(payload, indent=2, ensure_ascii=False)

    def to_log_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "results": [result.to_log_dict() for result in self.results],
            "token_usage": asdict(self.token_usage),
            "duration_ms": self.duration_ms,
        }


@dataclass(frozen=True)
class SubAgentWorkflowStage:
    name: str
    nodes: list[SubAgentTask]


@dataclass
class SubAgentWorkflowStageResult:
    index: int
    name: str
    status: str
    results: list[SubAgentResult]
    handoff_context: str
    token_usage: TokenUsage = field(default_factory=TokenUsage)
    duration_ms: int = 0

    def to_observation_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "name": self.name,
            "status": self.status,
            "handoff_context": self.handoff_context,
            "results": [result.to_observation_dict() for result in self.results],
            "token_usage": asdict(self.token_usage),
            "duration_ms": self.duration_ms,
        }

    def to_log_dict(self) -> dict[str, Any]:
        data = self.to_observation_dict()
        data["results"] = [result.to_log_dict() for result in self.results]
        return data


@dataclass
class SubAgentWorkflowResult:
    status: str
    stages: list[SubAgentWorkflowStageResult]
    final_handoff_context: str
    token_usage: TokenUsage = field(default_factory=TokenUsage)
    duration_ms: int = 0

    def to_observation_text(self) -> str:
        payload = {
            "status": self.status,
            "final_handoff_context": self.final_handoff_context,
            "stages": [stage.to_observation_dict() for stage in self.stages],
            "token_usage": asdict(self.token_usage),
            "duration_ms": self.duration_ms,
        }
        return json.dumps(payload, indent=2, ensure_ascii=False)

    def to_log_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "final_handoff_context": self.final_handoff_context,
            "stages": [stage.to_log_dict() for stage in self.stages],
            "token_usage": asdict(self.token_usage),
            "duration_ms": self.duration_ms,
        }


class ChatClient(Protocol):
    def chat_response(self, model: str, messages: list[dict[str, str]]):
        ...


class ParentToolExecutor(Protocol):
    workspace: Path

    def execute(self, name: str, args: dict[str, Any]):
        ...


class SubAgentRunner:
    def __init__(
        self,
        *,
        llm: ChatClient,
        model: str,
        tools: ParentToolExecutor,
        max_parallel: int = 4,
    ):
        self.llm = llm
        self.model = model
        self.tools = tools
        self.workspace = tools.workspace.resolve()
        self.max_parallel = max(1, min(8, max_parallel))

    def run_many(self, tasks: list[SubAgentTask]) -> SubAgentBatchResult:
        timer = Timer()
        if not tasks:
            return SubAgentBatchResult(status="empty", results=[], duration_ms=timer.elapsed_ms())

        results: list[SubAgentResult] = []
        with ThreadPoolExecutor(max_workers=min(self.max_parallel, len(tasks))) as executor:
            future_to_index = {
                executor.submit(self.run_one, task): index
                for index, task in enumerate(tasks)
            }
            ordered: dict[int, SubAgentResult] = {}
            for future in as_completed(future_to_index):
                index = future_to_index[future]
                try:
                    ordered[index] = future.result()
                except Exception as exc:
                    task = tasks[index]
                    ordered[index] = SubAgentResult(
                        name=task.name,
                        task=task.task,
                        context=task.context,
                        status="error",
                        summary="Subagent crashed before returning a result.",
                        allowed_tools=task.allowed_tools,
                        path_scope=task.path_scope,
                        error=str(exc),
                    )
            results = [ordered[index] for index in sorted(ordered)]

        token_usage = TokenUsage()
        for result in results:
            token_usage.add(result.token_usage)
        status = "completed" if all(result.status == "completed" for result in results) else "partial"
        return SubAgentBatchResult(
            status=status,
            results=results,
            token_usage=token_usage,
            duration_ms=timer.elapsed_ms(),
        )

    def run_one(self, task: SubAgentTask) -> SubAgentResult:
        timer = Timer()
        allowed_tools = _normalize_allowed_tools(task.allowed_tools)
        path_scope = _normalize_path_scope(task.path_scope)
        executor = ScopedToolExecutor(
            parent=self.tools,
            workspace=self.workspace,
            allowed_tools=allowed_tools,
            path_scope=path_scope,
        )
        result = SubAgentResult(
            name=task.name,
            task=task.task,
            context=task.context,
            status="started",
            summary="",
            allowed_tools=allowed_tools,
            path_scope=path_scope,
        )
        messages = [
            {
                "role": "system",
                "content": _subagent_system_prompt(executor.describe_allowed_tools()),
            },
            {
                "role": "user",
                "content": _subagent_user_prompt(task),
            },
        ]

        for step_number in range(1, max(1, min(8, task.max_steps)) + 1):
            step_timer = Timer()
            response = self.llm.chat_response(model=self.model, messages=messages)
            result.token_usage.add(response.token_usage)
            action = parse_action(response.content)
            name = str(action.get("action") or "")
            args = action.get("args") or {}
            if not isinstance(args, dict):
                args = {}
            if name == "finish":
                answer = extract_finish_answer(action, args)
                result.steps.append(
                    SubAgentStep(
                        step=step_number,
                        action=action,
                        tool_name="finish",
                        tool_args=args,
                        ok=True,
                        output_preview=_preview(answer, 1200),
                        exit_code=0,
                        token_usage=response.token_usage,
                        duration_ms=step_timer.elapsed_ms(),
                    )
                )
                result.status = "completed"
                result.summary = _preview(answer, 2000)
                result.duration_ms = timer.elapsed_ms()
                return result

            tool_result = executor.execute(name, args)
            output = tool_result.output
            result.steps.append(
                SubAgentStep(
                    step=step_number,
                    action=action,
                    tool_name=name,
                    tool_args=args,
                    ok=bool(tool_result.ok),
                    output_preview=_preview(output, 1200),
                    exit_code=tool_result.exit_code,
                    token_usage=response.token_usage,
                    duration_ms=step_timer.elapsed_ms(),
                )
            )
            messages.append({"role": "assistant", "content": json.dumps(action, ensure_ascii=False)})
            messages.append(
                {
                    "role": "user",
                    "content": "Observation:\n" + _preview(output, 4000),
                }
            )

        result.status = "max_steps"
        result.summary = _fallback_summary(task, result.steps)
        result.duration_ms = timer.elapsed_ms()
        return result


class SubAgentWorkflowRunner:
    def __init__(
        self,
        *,
        llm: ChatClient,
        model: str,
        tools: ParentToolExecutor,
        max_parallel_per_stage: int = 4,
    ):
        self.llm = llm
        self.model = model
        self.tools = tools
        self.max_parallel_per_stage = max(1, min(8, max_parallel_per_stage))

    def run(self, stages: list[SubAgentWorkflowStage]) -> SubAgentWorkflowResult:
        timer = Timer()
        stage_results: list[SubAgentWorkflowStageResult] = []
        workflow_usage = TokenUsage()
        handoff_context = ""

        for index, stage in enumerate(stages, start=1):
            stage_timer = Timer()
            nodes = [_with_handoff_context(node, handoff_context) for node in stage.nodes]
            batch = SubAgentRunner(
                llm=self.llm,
                model=self.model,
                tools=self.tools,
                max_parallel=self.max_parallel_per_stage,
            ).run_many(nodes)
            workflow_usage.add(batch.token_usage)
            handoff_context = _stage_handoff_context(stage.name, batch.results)
            stage_result = SubAgentWorkflowStageResult(
                index=index,
                name=stage.name,
                status=batch.status,
                results=batch.results,
                handoff_context=handoff_context,
                token_usage=batch.token_usage,
                duration_ms=stage_timer.elapsed_ms(),
            )
            stage_results.append(stage_result)

        status = "completed" if all(stage.status == "completed" for stage in stage_results) else "partial"
        return SubAgentWorkflowResult(
            status=status,
            stages=stage_results,
            final_handoff_context=handoff_context,
            token_usage=workflow_usage,
            duration_ms=timer.elapsed_ms(),
        )


class ScopedToolExecutor:
    def __init__(
        self,
        *,
        parent: ParentToolExecutor,
        workspace: Path,
        allowed_tools: list[str],
        path_scope: list[str],
    ):
        self.parent = parent
        self.workspace = workspace.resolve()
        self.allowed_tools = allowed_tools
        self.path_scope = path_scope
        self._resolved_scopes = [_resolve_under_workspace(self.workspace, scope) for scope in path_scope]

    def describe_allowed_tools(self) -> str:
        descriptions = {
            "list_files": '- list_files: {"path": "scoped/path", "max_depth": 2, "limit": 100}',
            "read_file": '- read_file: {"path": "scoped/path.py", "start_line": 1, "limit": 120}',
            "grep_files": '- grep_files: {"pattern": "text or regex", "path": "scoped/path", "limit": 100, "case_sensitive": false}',
            "search_skills": '- search_skills: {"query": "what workflow you need", "limit": 3}',
            "load_skill": '- load_skill: {"name": "skill_name", "max_chars": 3000}',
            "search_memory": '- search_memory: {"query": "project fact or lesson", "limit": 3}',
            "load_memory": '- load_memory: {"memory_id": "memory-id", "max_chars": 3000}',
        }
        return "\n".join(descriptions[name] for name in self.allowed_tools if name in descriptions)

    def execute(self, name: str, args: dict[str, Any]):
        if name not in self.allowed_tools:
            return _blocked_tool_result(
                f"ERROR: subagent is not allowed to use tool {name!r}. Allowed tools: {', '.join(self.allowed_tools)}"
            )
        if name in FORBIDDEN_SUBAGENT_TOOLS:
            return _blocked_tool_result(f"ERROR: subagent tool {name!r} is forbidden in this version.")
        if name in {"list_files", "read_file", "grep_files"}:
            raw_path = str(args.get("path", ".")).strip() or "."
            if not self._path_allowed(raw_path):
                return _blocked_tool_result(
                    f"ERROR: subagent path {raw_path!r} is outside allowed scope: {', '.join(self.path_scope)}"
                )
        return self.parent.execute(name, args)

    def _path_allowed(self, raw_path: str) -> bool:
        try:
            candidate = _resolve_under_workspace(self.workspace, raw_path)
        except ValueError:
            return False
        return any(candidate == scope or scope in candidate.parents for scope in self._resolved_scopes)


def parse_subagent_tasks(args: dict[str, Any]) -> tuple[list[SubAgentTask], str]:
    raw_tasks = args.get("tasks")
    if not isinstance(raw_tasks, list) or not raw_tasks:
        return [], "run_subagents requires non-empty args.tasks list"
    if len(raw_tasks) > 6:
        return [], "run_subagents supports at most 6 subagent tasks per call"

    tasks: list[SubAgentTask] = []
    for index, item in enumerate(raw_tasks, start=1):
        if not isinstance(item, dict):
            return [], f"subagent task #{index} must be an object"
        task_text = str(item.get("task") or "").strip()
        if not task_text:
            return [], f"subagent task #{index} requires non-empty task"
        name = _slugify(str(item.get("name") or f"subagent_{index}"))
        context = _preview(str(item.get("context") or ""), 1200)
        allowed_tools = _normalize_allowed_tools(item.get("allowed_tools"))
        path_scope = _normalize_path_scope(item.get("path_scope"))
        max_steps = _as_int(item.get("max_steps"), default=4, minimum=1, maximum=8)
        tasks.append(
            SubAgentTask(
                name=name,
                task=task_text,
                context=context,
                allowed_tools=allowed_tools,
                path_scope=path_scope,
                max_steps=max_steps,
            )
        )
    return tasks, ""


def parse_subagent_workflow(args: dict[str, Any]) -> tuple[list[SubAgentWorkflowStage], str]:
    raw_stages = args.get("stages")
    if not isinstance(raw_stages, list) or not raw_stages:
        return [], "subagent workflow requires non-empty args.stages list"
    if len(raw_stages) > 6:
        return [], "subagent workflow supports at most 6 stages"

    stages: list[SubAgentWorkflowStage] = []
    for index, item in enumerate(raw_stages, start=1):
        if not isinstance(item, dict):
            return [], f"workflow stage #{index} must be an object"
        name = _slugify(str(item.get("name") or f"stage_{index}"))
        raw_nodes = item.get("nodes")
        if raw_nodes is None:
            raw_nodes = item.get("tasks")
        nodes, error = parse_subagent_tasks({"tasks": raw_nodes})
        if error:
            return [], f"workflow stage #{index}: {error}"
        stages.append(SubAgentWorkflowStage(name=name, nodes=nodes))
    return stages, ""


def _subagent_system_prompt(tool_descriptions: str) -> str:
    return f"""You are a read-only MiniCode subagent controlled by a main agent.

You do not talk to the user directly. You investigate one bounded subtask and
return a compact report to the main agent. Do not modify files, run shell
commands, run tests, or spawn other subagents. Use only the listed tools and
only within the provided path scope.

Return exactly one JSON object and no Markdown fences. Every response must
include "action" and "args". For final reports use:
{{"action":"finish","args":{{"answer":"compact report with findings, evidence, and suggested next actions"}}}}

Available actions:
{tool_descriptions}
- finish: {{"answer": "compact report for the main agent"}}
"""


def _subagent_user_prompt(task: SubAgentTask) -> str:
    return "\n".join(
        [
            "Subtask:",
            task.task,
            "",
            "Main-agent context for this subtask:",
            task.context or "No extra context provided.",
            "",
            "Path scope:",
            "\n".join(f"- {scope}" for scope in task.path_scope),
            "",
            "Report requirements:",
            "- Summarize only the useful findings.",
            "- Include file paths and line numbers when available.",
            "- Say what you inspected.",
            "- Suggest next actions for the main agent.",
            "- Do not include full file contents or huge tool output.",
        ]
    )


def _fallback_summary(task: SubAgentTask, steps: list[SubAgentStep]) -> str:
    if not steps:
        return "No steps were executed."
    rows = [f"Subagent reached max steps while working on: {task.task}", "Executed tools:"]
    rows.extend(f"- {step.tool_name}: {step.output_preview}" for step in steps[-3:])
    return "\n".join(rows)


def _with_handoff_context(task: SubAgentTask, handoff_context: str) -> SubAgentTask:
    if not handoff_context:
        return task
    merged_context = "\n\n".join(
        part
        for part in [
            task.context.strip(),
            "Previous stage handoff context:\n" + _preview(handoff_context, 3000),
        ]
        if part
    )
    return SubAgentTask(
        name=task.name,
        task=task.task,
        context=merged_context,
        allowed_tools=task.allowed_tools,
        path_scope=task.path_scope,
        max_steps=task.max_steps,
    )


def _stage_handoff_context(stage_name: str, results: list[SubAgentResult]) -> str:
    rows = [f"Stage {stage_name} completed. Useful context for the next stage:"]
    for result in results:
        rows.append(
            "\n".join(
                [
                    f"- node: {result.name}",
                    f"  status: {result.status}",
                    f"  summary: {_preview(result.summary, 800)}",
                ]
            )
        )
    return _preview("\n".join(rows), 3500)


def _normalize_allowed_tools(value: Any) -> list[str]:
    if not isinstance(value, list) or not value:
        return sorted(READ_ONLY_SUBAGENT_TOOLS)
    tools = sorted({str(item).strip() for item in value if str(item).strip()})
    return [tool for tool in tools if tool in READ_ONLY_SUBAGENT_TOOLS and tool not in FORBIDDEN_SUBAGENT_TOOLS]


def _normalize_path_scope(value: Any) -> list[str]:
    if not isinstance(value, list) or not value:
        return ["."]
    scopes = [str(item).strip().replace("\\", "/") for item in value if str(item).strip()]
    return scopes or ["."]


def _resolve_under_workspace(workspace: Path, raw_path: str) -> Path:
    candidate = (workspace / raw_path).resolve()
    if candidate == workspace or workspace in candidate.parents:
        return candidate
    raise ValueError(f"path escapes workspace: {raw_path}")


def _blocked_tool_result(message: str):
    from .tools import ToolResult

    return ToolResult(
        False,
        message,
        stderr=message,
        exit_code=126,
        permission_decision="deny",
        permission_reason="subagent scope denied",
        invalid_command=True,
    )


def _as_int(value: Any, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, min(maximum, parsed))


def _slugify(value: str) -> str:
    slug = re.sub(r"[^0-9A-Za-z_.-]+", "_", value.strip()).strip("_.-").lower()
    return slug or "subagent"


def _preview(value: str, limit: int) -> str:
    text = str(value or "").strip()
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."
