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


ToolHandler = Callable[[dict[str, Any]], ToolResult]


class ToolRegistry:
    def __init__(self, workspace: Path, sandbox: DockerSandbox):
        self.workspace = workspace.resolve()
        self.sandbox = sandbox
        self._tools: dict[str, ToolHandler] = {
            "run_shell": self._run_shell,
            "list_files": self._list_files,
            "read_file": self._read_file,
            "write_file": self._write_file,
            "run_tests": self._run_tests,
        }

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
                '- finish: {"answer": "concise final answer for the user"}',
            ]
        )

    def execute(self, name: str, args: dict[str, Any]) -> ToolResult:
        handler = self._tools.get(name)
        if handler is None:
            return ToolResult(False, f"ERROR: unknown action {name!r}. Available tools: {', '.join(self.names())}")
        try:
            return handler(args)
        except Exception as exc:
            return ToolResult(False, f"ERROR: {exc}")

    def _run_shell(self, args: dict[str, Any]) -> ToolResult:
        command = str(args.get("command", "")).strip()
        if not command:
            return ToolResult(False, "ERROR: run_shell requires args.command.")
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
                    return ToolResult(True, "\n".join(rows))
        return ToolResult(True, "\n".join(rows) or "No files found.")

    def _read_file(self, args: dict[str, Any]) -> ToolResult:
        path = self._resolve_workspace_path(str(args.get("path", "")))
        start_line = _as_int(args.get("start_line", 1), default=1, minimum=1, maximum=1_000_000)
        limit = _as_int(args.get("limit", 200), default=200, minimum=1, maximum=2000)
        if not path.is_file():
            return ToolResult(False, f"ERROR: file not found: {path.relative_to(self.workspace).as_posix()}")

        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        selected = lines[start_line - 1 : start_line - 1 + limit]
        numbered = [f"{index}: {line}" for index, line in enumerate(selected, start=start_line)]
        return ToolResult(True, "\n".join(numbered) or "File is empty or range has no lines.")

    def _write_file(self, args: dict[str, Any]) -> ToolResult:
        path = self._resolve_workspace_path(str(args.get("path", "")))
        content = args.get("content")
        overwrite = bool(args.get("overwrite", False))
        if not isinstance(content, str):
            return ToolResult(False, "ERROR: write_file requires string args.content.")
        if path.exists() and not overwrite:
            return ToolResult(False, "ERROR: file exists. Set overwrite=true to replace it.")

        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        rel = path.relative_to(self.workspace).as_posix()
        return ToolResult(True, f"Wrote {rel} ({len(content)} bytes).")

    def _run_tests(self, args: dict[str, Any]) -> ToolResult:
        command = str(args.get("command", "python -m pytest")).strip() or "python -m pytest"
        return _sandbox_result_to_tool_result(self.sandbox.run(command))

    def _resolve_workspace_path(self, raw_path: str) -> Path:
        if not raw_path:
            raise ValueError("path is required")
        candidate = (self.workspace / raw_path).resolve()
        if candidate != self.workspace and self.workspace not in candidate.parents:
            raise ValueError(f"path escapes workspace: {raw_path}")
        return candidate


def _sandbox_result_to_tool_result(result: SandboxResult) -> ToolResult:
    output = f"exit_code={result.exit_code}\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    return ToolResult(result.exit_code == 0, output)


def _as_int(value: Any, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, min(maximum, parsed))
