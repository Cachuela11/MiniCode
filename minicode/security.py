from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .permissions import Decision


@dataclass(frozen=True)
class ToolRisk:
    name: str
    category: str
    mutates_workspace: bool = False
    sandboxed: bool = False
    risk: str = "low"


@dataclass(frozen=True)
class SecurityReviewResult:
    decision: Decision
    reason: str
    tool_name: str
    risk: str = "low"
    dangerous: bool = False
    invalid: bool = False


class ToolSecurityReviewer:
    """Pre-execution review for model-generated tool actions."""

    def __init__(self, workspace: Path):
        self.workspace = workspace.resolve()
        self.risks = {
            "list_files": ToolRisk("list_files", "file_read", risk="low"),
            "read_file": ToolRisk("read_file", "file_read", risk="medium"),
            "write_file": ToolRisk("write_file", "file_write", mutates_workspace=True, risk="high"),
            "run_shell": ToolRisk("run_shell", "shell", sandboxed=True, risk="high"),
            "run_tests": ToolRisk("run_tests", "test", sandboxed=True, risk="medium"),
            "grep_files": ToolRisk("grep_files", "file_read", risk="medium"),
            "read_context_artifact": ToolRisk("read_context_artifact", "context_read", risk="low"),
            "search_skills": ToolRisk("search_skills", "retrieval", risk="low"),
            "load_skill": ToolRisk("load_skill", "retrieval", risk="low"),
            "search_memory": ToolRisk("search_memory", "retrieval", risk="low"),
            "load_memory": ToolRisk("load_memory", "retrieval", risk="low"),
            "run_subagents": ToolRisk("run_subagents", "subagent", risk="medium"),
        }

    def review(self, tool_name: str, args: Any) -> SecurityReviewResult:
        risk = self.risks.get(tool_name, ToolRisk(tool_name, "unknown", risk="unknown"))
        if not isinstance(args, dict):
            return self._deny(tool_name, risk, "tool args must be a JSON object", invalid=True)

        if tool_name == "list_files":
            return self._review_workspace_path(tool_name, risk, args, path_key="path", default_path=".")
        if tool_name == "read_file":
            return self._review_workspace_path(
                tool_name,
                risk,
                args,
                path_key="path",
                require_file_path=True,
                deny_secret=True,
            )
        if tool_name == "write_file":
            return self._review_write_file(tool_name, risk, args)
        if tool_name in {"run_shell", "run_tests"}:
            return self._review_command(tool_name, risk, args)
        if tool_name == "grep_files":
            path_review = self._review_workspace_path(tool_name, risk, args, path_key="path", default_path=".")
            if path_review.decision != Decision.ALLOW:
                return path_review
            pattern = args.get("pattern")
            if not isinstance(pattern, str) or not pattern.strip():
                return self._deny(tool_name, risk, "grep_files requires non-empty string args.pattern", invalid=True)
            return self._allow(tool_name, risk, "security review passed")
        if tool_name == "read_context_artifact":
            return self._review_context_artifact(tool_name, risk, args)
        if tool_name in {"search_skills", "search_memory"}:
            return self._review_query_tool(tool_name, risk, args)
        if tool_name in {"load_skill", "load_memory"}:
            return self._review_load_tool(tool_name, risk, args)
        if tool_name == "run_subagents":
            return self._review_subagents(tool_name, risk, args)
        return self._allow(tool_name, risk, "security review passed")

    def _review_workspace_path(
        self,
        tool_name: str,
        risk: ToolRisk,
        args: dict[str, Any],
        *,
        path_key: str,
        default_path: str = "",
        require_file_path: bool = False,
        deny_secret: bool = False,
    ) -> SecurityReviewResult:
        raw_path = args.get(path_key, default_path)
        if not isinstance(raw_path, str) or not raw_path.strip():
            return self._deny(tool_name, risk, f"{tool_name} requires string args.{path_key}", invalid=True)
        resolved = self._resolve_workspace_path(raw_path)
        if resolved is None:
            return self._deny(tool_name, risk, f"path escapes workspace: {raw_path}", dangerous=True, invalid=True)
        rel_path = resolved.relative_to(self.workspace).as_posix() if resolved != self.workspace else "."
        if _is_protected_path(rel_path):
            return self._deny(tool_name, risk, f"protected path is blocked: {rel_path}", dangerous=True)
        if deny_secret and _is_secret_path(rel_path):
            return self._deny(tool_name, risk, f"secret-like file is blocked: {rel_path}", dangerous=True)
        if require_file_path and resolved == self.workspace:
            return self._deny(tool_name, risk, f"{tool_name} requires a file path", invalid=True)
        return self._allow(tool_name, risk, "security review passed")

    def _review_write_file(self, tool_name: str, risk: ToolRisk, args: dict[str, Any]) -> SecurityReviewResult:
        path_review = self._review_workspace_path(
            tool_name,
            risk,
            args,
            path_key="path",
            require_file_path=True,
            deny_secret=True,
        )
        if path_review.decision != Decision.ALLOW:
            return path_review
        if not isinstance(args.get("content"), str):
            return self._deny(tool_name, risk, "write_file requires string args.content", invalid=True)
        if "overwrite" in args and not isinstance(args.get("overwrite"), bool):
            return self._deny(tool_name, risk, "write_file args.overwrite must be boolean", invalid=True)
        return self._allow(tool_name, risk, "security review passed")

    def _review_command(self, tool_name: str, risk: ToolRisk, args: dict[str, Any]) -> SecurityReviewResult:
        command = str(args.get("command", "")).strip()
        if not command:
            return self._deny(tool_name, risk, f"{tool_name} requires non-empty args.command", invalid=True)
        if _reads_secret_in_shell(command):
            return self._deny(tool_name, risk, "shell command appears to read secret-like files", dangerous=True)
        return self._allow(tool_name, risk, "security review passed")

    def _review_context_artifact(self, tool_name: str, risk: ToolRisk, args: dict[str, Any]) -> SecurityReviewResult:
        artifact_id = args.get("artifact_id")
        if not isinstance(artifact_id, str) or not re.fullmatch(r"obs-\d{4,}", artifact_id.strip()):
            return self._deny(tool_name, risk, "read_context_artifact requires a valid artifact_id", invalid=True)
        return self._allow(tool_name, risk, "security review passed")

    def _review_query_tool(self, tool_name: str, risk: ToolRisk, args: dict[str, Any]) -> SecurityReviewResult:
        query = args.get("query")
        if not isinstance(query, str) or not query.strip():
            return self._deny(tool_name, risk, f"{tool_name} requires non-empty string args.query", invalid=True)
        return self._allow(tool_name, risk, "security review passed")

    def _review_load_tool(self, tool_name: str, risk: ToolRisk, args: dict[str, Any]) -> SecurityReviewResult:
        key = "name" if tool_name == "load_skill" else "memory_id"
        value = args.get(key)
        if not isinstance(value, str) or not value.strip():
            return self._deny(tool_name, risk, f"{tool_name} requires non-empty string args.{key}", invalid=True)
        return self._allow(tool_name, risk, "security review passed")

    def _review_subagents(self, tool_name: str, risk: ToolRisk, args: dict[str, Any]) -> SecurityReviewResult:
        tasks = args.get("tasks")
        if not isinstance(tasks, list) or not tasks:
            return self._deny(tool_name, risk, "run_subagents requires non-empty args.tasks list", invalid=True)
        if len(tasks) > 6:
            return self._deny(tool_name, risk, "run_subagents supports at most 6 tasks", invalid=True)
        for index, task in enumerate(tasks, start=1):
            if not isinstance(task, dict):
                return self._deny(tool_name, risk, f"subagent task #{index} must be an object", invalid=True)
            task_text = task.get("task")
            if not isinstance(task_text, str) or not task_text.strip():
                return self._deny(tool_name, risk, f"subagent task #{index} requires string task", invalid=True)
            for scope in task.get("path_scope") or ["."]:
                if not isinstance(scope, str) or self._resolve_workspace_path(scope) is None:
                    return self._deny(
                        tool_name,
                        risk,
                        f"subagent task #{index} path_scope escapes workspace: {scope}",
                        dangerous=True,
                        invalid=True,
                    )
            for allowed_tool in task.get("allowed_tools") or []:
                if allowed_tool in {"write_file", "run_shell", "run_tests", "run_subagents"}:
                    return self._deny(
                        tool_name,
                        risk,
                        f"subagent task #{index} requests forbidden tool: {allowed_tool}",
                        dangerous=True,
                    )
        return self._allow(tool_name, risk, "security review passed")

    def _resolve_workspace_path(self, raw_path: str) -> Path | None:
        candidate = (self.workspace / raw_path).resolve()
        if candidate == self.workspace or self.workspace in candidate.parents:
            return candidate
        return None

    def _allow(self, tool_name: str, risk: ToolRisk, reason: str) -> SecurityReviewResult:
        return SecurityReviewResult(Decision.ALLOW, reason, tool_name=tool_name, risk=risk.risk)

    def _deny(
        self,
        tool_name: str,
        risk: ToolRisk,
        reason: str,
        *,
        dangerous: bool = False,
        invalid: bool = False,
    ) -> SecurityReviewResult:
        return SecurityReviewResult(
            Decision.DENY,
            reason,
            tool_name=tool_name,
            risk=risk.risk,
            dangerous=dangerous,
            invalid=invalid,
        )


def _is_protected_path(rel_path: str) -> bool:
    parts = [part for part in rel_path.replace("\\", "/").split("/") if part and part != "."]
    return bool(parts and parts[0] == ".git")


def _is_secret_path(rel_path: str) -> bool:
    path = rel_path.replace("\\", "/").lower()
    name = path.rsplit("/", 1)[-1]
    if name == ".env" or (name.startswith(".env.") and name != ".env.example"):
        return True
    if name in {"id_rsa", "id_dsa", "id_ecdsa", "id_ed25519"}:
        return True
    return name.endswith((".pem", ".key", ".p12", ".pfx"))


def _reads_secret_in_shell(command: str) -> bool:
    normalized = " ".join(command.split()).lower()
    if not re.search(r"\b(cat|type|less|more|head|tail|sed|awk|grep)\b", normalized):
        return False
    return any(
        pattern.search(normalized)
        for pattern in [
            re.compile(r"(^|[\s/])\.env($|[\s./])"),
            re.compile(r"\bid_(rsa|dsa|ecdsa|ed25519)\b"),
            re.compile(r"\.(pem|key|p12|pfx)\b"),
        ]
    )
