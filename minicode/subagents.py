from __future__ import annotations

import fnmatch
import json
import os
import re
import shutil
import tempfile
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
SNAPSHOT_IGNORED_DIRS = {
    ".git",
    ".minicode",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    ".venv",
    "venv",
    "node_modules",
    "dist",
    "build",
}
SNAPSHOT_IGNORED_PATTERNS = {"*.pyc", "*.pyo", "*.egg-info"}
MAX_WORKFLOW_STAGES = 4
MAX_STAGE_NODES = 3
MAX_STAGE_EDGES = 6
MAX_STAGE_WORKFLOW_ITERATIONS = 6
MAX_EDGE_TRAVERSALS = 2
SUBAGENT_CONTEXT_LIMIT = 1200
SUBAGENT_SUMMARY_LIMIT = 800
NODE_HANDOFF_LIMIT = 1200
STAGE_HANDOFF_LIMIT = 1500
FINAL_HANDOFF_LIMIT = 2000
MAX_STRUCTURED_ITEMS = 3


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
    workspace_isolation: dict[str, Any] = field(default_factory=dict)

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
            "workspace_isolation": self.workspace_isolation,
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
class SubAgentWorkflowEdge:
    from_node: str
    to_node: str
    condition: str = "always"
    max_traversals: int = 1


@dataclass(frozen=True)
class SubAgentWorkflowStage:
    name: str
    nodes: list[SubAgentTask]
    edges: list[SubAgentWorkflowEdge] = field(default_factory=list)
    entry_nodes: list[str] = field(default_factory=list)
    max_iterations: int = 4


@dataclass
class SubAgentWorkflowStageResult:
    index: int
    name: str
    status: str
    results: list[SubAgentResult]
    handoff_context: str
    control_flow: dict[str, Any] = field(default_factory=dict)
    token_usage: TokenUsage = field(default_factory=TokenUsage)
    duration_ms: int = 0

    def to_observation_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "name": self.name,
            "status": self.status,
            "handoff_context": self.handoff_context,
            "control_flow": self.control_flow,
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
                        workspace_isolation={"mode": "snapshot", "created": False, "destroyed": True},
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
        temp_dir = tempfile.TemporaryDirectory(prefix="minicode-subagent-")
        snapshot_workspace = Path(temp_dir.name) / "workspace"
        isolation = {
            "mode": "snapshot",
            "source_workspace": str(self.workspace),
            "snapshot_workspace": str(snapshot_workspace),
            "created": False,
            "destroyed": False,
            "ignored_dirs": sorted(SNAPSHOT_IGNORED_DIRS),
        }
        result: SubAgentResult | None = None
        try:
            _copy_workspace_snapshot(self.workspace, snapshot_workspace)
            isolation["created"] = True
            result = self._run_one_in_workspace(task, snapshot_workspace, timer, isolation)
            return result
        finally:
            temp_dir.cleanup()
            isolation["destroyed"] = True
            if result is not None:
                result.workspace_isolation = dict(isolation)

    def _run_one_in_workspace(
        self,
        task: SubAgentTask,
        workspace: Path,
        timer: Timer,
        isolation: dict[str, Any],
    ) -> SubAgentResult:
        allowed_tools = _normalize_allowed_tools(task.allowed_tools)
        path_scope = _normalize_path_scope(task.path_scope)
        executor = ScopedToolExecutor(
            parent=self.tools,
            workspace=workspace,
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
            workspace_isolation=dict(isolation),
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
                answer = _compact_subagent_answer(args.get("answer") if "answer" in args else action.get("answer"))
                if not answer:
                    answer = _compact_subagent_answer(extract_finish_answer(action, args))
                result.steps.append(
                    SubAgentStep(
                        step=step_number,
                        action=action,
                        tool_name="finish",
                        tool_args=args,
                        ok=True,
                        output_preview=_preview(answer, SUBAGENT_SUMMARY_LIMIT),
                        exit_code=0,
                        token_usage=response.token_usage,
                        duration_ms=step_timer.elapsed_ms(),
                    )
                )
                result.status = "completed"
                result.summary = _preview(answer, SUBAGENT_SUMMARY_LIMIT)
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
                    output_preview=_preview(output, SUBAGENT_SUMMARY_LIMIT),
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
            batch, control_flow = self._run_stage(stage, handoff_context)
            workflow_usage.add(batch.token_usage)
            handoff_context = _stage_handoff_context(stage.name, batch.results)
            stage_result = SubAgentWorkflowStageResult(
                index=index,
                name=stage.name,
                status=batch.status,
                results=batch.results,
                handoff_context=handoff_context,
                control_flow=control_flow,
                token_usage=batch.token_usage,
                duration_ms=stage_timer.elapsed_ms(),
            )
            stage_results.append(stage_result)

        status = "completed" if all(stage.status == "completed" for stage in stage_results) else "partial"
        return SubAgentWorkflowResult(
            status=status,
            stages=stage_results,
            final_handoff_context=_preview(handoff_context, FINAL_HANDOFF_LIMIT),
            token_usage=workflow_usage,
            duration_ms=timer.elapsed_ms(),
        )

    def _run_stage(self, stage: SubAgentWorkflowStage, prior_handoff_context: str) -> tuple[SubAgentBatchResult, dict[str, Any]]:
        if not stage.edges:
            nodes = [_with_handoff_context(node, prior_handoff_context) for node in stage.nodes]
            batch = SubAgentRunner(
                llm=self.llm,
                model=self.model,
                tools=self.tools,
                max_parallel=self.max_parallel_per_stage,
            ).run_many(nodes)
            return batch, {"mode": "parallel", "iterations": 1 if nodes else 0, "events": []}
        return self._run_controlled_stage(stage, prior_handoff_context)

    def _run_controlled_stage(
        self,
        stage: SubAgentWorkflowStage,
        prior_handoff_context: str,
    ) -> tuple[SubAgentBatchResult, dict[str, Any]]:
        timer = Timer()
        runner = SubAgentRunner(
            llm=self.llm,
            model=self.model,
            tools=self.tools,
            max_parallel=self.max_parallel_per_stage,
        )
        nodes_by_name = {node.name: node for node in stage.nodes}
        outgoing: dict[str, list[SubAgentWorkflowEdge]] = {name: [] for name in nodes_by_name}
        incoming: dict[str, int] = {name: 0 for name in nodes_by_name}
        for edge in stage.edges:
            outgoing.setdefault(edge.from_node, []).append(edge)
            incoming[edge.to_node] = incoming.get(edge.to_node, 0) + 1

        ready = list(stage.entry_nodes or [name for name, count in incoming.items() if count == 0])
        if not ready and stage.nodes:
            ready = [stage.nodes[0].name]

        results: list[SubAgentResult] = []
        token_usage = TokenUsage()
        traversals: dict[str, int] = {}
        visits: dict[str, int] = {name: 0 for name in nodes_by_name}
        events: list[dict[str, Any]] = []
        stage_context = ""
        iteration = 0

        while ready and iteration < stage.max_iterations:
            iteration += 1
            wave_names = _dedupe_names([name for name in ready if name in nodes_by_name])
            ready = []
            tasks = []
            for name in wave_names:
                visits[name] += 1
                tasks.append(_with_stage_context(_with_handoff_context(nodes_by_name[name], prior_handoff_context), stage_context))
            batch = runner.run_many(tasks)
            token_usage.add(batch.token_usage)
            results.extend(batch.results)
            events.append({"iteration": iteration, "nodes": wave_names, "status": batch.status})
            stage_context = _stage_handoff_context(stage.name, results)

            for result in batch.results:
                for edge in outgoing.get(result.name, []):
                    edge_key = f"{edge.from_node}->{edge.to_node}:{edge.condition}"
                    used = traversals.get(edge_key, 0)
                    if used >= edge.max_traversals:
                        events.append(
                            {
                                "iteration": iteration,
                                "edge": edge_key,
                                "decision": "blocked",
                                "reason": "max_traversals reached",
                            }
                        )
                        continue
                    if not _edge_condition_matches(edge.condition, result):
                        events.append({"iteration": iteration, "edge": edge_key, "decision": "skipped"})
                        continue
                    traversals[edge_key] = used + 1
                    ready.append(edge.to_node)
                    events.append({"iteration": iteration, "edge": edge_key, "decision": "taken"})

        stopped_by_guard = bool(ready)
        if stopped_by_guard:
            events.append(
                {
                    "iteration": iteration,
                    "decision": "stopped",
                    "reason": "stage max_iterations reached; returning partial result to main agent",
                    "pending_nodes": ready,
                }
            )

        status = "completed" if results and not stopped_by_guard and all(result.status == "completed" for result in results) else "partial"
        return (
            SubAgentBatchResult(
                status=status,
                results=results,
                token_usage=token_usage,
                duration_ms=timer.elapsed_ms(),
            ),
            {
                "mode": "controlled_graph",
                "iterations": iteration,
                "max_iterations": stage.max_iterations,
                "edge_traversals": traversals,
                "node_visits": visits,
                "stopped_by_guard": stopped_by_guard,
                "events": events,
            },
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
            if name == "list_files":
                return self._list_files(args)
            if name == "read_file":
                return self._read_file(args)
            if name == "grep_files":
                return self._grep_files(args)
        return self.parent.execute(name, args)

    def _path_allowed(self, raw_path: str) -> bool:
        try:
            candidate = _resolve_under_workspace(self.workspace, raw_path)
        except ValueError:
            return False
        return any(candidate == scope or scope in candidate.parents for scope in self._resolved_scopes)

    def _list_files(self, args: dict[str, Any]):
        root = self._resolve_path(str(args.get("path", ".")))
        max_depth = _as_int(args.get("max_depth", 2), default=2, minimum=0, maximum=20)
        limit = _as_int(args.get("limit", 100), default=100, minimum=1, maximum=1000)

        rows: list[str] = []
        root_depth = len(root.relative_to(self.workspace).parts)
        for current, dirs, files in os.walk(root):
            current_path = Path(current)
            rel_depth = len(current_path.relative_to(self.workspace).parts) - root_depth
            if rel_depth >= max_depth:
                dirs[:] = []
            dirs[:] = [name for name in sorted(dirs) if name not in SNAPSHOT_IGNORED_DIRS]
            for filename in sorted(files):
                path = current_path / filename
                rel = path.relative_to(self.workspace).as_posix()
                if _is_secret_path(rel) or _matches_ignored_pattern(filename):
                    continue
                rows.append(rel)
                if len(rows) >= limit:
                    output = "\n".join(rows)
                    return _tool_result(True, output, exit_code=0)
        output = "\n".join(rows) or "No files found."
        return _tool_result(True, output, exit_code=0)

    def _read_file(self, args: dict[str, Any]):
        path = self._resolve_path(str(args.get("path", "")))
        rel = path.relative_to(self.workspace).as_posix()
        if _is_secret_path(rel):
            return _blocked_tool_result(f"ERROR: subagent cannot read secret-like file: {rel}")
        if not path.is_file():
            return _tool_result(False, f"ERROR: file not found: {rel}", exit_code=1)
        start_line = _as_int(args.get("start_line", 1), default=1, minimum=1, maximum=1_000_000)
        limit = _as_int(args.get("limit", 120), default=120, minimum=1, maximum=2000)
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        selected = lines[start_line - 1 : start_line - 1 + limit]
        output = "\n".join(f"{index}: {line}" for index, line in enumerate(selected, start=start_line))
        return _tool_result(True, output or "File is empty or range has no lines.", exit_code=0)

    def _grep_files(self, args: dict[str, Any]):
        pattern = str(args.get("pattern", "")).strip()
        if not pattern:
            return _tool_result(False, "ERROR: grep_files requires args.pattern.", exit_code=2, invalid=True)
        root = self._resolve_path(str(args.get("path", ".")))
        limit = _as_int(args.get("limit", 100), default=100, minimum=1, maximum=1000)
        flags = 0 if bool(args.get("case_sensitive", False)) else re.IGNORECASE
        try:
            regex = re.compile(pattern, flags=flags)
        except re.error as exc:
            return _tool_result(False, f"ERROR: invalid regex pattern: {exc}", exit_code=2, invalid=True)

        files = [root] if root.is_file() else sorted(path for path in root.rglob("*") if path.is_file())
        rows: list[str] = []
        for path in files:
            if _should_skip_snapshot_file(path, self.workspace):
                continue
            try:
                lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
            except OSError:
                continue
            rel = path.relative_to(self.workspace).as_posix()
            for line_number, line in enumerate(lines, start=1):
                if regex.search(line):
                    rows.append(f"{rel}:{line_number}: {line}")
                    if len(rows) >= limit:
                        return _tool_result(True, "\n".join(rows), exit_code=0)
        return _tool_result(True, "\n".join(rows) or "No matches found.", exit_code=0)

    def _resolve_path(self, raw_path: str) -> Path:
        if not raw_path:
            raise ValueError("path is required")
        return _resolve_under_workspace(self.workspace, raw_path)


def parse_subagent_tasks(args: dict[str, Any]) -> tuple[list[SubAgentTask], str]:
    raw_tasks = args.get("tasks")
    if not isinstance(raw_tasks, list) or not raw_tasks:
        return [], "run_subagents requires non-empty args.tasks list"
    if len(raw_tasks) > MAX_STAGE_NODES:
        return [], f"run_subagents supports at most {MAX_STAGE_NODES} subagent tasks per call"

    tasks: list[SubAgentTask] = []
    for index, item in enumerate(raw_tasks, start=1):
        if not isinstance(item, dict):
            return [], f"subagent task #{index} must be an object"
        task_text = str(item.get("task") or "").strip()
        if not task_text:
            return [], f"subagent task #{index} requires non-empty task"
        name = _slugify(str(item.get("name") or f"subagent_{index}"))
        context = _preview(str(item.get("context") or ""), SUBAGENT_CONTEXT_LIMIT)
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
    if len(raw_stages) > MAX_WORKFLOW_STAGES:
        return [], f"subagent workflow supports at most {MAX_WORKFLOW_STAGES} stages"

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
        edges, edge_error = _parse_stage_edges(item, nodes)
        if edge_error:
            return [], f"workflow stage #{index}: {edge_error}"
        entry_nodes, entry_error = _parse_entry_nodes(item, nodes, edges)
        if entry_error:
            return [], f"workflow stage #{index}: {entry_error}"
        max_iterations = _as_int(
            item.get("max_iterations"),
            default=min(MAX_STAGE_WORKFLOW_ITERATIONS, max(1, len(nodes) + len(edges))),
            minimum=1,
            maximum=MAX_STAGE_WORKFLOW_ITERATIONS,
        )
        stages.append(
            SubAgentWorkflowStage(
                name=name,
                nodes=nodes,
                edges=edges,
                entry_nodes=entry_nodes,
                max_iterations=max_iterations,
            )
        )
    return stages, ""


def _parse_stage_edges(item: dict[str, Any], nodes: list[SubAgentTask]) -> tuple[list[SubAgentWorkflowEdge], str]:
    raw_edges = item.get("edges")
    if raw_edges is None:
        raw_edges = item.get("control_edges")
    if raw_edges is None:
        return [], ""
    if not isinstance(raw_edges, list):
        return [], "stage edges must be a list"
    if len(raw_edges) > MAX_STAGE_EDGES:
        return [], f"stage supports at most {MAX_STAGE_EDGES} control edges"

    node_names = {node.name for node in nodes}
    edges: list[SubAgentWorkflowEdge] = []
    for edge_index, raw_edge in enumerate(raw_edges, start=1):
        if not isinstance(raw_edge, dict):
            return [], f"edge #{edge_index} must be an object"
        from_raw = raw_edge.get("from") or raw_edge.get("from_node")
        to_raw = raw_edge.get("to") or raw_edge.get("to_node")
        if not isinstance(from_raw, str) or not from_raw.strip() or not isinstance(to_raw, str) or not to_raw.strip():
            return [], f"edge #{edge_index} requires from and to"
        from_node = _slugify(from_raw)
        to_node = _slugify(to_raw)
        if from_node not in node_names or to_node not in node_names:
            return [], f"edge #{edge_index} references unknown node: {from_node}->{to_node}"
        condition = _preview(str(raw_edge.get("condition") or "always"), 240)
        max_traversals = _as_int(
            raw_edge.get("max_traversals"),
            default=1,
            minimum=1,
            maximum=MAX_EDGE_TRAVERSALS,
        )
        edges.append(
            SubAgentWorkflowEdge(
                from_node=from_node,
                to_node=to_node,
                condition=condition,
                max_traversals=max_traversals,
            )
        )
    return edges, ""


def _parse_entry_nodes(
    item: dict[str, Any],
    nodes: list[SubAgentTask],
    edges: list[SubAgentWorkflowEdge],
) -> tuple[list[str], str]:
    node_names = {node.name for node in nodes}
    raw_entries = item.get("entry_nodes")
    if raw_entries is None:
        raw_entries = item.get("entry")
    if raw_entries is None:
        entries = []
    elif isinstance(raw_entries, str):
        entries = [_slugify(raw_entries)]
    elif isinstance(raw_entries, list):
        entries = [_slugify(str(entry)) for entry in raw_entries]
    else:
        return [], "entry_nodes must be a string or list"
    entries = _dedupe_names([entry for entry in entries if entry])
    unknown = [entry for entry in entries if entry not in node_names]
    if unknown:
        return [], f"entry_nodes reference unknown nodes: {', '.join(unknown)}"
    if edges and not entries:
        incoming = {node.name: 0 for node in nodes}
        for edge in edges:
            incoming[edge.to_node] = incoming.get(edge.to_node, 0) + 1
        entries = [name for name, count in incoming.items() if count == 0]
        if not entries:
            return [], "controlled stage with cycles requires explicit entry_nodes"
    return entries, ""


def _subagent_system_prompt(tool_descriptions: str) -> str:
    return f"""You are a read-only MiniCode subagent controlled by a main agent.

You do not talk to the user directly. You investigate one bounded subtask and
return a compact report to the main agent. Do not modify files, run shell
commands, run tests, or spawn other subagents. Use only the listed tools and
only within the provided path scope.

Return exactly one JSON object and no Markdown fences. Every response must
include "action" and "args". For final reports use:
{{"action":"finish","args":{{"answer":{{"summary":"one sentence","findings":[{{"file":"path","line":1,"fact":"evidence"}}],"handoff":["facts the next node needs"],"next":["suggested next action"]}}}}}}
Keep final reports small: at most 3 findings, 3 handoff items, and 3 next items.

Available actions:
{tool_descriptions}
- finish: {{"answer": {{"summary": "...", "findings": [], "handoff": [], "next": []}}}}
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
            "- Return args.answer as a compact structured object.",
            "- summary: one short sentence about what you found.",
            "- findings: at most 3 useful facts, with file paths and line numbers when available.",
            "- handoff: at most 3 facts the next stage or main agent should keep.",
            "- next: at most 3 suggested next actions for the main agent.",
            "- Do not include full file contents or huge tool output.",
        ]
    )


def _fallback_summary(task: SubAgentTask, steps: list[SubAgentStep]) -> str:
    if not steps:
        return _compact_subagent_answer({"summary": "No steps were executed.", "findings": [], "handoff": [], "next": []})
    payload = {
        "summary": f"Subagent reached max steps while working on: {task.task}",
        "findings": [
            {"file": "", "line": None, "fact": f"{step.tool_name}: {_preview(step.output_preview, 180)}"}
            for step in steps[-MAX_STRUCTURED_ITEMS:]
        ],
        "handoff": [],
        "next": ["Main agent should inspect the trace before relying on this result."],
    }
    return _compact_subagent_answer(payload)


def _with_handoff_context(task: SubAgentTask, handoff_context: str) -> SubAgentTask:
    if not handoff_context:
        return task
    merged_context = "\n\n".join(
        part
        for part in [
            task.context.strip(),
            "Previous stage handoff context:\n" + _preview(handoff_context, NODE_HANDOFF_LIMIT),
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


def _with_stage_context(task: SubAgentTask, stage_context: str) -> SubAgentTask:
    if not stage_context:
        return task
    merged_context = "\n\n".join(
        part
        for part in [
            task.context.strip(),
            "Current stage workflow context:\n" + _preview(stage_context, NODE_HANDOFF_LIMIT),
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
    nodes: list[dict[str, Any]] = []
    for result in results:
        nodes.append(_handoff_node_payload(result))
    payload = {"stage": stage_name, "nodes": nodes}
    return _preview(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), STAGE_HANDOFF_LIMIT)


def _handoff_node_payload(result: SubAgentResult) -> dict[str, Any]:
    summary = _load_summary_payload(result.summary)
    return {
        "node": result.name,
        "status": result.status,
        "summary": _preview(str(summary.get("summary") or result.summary), 240),
        "findings": _compact_list(summary.get("findings"), MAX_STRUCTURED_ITEMS, 220),
        "handoff": _compact_list(summary.get("handoff"), MAX_STRUCTURED_ITEMS, 160),
        "next": _compact_list(summary.get("next"), MAX_STRUCTURED_ITEMS, 160),
    }


def _compact_subagent_answer(value: Any) -> str:
    payload = _load_summary_payload(value)
    if not payload:
        text = _preview(" ".join(str(value or "").split()), 240)
        return _fit_summary_json({"summary": text, "findings": [], "handoff": [], "next": []}, SUBAGENT_SUMMARY_LIMIT)

    compact = {
        "summary": _preview(str(payload.get("summary") or ""), 240),
        "findings": _compact_list(payload.get("findings"), MAX_STRUCTURED_ITEMS, 180),
        "handoff": _compact_list(payload.get("handoff"), MAX_STRUCTURED_ITEMS, 160),
        "next": _compact_list(payload.get("next"), MAX_STRUCTURED_ITEMS, 160),
    }
    if not compact["summary"]:
        compact["summary"] = _preview(" ".join(str(value or "").split()), 240)
    return _fit_summary_json(compact, SUBAGENT_SUMMARY_LIMIT)


def _load_summary_payload(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        text = value.strip()
        if text.startswith("{") and text.endswith("}"):
            try:
                parsed = json.loads(text)
            except json.JSONDecodeError:
                return {}
            if isinstance(parsed, dict):
                return parsed
    return {}


def _compact_list(value: Any, limit: int, item_limit: int) -> list[Any]:
    if not isinstance(value, list):
        return []
    compact: list[Any] = []
    for item in value[:limit]:
        if isinstance(item, dict):
            compact.append(
                {
                    key: _preview(str(val), item_limit)
                    for key, val in item.items()
                    if key in {"file", "line", "fact", "evidence", "detail", "reason"} and val is not None
                }
            )
        else:
            compact.append(_preview(str(item), item_limit))
    return compact


def _fit_summary_json(payload: dict[str, Any], limit: int) -> str:
    compact = dict(payload)
    for item_limit in [160, 120, 80, 50]:
        compact["summary"] = _preview(str(compact.get("summary") or ""), min(180, item_limit * 2))
        compact["findings"] = _compact_list(compact.get("findings"), MAX_STRUCTURED_ITEMS, item_limit)
        compact["handoff"] = _compact_list(compact.get("handoff"), MAX_STRUCTURED_ITEMS, item_limit)
        compact["next"] = _compact_list(compact.get("next"), MAX_STRUCTURED_ITEMS, item_limit)
        encoded = json.dumps(compact, ensure_ascii=False, separators=(",", ":"))
        if len(encoded) <= limit:
            return encoded

    compact["findings"] = compact.get("findings", [])[:1]
    compact["handoff"] = compact.get("handoff", [])[:1]
    compact["next"] = compact.get("next", [])[:1]
    compact["summary"] = _preview(str(compact.get("summary") or ""), 120)
    encoded = json.dumps(compact, ensure_ascii=False, separators=(",", ":"))
    if len(encoded) <= limit:
        return encoded
    minimal = {"summary": _preview(str(compact.get("summary") or ""), 120), "findings": [], "handoff": [], "next": []}
    return json.dumps(minimal, ensure_ascii=False, separators=(",", ":"))


def _edge_condition_matches(condition: str, result: SubAgentResult) -> bool:
    normalized = " ".join(str(condition or "always").lower().split())
    summary = _result_summary_text(result)
    if normalized in {"", "always", "true", "on_complete", "on_completed"}:
        return True
    if normalized in {"on_success", "success", "completed"}:
        return result.status == "completed"
    if normalized in {"on_failure", "failure", "failed", "error"}:
        return result.status != "completed"
    if normalized.startswith("contains:"):
        needle = normalized.split(":", 1)[1].strip()
        return bool(needle and needle in summary)
    if normalized.startswith("not_contains:"):
        needle = normalized.split(":", 1)[1].strip()
        return bool(needle and needle not in summary)
    if normalized in {"resolved", "if_resolved"}:
        return result.status == "completed" and not _looks_unresolved(summary)
    if normalized in {"unresolved", "if_unresolved", "needs_user", "blocked"}:
        return result.status != "completed" or _looks_unresolved(summary)
    if any(term in normalized for term in ["issue", "bug", "problem", "risk", "error", "失败", "问题", "风险"]):
        return any(term in summary for term in ["issue", "bug", "problem", "risk", "error", "fail", "失败", "问题", "风险"])
    return False


def _result_summary_text(result: SubAgentResult) -> str:
    payload = _load_summary_payload(result.summary)
    if payload:
        parts = [str(payload.get("summary") or "")]
        for key in ["findings", "handoff", "next"]:
            parts.append(json.dumps(payload.get(key) or [], ensure_ascii=False))
        return " ".join(parts).lower()
    return str(result.summary or "").lower()


def _looks_unresolved(summary: str) -> bool:
    return any(
        term in summary
        for term in [
            "unresolved",
            "unknown",
            "blocked",
            "cannot",
            "no useful",
            "max steps",
            "needs user",
            "无法",
            "未知",
            "阻塞",
            "需要用户",
        ]
    )


def _dedupe_names(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result


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


def _copy_workspace_snapshot(source: Path, target: Path) -> None:
    target.mkdir(parents=True, exist_ok=True)
    for item in source.iterdir():
        if _should_skip_snapshot_item(item, source):
            continue
        destination = target / item.name
        if item.is_dir():
            shutil.copytree(item, destination, ignore=_snapshot_ignore)
        elif item.is_file():
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(item, destination)


def _snapshot_ignore(directory: str, names: list[str]) -> set[str]:
    ignored: set[str] = set()
    base = Path(directory)
    for name in names:
        path = base / name
        if _should_skip_snapshot_item(path, base):
            ignored.add(name)
    return ignored


def _should_skip_snapshot_item(path: Path, root: Path) -> bool:
    name = path.name
    if name in SNAPSHOT_IGNORED_DIRS:
        return True
    if _matches_ignored_pattern(name):
        return True
    try:
        rel = path.relative_to(root).as_posix()
    except ValueError:
        rel = name
    if _is_secret_path(rel):
        return True
    return False


def _should_skip_snapshot_file(path: Path, workspace: Path) -> bool:
    try:
        rel_parts = path.relative_to(workspace).parts
        rel = path.relative_to(workspace).as_posix()
    except ValueError:
        return True
    if any(part in SNAPSHOT_IGNORED_DIRS for part in rel_parts):
        return True
    if _is_secret_path(rel) or _matches_ignored_pattern(path.name):
        return True
    try:
        return path.stat().st_size > 1_000_000
    except OSError:
        return True


def _matches_ignored_pattern(name: str) -> bool:
    return any(fnmatch.fnmatch(name, pattern) for pattern in SNAPSHOT_IGNORED_PATTERNS)


def _is_secret_path(rel_path: str) -> bool:
    path = rel_path.replace("\\", "/").lower()
    name = path.rsplit("/", 1)[-1]
    if name == ".env" or (name.startswith(".env.") and name != ".env.example"):
        return True
    if name in {"id_rsa", "id_dsa", "id_ecdsa", "id_ed25519"}:
        return True
    return name.endswith((".pem", ".key", ".p12", ".pfx"))


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


def _tool_result(ok: bool, output: str, *, exit_code: int | None, invalid: bool = False):
    from .tools import ToolResult

    return ToolResult(
        ok,
        output,
        stdout=output if ok else "",
        stderr="" if ok else output,
        exit_code=exit_code,
        invalid_command=invalid,
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
