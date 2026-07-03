from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol

from .observability import TokenUsage
from .skills import SkillCatalog


@dataclass(frozen=True)
class SkillEvolutionConfig:
    run_log_path: str = ".minicode/runs"
    skills_dir: str = ".skills"
    max_runs: int = 20
    min_tool_steps: int = 3
    drafts_dirname: str = "_drafts"


@dataclass(frozen=True)
class SkillDraftWrite:
    path: str
    operation: str
    target_skill: str
    name: str


@dataclass
class SkillEvolutionResult:
    status: str
    inspected_runs: int = 0
    eligible_runs: int = 0
    drafts: list[SkillDraftWrite] = field(default_factory=list)
    skipped: list[dict[str, Any]] = field(default_factory=list)
    token_usage: TokenUsage = field(default_factory=TokenUsage)
    error: str = ""

    def to_log_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["token_usage"] = asdict(self.token_usage)
        return data


class ChatClient(Protocol):
    def chat_response(self, model: str, messages: list[dict[str, str]]):
        ...


class SkillEvolution:
    """Offline skill draft generation from successful run logs."""

    def __init__(
        self,
        *,
        llm: ChatClient,
        model: str,
        workspace: Path,
        config: SkillEvolutionConfig | None = None,
    ):
        self.llm = llm
        self.model = model
        self.workspace = workspace.resolve()
        self.config = config or SkillEvolutionConfig()

    def run(self) -> SkillEvolutionResult:
        result = SkillEvolutionResult(status="started")
        try:
            run_logs = _load_recent_run_logs(
                _resolve_path(self.workspace, self.config.run_log_path),
                limit=self.config.max_runs,
            )
            result.inspected_runs = len(run_logs)
            catalog = SkillCatalog.load(_resolve_path(self.workspace, self.config.skills_dir))
            for path, payload in run_logs:
                trace = _extract_trace(payload)
                if not _is_eligible_trace(trace, self.config.min_tool_steps):
                    result.skipped.append(
                        {
                            "path": str(path),
                            "reason": "insufficient_successful_reusable_signal",
                            "task": trace.get("task", ""),
                        }
                    )
                    continue
                result.eligible_runs += 1
                try:
                    draft, token_usage = self._generate_draft(trace, catalog)
                    result.token_usage.add(token_usage)
                except Exception as exc:
                    result.skipped.append({"path": str(path), "reason": f"llm_error:{exc}"})
                    continue

                operation = str(draft.get("operation") or "reject").strip().lower()
                if operation not in {"create", "update", "merge", "reject"}:
                    operation = "reject"
                if operation == "reject":
                    result.skipped.append(
                        {
                            "path": str(path),
                            "reason": draft.get("reason") or "llm_rejected_skill_candidate",
                            "task": trace.get("task", ""),
                        }
                    )
                    continue
                write = self._write_draft(draft, operation=operation, source_path=path)
                result.drafts.append(write)
            result.status = "completed"
        except Exception as exc:
            result.status = "error"
            result.error = str(exc)
        return result

    def _generate_draft(self, trace: dict[str, Any], catalog: SkillCatalog) -> tuple[dict[str, Any], TokenUsage]:
        messages = [
            {"role": "system", "content": _skill_evolution_prompt()},
            {
                "role": "user",
                "content": json.dumps(
                    {
                        "successful_trace": trace,
                        "existing_skills": [_skill_metadata(skill) for skill in catalog.all()],
                    },
                    ensure_ascii=False,
                ),
            },
        ]
        response = self.llm.chat_response(model=self.model, messages=messages)
        data = _parse_json_object(response.content)
        return data, response.token_usage

    def _write_draft(self, draft: dict[str, Any], *, operation: str, source_path: Path) -> SkillDraftWrite:
        name = _slugify(str(draft.get("name") or "skill_draft"), limit=48)
        target_skill = _slugify(str(draft.get("target_skill") or ""), limit=48)
        draft_dir = _resolve_path(self.workspace, self.config.skills_dir) / self.config.drafts_dirname
        draft_dir.mkdir(parents=True, exist_ok=True)
        filename = f"{datetime.now().strftime('%Y%m%d-%H%M%S')}-{operation}-{name}.md"
        path = _avoid_overwrite(draft_dir / filename)
        path.write_text(_render_skill_draft(draft, operation=operation, source_path=source_path), encoding="utf-8")
        return SkillDraftWrite(
            path=str(path),
            operation=operation,
            target_skill=target_skill,
            name=name,
        )


def _skill_evolution_prompt() -> str:
    return (
        "You generate draft MiniCode skills from successful agent traces. "
        "Return one JSON object only. Schema: "
        '{"operation":"create|update|merge|reject","target_skill":"existing skill name or empty",'
        '"name":"snake_case_skill_name","description":"short description","tags":["tag"],'
        '"intents":["intent"],"tools":["tool"],"triggers":["trigger"],'
        '"workflow":["step"],"boundaries":["boundary"],"completion_criteria":["criterion"],'
        '"reason":"why this should or should not become a skill"}. '
        "Create or update a skill only when the trace contains a reusable workflow, not a one-off fact. "
        "Prefer reject for vague, failed, trivial, or project-only tasks. "
        "Do not include secrets or raw logs. Keep the workflow concise and tool-oriented."
    )


def _load_recent_run_logs(path: Path, limit: int) -> list[tuple[Path, dict[str, Any]]]:
    if path.is_file():
        paths = [path]
    elif path.is_dir():
        paths = sorted(
            (
                item
                for item in path.glob("*.json")
                if item.is_file() and "_deleted" not in item.parts
            ),
            key=lambda item: item.stat().st_mtime,
            reverse=True,
        )[: max(1, limit)]
    else:
        return []

    logs: list[tuple[Path, dict[str, Any]]] = []
    for item in paths:
        try:
            payload = json.loads(item.read_text(encoding="utf-8"))
        except Exception:
            continue
        if isinstance(payload, dict):
            logs.append((item, payload))
    return logs


def _extract_trace(payload: dict[str, Any]) -> dict[str, Any]:
    steps = payload.get("steps") if isinstance(payload.get("steps"), list) else []
    tool_steps = [step for step in steps if isinstance(step, dict) and step.get("tool_name") != "finish"]
    final_test = payload.get("final_test_result")
    modified_files = sorted(
        {
            str(path)
            for step in tool_steps
            for path in (step.get("modified_files") or [])
        }
    )
    return {
        "run_id": payload.get("run_id") or "",
        "task": payload.get("task") or "",
        "answer": _preview(payload.get("answer") or "", 1200),
        "started_at": payload.get("started_at") or "",
        "tool_sequence": [step.get("tool_name") for step in tool_steps],
        "tool_count": len(tool_steps),
        "modified_files": modified_files,
        "final_test_result": final_test if isinstance(final_test, dict) else None,
        "invalid_command_count": sum(1 for step in tool_steps if step.get("invalid_command")),
        "dangerous_command_count": sum(1 for step in tool_steps if step.get("dangerous_command")),
        "prompt_injection_risks": [
            step.get("prompt_injection_review", {})
            for step in tool_steps
            if isinstance(step.get("prompt_injection_review"), dict)
            and step.get("prompt_injection_review", {}).get("level") in {"medium", "high"}
        ],
        "step_summaries": [_step_summary(step) for step in tool_steps[:12]],
    }


def _is_eligible_trace(trace: dict[str, Any], min_tool_steps: int) -> bool:
    if not trace.get("answer"):
        return False
    if trace.get("dangerous_command_count", 0) > 0:
        return False
    if trace.get("invalid_command_count", 0) > 1:
        return False
    final_test = trace.get("final_test_result")
    if isinstance(final_test, dict) and final_test and not final_test.get("passed"):
        return False
    tool_sequence = [tool for tool in trace.get("tool_sequence", []) if tool]
    has_reusable_signal = (
        len(tool_sequence) >= min_tool_steps
        or "run_tests" in tool_sequence
        or bool(trace.get("modified_files"))
    )
    return bool(has_reusable_signal)


def _step_summary(step: dict[str, Any]) -> dict[str, Any]:
    return {
        "step": step.get("step"),
        "tool": step.get("tool_name"),
        "args": _safe_args(step.get("tool_args") or {}),
        "exit_code": step.get("exit_code"),
        "modified_files": step.get("modified_files") or [],
        "stdout_preview": _preview(step.get("stdout") or "", 500),
        "stderr_preview": _preview(step.get("stderr") or "", 300),
    }


def _safe_args(args: dict[str, Any]) -> dict[str, Any]:
    safe: dict[str, Any] = {}
    for key, value in args.items():
        if key in {"content", "api_key", "token", "password", "secret"}:
            safe[key] = f"<redacted {len(str(value))} chars>"
        else:
            safe[key] = _preview(str(value), 240)
    return safe


def _skill_metadata(skill: Any) -> dict[str, Any]:
    return {
        "name": skill.name,
        "description": skill.description,
        "tags": skill.tags,
        "intents": skill.intents,
        "tools": skill.tools,
        "triggers": skill.triggers,
    }


def _render_skill_draft(draft: dict[str, Any], *, operation: str, source_path: Path) -> str:
    name = _slugify(str(draft.get("name") or "skill_draft"), limit=48)
    description = str(draft.get("description") or "Draft skill generated from run logs.").strip()
    rows = [
        "---",
        f'name: "{name}"',
        f'description: "{_escape(description)}"',
        f"tags: {_frontmatter_list(draft.get('tags'))}",
        f"intents: {_frontmatter_list(draft.get('intents'))}",
        f"tools: {_frontmatter_list(draft.get('tools'))}",
        f"triggers: {_frontmatter_list(draft.get('triggers'))}",
        f'evolution_operation: "{operation}"',
        f'target_skill: "{_escape(str(draft.get("target_skill") or ""))}"',
        f'source_run_log: "{_escape(str(source_path))}"',
        f'generated_at: "{datetime.now().isoformat()}"',
        "---",
        "",
        "# Draft Skill",
        "",
        f"> Operation: `{operation}`. Review manually before moving this file into `.skills/`.",
        "",
        "## Why This Draft Exists",
        "",
        str(draft.get("reason") or "Generated from a successful reusable trace.").strip(),
        "",
        "## Workflow",
        "",
    ]
    rows.extend(f"{index}. {item}" for index, item in enumerate(_as_list(draft.get("workflow")), start=1))
    rows.extend(["", "## Boundaries", ""])
    rows.extend(f"- {item}" for item in _as_list(draft.get("boundaries")))
    rows.extend(["", "## Completion Criteria", ""])
    rows.extend(f"- {item}" for item in _as_list(draft.get("completion_criteria")))
    rows.append("")
    return "\n".join(rows)


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
        raise ValueError("skill evolution response must be a JSON object")
    return data


def _frontmatter_list(value: Any) -> str:
    return "[" + ", ".join(f'"{_escape(item)}"' for item in _as_list(value)) + "]"


def _as_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if value is None:
        return []
    text = str(value).strip()
    return [text] if text else []


def _resolve_path(workspace: Path, raw_path: str) -> Path:
    path = Path(raw_path)
    if path.is_absolute():
        return path
    return workspace / path


def _avoid_overwrite(path: Path) -> Path:
    if not path.exists():
        return path
    stem = path.stem
    suffix = path.suffix
    for index in range(1, 1000):
        candidate = path.with_name(f"{stem}-{index}{suffix}")
        if not candidate.exists():
            return candidate
    raise RuntimeError(f"could not find available skill draft filename for {path}")


def _slugify(value: str, limit: int = 60) -> str:
    slug = re.sub(r"[^0-9A-Za-z_.-]+", "_", value.strip()).strip("_.-").lower()
    return (slug or "skill_draft")[:limit]


def _preview(value: str, limit: int) -> str:
    text = " ".join(str(value or "").split())
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."


def _escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')
