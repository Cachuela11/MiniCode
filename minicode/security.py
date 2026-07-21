from __future__ import annotations

import re
import ipaddress
import urllib.parse
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .permissions import Decision
from .subagents import (
    MAX_EDGE_TRAVERSALS,
    MAX_STAGE_EDGES,
    MAX_STAGE_NODES,
    MAX_STAGE_WORKFLOW_ITERATIONS,
    MAX_WORKFLOW_STAGES,
)


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
            "edit_file": ToolRisk("edit_file", "file_edit", mutates_workspace=True, risk="high"),
            "write_file": ToolRisk("write_file", "file_write", mutates_workspace=True, risk="high"),
            "run_shell": ToolRisk("run_shell", "shell", sandboxed=True, risk="high"),
            "run_tests": ToolRisk("run_tests", "test", sandboxed=True, risk="medium"),
            "glob_files": ToolRisk("glob_files", "file_read", risk="low"),
            "grep_files": ToolRisk("grep_files", "file_read", risk="medium"),
            "todo_write": ToolRisk("todo_write", "planning", risk="low"),
            "web_fetch": ToolRisk("web_fetch", "network", risk="medium"),
            "inspect_diagnostics": ToolRisk("inspect_diagnostics", "diagnostics", risk="medium"),
            "read_context_artifact": ToolRisk("read_context_artifact", "context_read", risk="low"),
            "search_skills": ToolRisk("search_skills", "retrieval", risk="low"),
            "load_skill": ToolRisk("load_skill", "retrieval", risk="low"),
            "search_memory": ToolRisk("search_memory", "retrieval", risk="low"),
            "load_memory": ToolRisk("load_memory", "retrieval", risk="low"),
            "plan_subagents": ToolRisk("plan_subagents", "subagent_plan", risk="medium"),
            "run_subagents": ToolRisk("run_subagents", "subagent", risk="medium"),
            "plan_subagent_workflow": ToolRisk("plan_subagent_workflow", "subagent_workflow_plan", risk="medium"),
            "run_subagent_workflow": ToolRisk("run_subagent_workflow", "subagent_workflow", risk="medium"),
            "search_tools": ToolRisk("search_tools", "tool_discovery", risk="low"),
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
        if tool_name == "edit_file":
            return self._review_edit_file(tool_name, risk, args)
        if tool_name == "write_file":
            return self._review_write_file(tool_name, risk, args)
        if tool_name in {"run_shell", "run_tests"}:
            return self._review_command(tool_name, risk, args)
        if tool_name == "glob_files":
            path_review = self._review_workspace_path(tool_name, risk, args, path_key="path", default_path=".")
            if path_review.decision != Decision.ALLOW:
                return path_review
            pattern = args.get("pattern")
            if not isinstance(pattern, str) or not pattern.strip():
                return self._deny(tool_name, risk, "glob_files requires non-empty string args.pattern", invalid=True)
            return self._allow(tool_name, risk, "security review passed")
        if tool_name == "grep_files":
            path_review = self._review_workspace_path(tool_name, risk, args, path_key="path", default_path=".")
            if path_review.decision != Decision.ALLOW:
                return path_review
            pattern = args.get("pattern")
            if not isinstance(pattern, str) or not pattern.strip():
                return self._deny(tool_name, risk, "grep_files requires non-empty string args.pattern", invalid=True)
            return self._allow(tool_name, risk, "security review passed")
        if tool_name == "todo_write":
            return self._review_todo_write(tool_name, risk, args)
        if tool_name == "web_fetch":
            return self._review_web_fetch(tool_name, risk, args)
        if tool_name == "inspect_diagnostics":
            return self._review_workspace_path(tool_name, risk, args, path_key="path", default_path=".")
        if tool_name == "read_context_artifact":
            return self._review_context_artifact(tool_name, risk, args)
        if tool_name in {"search_tools", "search_skills", "search_memory"}:
            return self._review_query_tool(tool_name, risk, args)
        if tool_name in {"load_skill", "load_memory"}:
            return self._review_load_tool(tool_name, risk, args)
        if tool_name in {"plan_subagents", "run_subagents"}:
            return self._review_subagents(tool_name, risk, args)
        if tool_name in {"plan_subagent_workflow", "run_subagent_workflow"}:
            return self._review_subagent_workflow(tool_name, risk, args)
        return self._allow(tool_name, risk, "security review passed")

    def _review_edit_file(self, tool_name: str, risk: ToolRisk, args: dict[str, Any]) -> SecurityReviewResult:
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
        if not isinstance(args.get("old_text"), str) or not args.get("old_text"):
            return self._deny(tool_name, risk, "edit_file requires non-empty string args.old_text", invalid=True)
        if not isinstance(args.get("new_text"), str):
            return self._deny(tool_name, risk, "edit_file requires string args.new_text", invalid=True)
        if "replace_all" in args and not isinstance(args.get("replace_all"), bool):
            return self._deny(tool_name, risk, "edit_file args.replace_all must be boolean", invalid=True)
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

    def _review_subagent_workflow(self, tool_name: str, risk: ToolRisk, args: dict[str, Any]) -> SecurityReviewResult:
        stages = args.get("stages")
        if not isinstance(stages, list) or not stages:
            return self._deny(tool_name, risk, f"{tool_name} requires non-empty args.stages list", invalid=True)
        if len(stages) > MAX_WORKFLOW_STAGES:
            return self._deny(
                tool_name,
                risk,
                f"subagent workflow supports at most {MAX_WORKFLOW_STAGES} stages",
                invalid=True,
            )
        for stage_index, stage in enumerate(stages, start=1):
            if not isinstance(stage, dict):
                return self._deny(tool_name, risk, f"workflow stage #{stage_index} must be an object", invalid=True)
            nodes = stage.get("nodes")
            if nodes is None:
                nodes = stage.get("tasks")
            review = self._review_subagents(tool_name, risk, {"tasks": nodes})
            if review.decision != Decision.ALLOW:
                return self._deny(
                    tool_name,
                    risk,
                    f"workflow stage #{stage_index}: {review.reason}",
                    dangerous=review.dangerous,
                    invalid=review.invalid,
                )
            graph_review = self._review_stage_control_graph(tool_name, risk, stage, nodes, stage_index)
            if graph_review.decision != Decision.ALLOW:
                return graph_review
        return self._allow(tool_name, risk, "security review passed")

    def _review_stage_control_graph(
        self,
        tool_name: str,
        risk: ToolRisk,
        stage: dict[str, Any],
        nodes: Any,
        stage_index: int,
    ) -> SecurityReviewResult:
        raw_edges = stage.get("edges")
        if raw_edges is None:
            raw_edges = stage.get("control_edges")
        if raw_edges is None:
            return self._allow(tool_name, risk, "security review passed")
        if not isinstance(raw_edges, list):
            return self._deny(tool_name, risk, f"workflow stage #{stage_index}: edges must be a list", invalid=True)
        if len(raw_edges) > MAX_STAGE_EDGES:
            return self._deny(
                tool_name,
                risk,
                f"workflow stage #{stage_index}: supports at most {MAX_STAGE_EDGES} edges",
                invalid=True,
            )
        max_iterations = stage.get("max_iterations", 1)
        if max_iterations is not None:
            try:
                parsed_iterations = int(max_iterations)
            except (TypeError, ValueError):
                return self._deny(
                    tool_name,
                    risk,
                    f"workflow stage #{stage_index}: max_iterations must be an integer",
                    invalid=True,
                )
            if parsed_iterations < 1 or parsed_iterations > MAX_STAGE_WORKFLOW_ITERATIONS:
                return self._deny(
                    tool_name,
                    risk,
                    f"workflow stage #{stage_index}: max_iterations must be 1-{MAX_STAGE_WORKFLOW_ITERATIONS}",
                    invalid=True,
                )
        node_names = {_security_slugify(str(node.get("name") or f"subagent_{index}")) for index, node in enumerate(nodes, start=1)}
        for edge_index, edge in enumerate(raw_edges, start=1):
            if not isinstance(edge, dict):
                return self._deny(tool_name, risk, f"workflow stage #{stage_index}: edge #{edge_index} must be an object", invalid=True)
            from_raw = edge.get("from") or edge.get("from_node")
            to_raw = edge.get("to") or edge.get("to_node")
            if not isinstance(from_raw, str) or not from_raw.strip() or not isinstance(to_raw, str) or not to_raw.strip():
                return self._deny(
                    tool_name,
                    risk,
                    f"workflow stage #{stage_index}: edge #{edge_index} requires string from and to",
                    invalid=True,
                )
            from_node = _security_slugify(from_raw)
            to_node = _security_slugify(to_raw)
            if from_node not in node_names or to_node not in node_names:
                return self._deny(
                    tool_name,
                    risk,
                    f"workflow stage #{stage_index}: edge #{edge_index} references unknown node",
                    invalid=True,
                )
            max_traversals = edge.get("max_traversals", 1)
            try:
                parsed_traversals = int(max_traversals)
            except (TypeError, ValueError):
                return self._deny(
                    tool_name,
                    risk,
                    f"workflow stage #{stage_index}: edge #{edge_index} max_traversals must be an integer",
                    invalid=True,
                )
            if parsed_traversals < 1 or parsed_traversals > MAX_EDGE_TRAVERSALS:
                return self._deny(
                    tool_name,
                    risk,
                    f"workflow stage #{stage_index}: edge #{edge_index} max_traversals must be 1-{MAX_EDGE_TRAVERSALS}",
                    invalid=True,
                )
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

    def _review_todo_write(self, tool_name: str, risk: ToolRisk, args: dict[str, Any]) -> SecurityReviewResult:
        todos = args.get("todos")
        if not isinstance(todos, list):
            return self._deny(tool_name, risk, "todo_write requires args.todos list", invalid=True)
        if len(todos) > 20:
            return self._deny(tool_name, risk, "todo_write supports at most 20 todos", invalid=True)
        for index, todo in enumerate(todos, start=1):
            if not isinstance(todo, dict):
                return self._deny(tool_name, risk, f"todo #{index} must be an object", invalid=True)
            if not isinstance(todo.get("content"), str) or not todo.get("content", "").strip():
                return self._deny(tool_name, risk, f"todo #{index} requires non-empty content", invalid=True)
            if todo.get("status", "pending") not in {"pending", "in_progress", "completed"}:
                return self._deny(tool_name, risk, f"todo #{index} has invalid status", invalid=True)
        return self._allow(tool_name, risk, "security review passed")

    def _review_web_fetch(self, tool_name: str, risk: ToolRisk, args: dict[str, Any]) -> SecurityReviewResult:
        url = args.get("url")
        if not isinstance(url, str) or not url.strip():
            return self._deny(tool_name, risk, "web_fetch requires non-empty string args.url", invalid=True)
        parsed = urllib_parse(url)
        if parsed is None:
            return self._deny(tool_name, risk, "web_fetch requires a valid URL", invalid=True)
        if parsed.scheme not in {"http", "https"}:
            return self._deny(tool_name, risk, "web_fetch allows only http or https URLs", dangerous=True, invalid=True)
        host = (parsed.hostname or "").lower()
        if _is_blocked_fetch_host(host):
            return self._deny(tool_name, risk, f"web_fetch blocks local or private host: {host}", dangerous=True)
        if "max_chars" in args:
            try:
                max_chars = int(args["max_chars"])
            except (TypeError, ValueError):
                return self._deny(tool_name, risk, "web_fetch args.max_chars must be an integer", invalid=True)
            if max_chars < 200 or max_chars > 20000:
                return self._deny(tool_name, risk, "web_fetch args.max_chars must be 200-20000", invalid=True)
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
        if len(tasks) > MAX_STAGE_NODES:
            return self._deny(
                tool_name,
                risk,
                f"run_subagents supports at most {MAX_STAGE_NODES} tasks",
                invalid=True,
            )
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
                if allowed_tool in {
                    "write_file",
                    "run_shell",
                    "run_tests",
                    "plan_subagents",
                    "run_subagents",
                    "plan_subagent_workflow",
                    "run_subagent_workflow",
                }:
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


def urllib_parse(url: str) -> urllib.parse.ParseResult | None:
    try:
        parsed = urllib.parse.urlparse(url)
    except ValueError:
        return None
    if not parsed.scheme or not parsed.netloc or not parsed.hostname:
        return None
    return parsed


def _is_blocked_fetch_host(host: str) -> bool:
    if not host:
        return True
    if host in {"localhost", "localhost.localdomain"} or host.endswith(".localhost"):
        return True
    try:
        ip = ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        return False
    return ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_reserved


def _security_slugify(value: str) -> str:
    slug = re.sub(r"[^0-9A-Za-z_.-]+", "_", value.strip()).strip("_.-").lower()
    return slug or "subagent"
