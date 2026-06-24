from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .sandbox import DockerSandbox, SandboxResult


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


ToolHandler = Callable[[dict[str, Any]], ToolResult]


class ToolRegistry:
    def __init__(self, workspace: Path, sandbox: DockerSandbox, context_manager: Any | None = None):
        self.workspace = workspace.resolve()
        self.sandbox = sandbox
        self.context_manager = context_manager
        self._tools: dict[str, ToolHandler] = {
            "run_shell": self._run_shell,
            "list_files": self._list_files,
            "read_file": self._read_file,
            "write_file": self._write_file,
            "run_tests": self._run_tests,
            "read_context_artifact": self._read_context_artifact,
        }

    def set_context_manager(self, context_manager: Any) -> None:
        self.context_manager = context_manager

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
                '- read_context_artifact: {"artifact_id": "obs-0001", "start_line": 1, "limit": 200}',
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
        try:
            return handler(args)
        except Exception as exc:
            message = f"ERROR: {exc}"
            return ToolResult(False, message, stderr=message, exit_code=1)

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
