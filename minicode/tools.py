from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Callable

from .memory import FileMemoryStore, NullMemory
from .permissions import Decision
from .retrieval.memory import MemoryToolRetriever
from .retrieval.skill import SkillToolRetriever
from .sandbox import DockerSandbox, SandboxResult
from .security import ToolSecurityReviewer
from .skills import SkillCatalog
from .subagents import SubAgentRunner, parse_subagent_tasks


@dataclass(frozen=True)
class ToolResult:
    ok: bool
    output: str
    stdout: str = ""
    stderr: str = ""
    exit_code: int | None = None
    permission_decision: str = "not_applicable"
    permission_reason: str = ""
    dangerous_command: bool = False
    invalid_command: bool = False
    duration_ms: int = 0
    retrieval_trace: dict[str, Any] | None = None
    subagent_trace: dict[str, Any] | None = None


ToolHandler = Callable[[dict[str, Any]], ToolResult]


class ToolRegistry:
    def __init__(
        self,
        workspace: Path,
        sandbox: DockerSandbox,
        context_manager: Any | None = None,
        skill_catalog: SkillCatalog | None = None,
        memory_store: FileMemoryStore | NullMemory | None = None,
        llm: Any | None = None,
        model: str = "",
        skill_recall_k: int = 8,
    ):
        self.workspace = workspace.resolve()
        self.sandbox = sandbox
        self.context_manager = context_manager
        self.skill_catalog = skill_catalog or SkillCatalog.empty()
        self.memory_store = memory_store or NullMemory()
        self.llm = llm
        self.model = model
        self.skill_recall_k = max(0, skill_recall_k)
        self.security = ToolSecurityReviewer(self.workspace)
        self._tools: dict[str, ToolHandler] = {
            "run_shell": self._run_shell,
            "list_files": self._list_files,
            "read_file": self._read_file,
            "write_file": self._write_file,
            "run_tests": self._run_tests,
            "grep_files": self._grep_files,
            "read_context_artifact": self._read_context_artifact,
            "search_skills": self._search_skills,
            "load_skill": self._load_skill,
            "search_memory": self._search_memory,
            "load_memory": self._load_memory,
            "plan_subagents": self._plan_subagents,
            "run_subagents": self._run_subagents,
        }

    def set_context_manager(self, context_manager: Any) -> None:
        self.context_manager = context_manager

    def set_skill_catalog(self, skill_catalog: SkillCatalog) -> None:
        self.skill_catalog = skill_catalog

    def set_memory_store(self, memory_store: FileMemoryStore | NullMemory) -> None:
        self.memory_store = memory_store

    def names(self) -> list[str]:
        return sorted(self._tools)

    def describe(self) -> str:
        return "\n".join(
            [
                '- run_shell: {"command": "shell command to run in /workspace"}',
                '- list_files: {"path": ".", "max_depth": 2, "limit": 200}',
                '- read_file: {"path": "relative/path", "start_line": 1, "limit": 200}',
                '- write_file: {"path": "relative/path", "content": "new file content", "overwrite": false}',
                '- run_tests: {"command": "test command, default: python -m pytest"}',
                '- grep_files: {"pattern": "text or regex", "path": ".", "limit": 100, "case_sensitive": false}',
                '- read_context_artifact: {"artifact_id": "obs-0001", "start_line": 1, "limit": 200}',
                '- search_skills: {"query": "what you need help with", "limit": 5}',
                '- load_skill: {"name": "skill_name", "max_chars": 4000}',
                '- search_memory: {"query": "project fact, past lesson, or session detail", "limit": 5}',
                '- load_memory: {"memory_id": "memory-id", "max_chars": 4000}',
                '- plan_subagents: {"goal": "main goal", "tasks": [{"name": "short_name", "task": "bounded investigation", "context": "why this subtask matters", "allowed_tools": ["list_files","read_file","grep_files"], "path_scope": ["relative/path"], "max_steps": 4}], "max_parallel": 2}',
                '- run_subagents: {"tasks": [{"name": "short_name", "task": "bounded investigation", "context": "approved planning context", "allowed_tools": ["list_files","read_file","grep_files"], "path_scope": ["relative/path"], "max_steps": 4}]}',
                '- finish: {"answer": "concise final answer for the user"} inside args, e.g. {"action":"finish","args":{"answer":"..."}}',
            ]
        )

    def execute(self, name: str, args: dict[str, Any]) -> ToolResult:
        handler = self._tools.get(name)
        if handler is None:
            message = f"ERROR: unknown action {name!r}. Available tools: {', '.join(self.names())}"
            return ToolResult(
                False,
                message,
                stderr=message,
                exit_code=127,
                invalid_command=True,
            )
        review = self.security.review(name, args)
        if review.decision == Decision.DENY:
            message = f"ERROR: tool blocked by security review: {review.reason}"
            return ToolResult(
                False,
                message,
                stderr=message,
                exit_code=126 if review.dangerous else 2,
                permission_decision=review.decision.value,
                permission_reason=review.reason,
                dangerous_command=review.dangerous,
                invalid_command=review.invalid,
            )
        try:
            result = handler(args)
        except Exception as exc:
            message = f"ERROR: {exc}"
            return ToolResult(False, message, stderr=message, exit_code=1)
        if result.permission_decision == "not_applicable":
            return replace(
                result,
                permission_decision=review.decision.value,
                permission_reason=review.reason,
                dangerous_command=result.dangerous_command or review.dangerous,
            )
        return result

    def _run_shell(self, args: dict[str, Any]) -> ToolResult:
        command = str(args.get("command", "")).strip()
        if not command:
            message = "ERROR: run_shell requires args.command."
            return ToolResult(False, message, stderr=message, exit_code=2, invalid_command=True)
        return _sandbox_result_to_tool_result(self.sandbox.run(command))

    def _list_files(self, args: dict[str, Any]) -> ToolResult:
        root = self._resolve_workspace_path(str(args.get("path", ".")))
        max_depth = _as_int(args.get("max_depth", 2), default=2, minimum=0, maximum=20)
        limit = _as_int(args.get("limit", 200), default=200, minimum=1, maximum=1000)

        rows: list[str] = []
        root_depth = len(root.relative_to(self.workspace).parts)
        for current, dirs, files in os.walk(root):
            current_path = Path(current)
            rel_depth = len(current_path.relative_to(self.workspace).parts) - root_depth
            if rel_depth >= max_depth:
                dirs[:] = []
            dirs[:] = [name for name in sorted(dirs) if name not in {".git", ".minicode", "__pycache__"}]
            for filename in sorted(files):
                path = current_path / filename
                rows.append(path.relative_to(self.workspace).as_posix())
                if len(rows) >= limit:
                    output = "\n".join(rows)
                    return ToolResult(True, output, stdout=output, exit_code=0)
        output = "\n".join(rows) or "No files found."
        return ToolResult(True, output, stdout=output, exit_code=0)

    def _read_file(self, args: dict[str, Any]) -> ToolResult:
        path = self._resolve_workspace_path(str(args.get("path", "")))
        start_line = _as_int(args.get("start_line", 1), default=1, minimum=1, maximum=1_000_000)
        limit = _as_int(args.get("limit", 200), default=200, minimum=1, maximum=2000)
        if not path.is_file():
            message = f"ERROR: file not found: {path.relative_to(self.workspace).as_posix()}"
            return ToolResult(False, message, stderr=message, exit_code=1)

        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        selected = lines[start_line - 1 : start_line - 1 + limit]
        numbered = [f"{index}: {line}" for index, line in enumerate(selected, start=start_line)]
        output = "\n".join(numbered) or "File is empty or range has no lines."
        return ToolResult(True, output, stdout=output, exit_code=0)

    def _write_file(self, args: dict[str, Any]) -> ToolResult:
        path = self._resolve_workspace_path(str(args.get("path", "")))
        content = args.get("content")
        overwrite = bool(args.get("overwrite", False))
        if not isinstance(content, str):
            message = "ERROR: write_file requires string args.content."
            return ToolResult(False, message, stderr=message, exit_code=2, invalid_command=True)
        if path.exists() and not overwrite:
            message = "ERROR: file exists. Set overwrite=true to replace it."
            return ToolResult(False, message, stderr=message, exit_code=1)

        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        rel = path.relative_to(self.workspace).as_posix()
        output = f"Wrote {rel} ({len(content)} bytes)."
        return ToolResult(True, output, stdout=output, exit_code=0)

    def _run_tests(self, args: dict[str, Any]) -> ToolResult:
        command = str(args.get("command", "python -m pytest")).strip() or "python -m pytest"
        return _sandbox_result_to_tool_result(self.sandbox.run(command))

    def _grep_files(self, args: dict[str, Any]) -> ToolResult:
        pattern = str(args.get("pattern", "")).strip()
        if not pattern:
            message = "ERROR: grep_files requires args.pattern."
            return ToolResult(False, message, stderr=message, exit_code=2, invalid_command=True)
        root = self._resolve_workspace_path(str(args.get("path", ".")))
        limit = _as_int(args.get("limit", 100), default=100, minimum=1, maximum=1000)
        case_sensitive = bool(args.get("case_sensitive", False))
        flags = 0 if case_sensitive else re.IGNORECASE
        try:
            regex = re.compile(pattern, flags=flags)
        except re.error as exc:
            message = f"ERROR: invalid regex pattern: {exc}"
            return ToolResult(False, message, stderr=message, exit_code=2, invalid_command=True)

        files = [root] if root.is_file() else sorted(path for path in root.rglob("*") if path.is_file())
        rows: list[str] = []
        for path in files:
            if _should_skip_file(path, self.workspace):
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
                        output = "\n".join(rows)
                        return ToolResult(True, output, stdout=output, exit_code=0)
        output = "\n".join(rows) or "No matches found."
        return ToolResult(True, output, stdout=output, exit_code=0)

    def _read_context_artifact(self, args: dict[str, Any]) -> ToolResult:
        if self.context_manager is None:
            message = "ERROR: context artifact storage is not available."
            return ToolResult(False, message, stderr=message, exit_code=1)
        artifact_id = str(args.get("artifact_id", "")).strip()
        if not artifact_id:
            message = "ERROR: read_context_artifact requires args.artifact_id."
            return ToolResult(False, message, stderr=message, exit_code=2, invalid_command=True)
        start_line = _as_int(args.get("start_line", 1), default=1, minimum=1, maximum=1_000_000)
        limit = _as_int(args.get("limit", 200), default=200, minimum=1, maximum=1000)
        try:
            output = self.context_manager.read_artifact(artifact_id, start_line=start_line, limit=limit)
        except Exception as exc:
            message = f"ERROR: {exc}"
            return ToolResult(False, message, stderr=message, exit_code=1)
        return ToolResult(True, output, stdout=output, exit_code=0)

    def _search_skills(self, args: dict[str, Any]) -> ToolResult:
        query = str(args.get("query", "")).strip()
        limit = _as_int(args.get("limit", 5), default=5, minimum=1, maximum=20)
        if not query:
            message = "ERROR: search_skills requires args.query."
            return ToolResult(False, message, stderr=message, exit_code=2, invalid_command=True)
        retrieval = SkillToolRetriever(
            self.skill_catalog,
            llm=self.llm,
            model=self.model,
            recall_k=self.skill_recall_k,
        ).retrieve(query, limit=limit)
        selected = retrieval.selected
        if not selected:
            output = "No matching skills found."
            return ToolResult(
                True,
                output,
                stdout=output,
                exit_code=0,
                retrieval_trace=retrieval.trace.to_log_dict(),
            )
        rows = ["Matching skills:"]
        for item in selected:
            skill = item.skill
            rows.append(
                "\n".join(
                    [
                        f"- name: {skill.name}",
                        f"  score: {item.score}",
                        f"  reason: {item.reason}",
                        f"  recall_score: {item.recall_score}",
                        f"  description: {skill.description}",
                        f"  tags: {', '.join(skill.tags) or 'none'}",
                        f"  recommended_tools: {', '.join(skill.tools) or 'none'}",
                    ]
                )
            )
        rows.append("Use load_skill with the selected name to inject the full skill workflow.")
        output = "\n".join(rows)
        return ToolResult(
            True,
            output,
            stdout=output,
            exit_code=0,
            retrieval_trace=retrieval.trace.to_log_dict(),
        )

    def _load_skill(self, args: dict[str, Any]) -> ToolResult:
        name = str(args.get("name", "")).strip()
        max_chars = _as_int(args.get("max_chars", 4000), default=4000, minimum=200, maximum=12000)
        if not name:
            message = "ERROR: load_skill requires args.name."
            return ToolResult(False, message, stderr=message, exit_code=2, invalid_command=True)
        skill = self.skill_catalog.get(name)
        if skill is None:
            available = ", ".join(self.skill_catalog.names()) or "none"
            message = f"ERROR: unknown skill {name!r}. Available skills: {available}"
            return ToolResult(False, message, stderr=message, exit_code=1)
        body = skill.body
        truncated = False
        if len(body) > max_chars:
            body = body[: max_chars - 3] + "..."
            truncated = True
        output = "\n".join(
            [
                f"Dynamic skill loaded: {skill.name}",
                f"Description: {skill.description}",
                f"Tags: {', '.join(skill.tags) or 'none'}",
                f"Intents: {', '.join(skill.intents) or 'none'}",
                f"Recommended tools: {', '.join(skill.tools) or 'none'}",
                f"Source: {skill.source_path}",
                f"Truncated: {str(truncated).lower()}",
                "",
                body,
            ]
        )
        return ToolResult(True, output, stdout=output, exit_code=0)

    def _search_memory(self, args: dict[str, Any]) -> ToolResult:
        query = str(args.get("query", "")).strip()
        limit = _as_int(args.get("limit", 5), default=5, minimum=1, maximum=20)
        if not query:
            message = "ERROR: search_memory requires args.query."
            return ToolResult(False, message, stderr=message, exit_code=2, invalid_command=True)
        retrieval = MemoryToolRetriever(self.memory_store, llm=self.llm, model=self.model).retrieve(query, limit=limit)
        results = retrieval.results
        if not results:
            output = "No matching memories found."
            return ToolResult(
                True,
                output,
                stdout=output,
                exit_code=0,
                retrieval_trace=retrieval.trace.to_log_dict(),
            )
        rows = ["Matching memories:"]
        for result in results:
            item = result.item
            preview = _compact_preview(item.body, limit=300)
            rows.append(
                "\n".join(
                    [
                        f"- memory_id: {item.memory_id}",
                        f"  score: {result.score}",
                        f"  reason: {result.reason}",
                        f"  type: {item.memory_type}",
                        f"  title: {item.title}",
                        f"  tags: {', '.join(item.tags) or 'none'}",
                        f"  preview: {preview}",
                    ]
                )
            )
        rows.append("Use load_memory with the selected memory_id to inject the full memory.")
        output = "\n".join(rows)
        return ToolResult(
            True,
            output,
            stdout=output,
            exit_code=0,
            retrieval_trace=retrieval.trace.to_log_dict(),
        )

    def _load_memory(self, args: dict[str, Any]) -> ToolResult:
        memory_id = str(args.get("memory_id", "")).strip()
        max_chars = _as_int(args.get("max_chars", 4000), default=4000, minimum=200, maximum=12000)
        if not memory_id:
            message = "ERROR: load_memory requires args.memory_id."
            return ToolResult(False, message, stderr=message, exit_code=2, invalid_command=True)
        item = self.memory_store.get(memory_id)
        if item is None:
            available = ", ".join(memory.memory_id for memory in self.memory_store.all()) or "none"
            message = f"ERROR: unknown memory {memory_id!r}. Available memories: {available}"
            return ToolResult(False, message, stderr=message, exit_code=1)
        self.memory_store.record_use(memory_id)
        body = item.body
        truncated = False
        if len(body) > max_chars:
            body = body[: max_chars - 3] + "..."
            truncated = True
        output = "\n".join(
            [
                f"Dynamic memory loaded: {item.memory_id}",
                f"Type: {item.memory_type}",
                f"Title: {item.title}",
                f"Tags: {', '.join(item.tags) or 'none'}",
                f"Source: {item.source_path}",
                f"Truncated: {str(truncated).lower()}",
                "",
                body,
            ]
        )
        return ToolResult(True, output, stdout=output, exit_code=0)

    def _plan_subagents(self, args: dict[str, Any]) -> ToolResult:
        tasks, error = parse_subagent_tasks(args)
        if error:
            return ToolResult(False, f"ERROR: {error}", stderr=f"ERROR: {error}", exit_code=2, invalid_command=True)
        max_parallel = _as_int(args.get("max_parallel", 4), default=4, minimum=1, maximum=6)
        approved_tasks = [
            {
                "name": task.name,
                "task": task.task,
                "context": task.context,
                "allowed_tools": task.allowed_tools,
                "path_scope": task.path_scope,
                "max_steps": task.max_steps,
            }
            for task in tasks
        ]
        payload = {
            "status": "approved",
            "goal": str(args.get("goal") or ""),
            "approved_tasks": approved_tasks,
            "max_parallel": min(max_parallel, len(approved_tasks)),
            "next_action": {
                "action": "run_subagents",
                "args": {
                    "tasks": approved_tasks,
                    "max_parallel": min(max_parallel, len(approved_tasks)),
                },
            },
            "instruction": "Call run_subagents next with exactly approved_tasks unless you need to revise the plan.",
        }
        output = json.dumps(payload, indent=2, ensure_ascii=False)
        return ToolResult(True, output, stdout=output, exit_code=0)

    def _run_subagents(self, args: dict[str, Any]) -> ToolResult:
        if self.llm is None or not self.model:
            message = "ERROR: run_subagents requires an LLM client and model."
            return ToolResult(False, message, stderr=message, exit_code=1)
        tasks, error = parse_subagent_tasks(args)
        if error:
            return ToolResult(False, f"ERROR: {error}", stderr=f"ERROR: {error}", exit_code=2, invalid_command=True)
        max_parallel = _as_int(args.get("max_parallel", 4), default=4, minimum=1, maximum=6)
        batch = SubAgentRunner(
            llm=self.llm,
            model=self.model,
            tools=self,
            max_parallel=max_parallel,
        ).run_many(tasks)
        output = batch.to_observation_text()
        return ToolResult(
            batch.status == "completed",
            output,
            stdout=output,
            exit_code=0 if batch.status in {"completed", "partial"} else 1,
            duration_ms=batch.duration_ms,
            subagent_trace=batch.to_log_dict(),
        )

    def _resolve_workspace_path(self, raw_path: str) -> Path:
        if not raw_path:
            raise ValueError("path is required")
        candidate = (self.workspace / raw_path).resolve()
        if candidate != self.workspace and self.workspace not in candidate.parents:
            raise ValueError(f"path escapes workspace: {raw_path}")
        return candidate


def _sandbox_result_to_tool_result(result: SandboxResult) -> ToolResult:
    output = f"exit_code={result.exit_code}\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    return ToolResult(
        result.exit_code == 0,
        output,
        stdout=result.stdout,
        stderr=result.stderr,
        exit_code=result.exit_code,
        permission_decision=result.permission_decision,
        permission_reason=result.permission_reason,
        dangerous_command=result.dangerous_command,
        duration_ms=result.duration_ms,
    )


def _as_int(value: Any, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, min(maximum, parsed))


def _compact_preview(value: str, limit: int = 300) -> str:
    text = " ".join(value.split())
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."


def _should_skip_file(path: Path, workspace: Path) -> bool:
    try:
        rel_parts = path.relative_to(workspace).parts
    except ValueError:
        return True
    if any(part in {".git", ".minicode", "__pycache__", ".pytest_cache", ".mypy_cache"} for part in rel_parts):
        return True
    try:
        return path.stat().st_size > 1_000_000
    except OSError:
        return True
