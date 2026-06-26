from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from typing import Any, Protocol

from .memory import FileMemoryStore, MemoryCandidate, MemoryWriteResult
from .observability import RunLog, TokenUsage


MEMORY_TRIGGER_MODES = {"off", "draft", "auto"}


@dataclass(frozen=True)
class MemorySignal:
    memory_type: str
    reason: str
    evidence: list[str]
    priority: int = 1


@dataclass
class MemoryEvolutionResult:
    mode: str
    status: str
    signals: list[MemorySignal] = field(default_factory=list)
    written: list[MemoryWriteResult] = field(default_factory=list)
    skipped: list[dict[str, Any]] = field(default_factory=list)
    token_usage: TokenUsage = field(default_factory=TokenUsage)
    error: str = ""

    def to_log_dict(self) -> dict[str, Any]:
        return asdict(self)


class ChatClient(Protocol):
    def chat_response(self, model: str, messages: list[dict[str, str]]):
        ...


class SelfEvolution:
    """Run-level memory sedimentation trigger.

    This class intentionally implements only the online trigger loop:
    rule signals -> LLM judgement/distillation -> draft/active memory write.
    Offline dreaming, merge, decay, and skill promotion belong to a later pass.
    """

    def __init__(
        self,
        llm: ChatClient,
        model: str,
        memory_store: FileMemoryStore,
        mode: str = "draft",
        min_confidence: float = 0.7,
        max_candidates: int = 5,
    ):
        self.llm = llm
        self.model = model
        self.memory_store = memory_store
        self.mode = _normalize_mode(mode)
        self.min_confidence = max(0.0, min(1.0, min_confidence))
        self.max_candidates = max(1, min(max_candidates, 10))
        self.prefilter = RuleMemoryPrefilter()

    def on_run_complete(self, run_log: RunLog) -> MemoryEvolutionResult:
        if self.mode == "off":
            return MemoryEvolutionResult(mode=self.mode, status="off")

        signals = self.prefilter.collect(run_log)
        if not signals:
            return MemoryEvolutionResult(mode=self.mode, status="no_signal")

        result = MemoryEvolutionResult(mode=self.mode, status="no_candidates", signals=signals)
        try:
            candidates, token_usage, skipped = LlmMemoryJudge(
                llm=self.llm,
                model=self.model,
                max_candidates=self.max_candidates,
            ).judge(run_log=run_log, signals=signals)
            result.token_usage = token_usage
            result.skipped.extend(skipped)
            write_status = "active" if self.mode == "auto" else "draft"
            for candidate in candidates:
                if candidate.confidence < self.min_confidence:
                    result.skipped.append(
                        {
                            "title": candidate.title,
                            "type": candidate.memory_type,
                            "reason": "confidence_below_threshold",
                            "confidence": candidate.confidence,
                        }
                    )
                    continue
                if _looks_duplicate(self.memory_store, candidate):
                    result.skipped.append(
                        {
                            "title": candidate.title,
                            "type": candidate.memory_type,
                            "reason": "possible_duplicate",
                            "confidence": candidate.confidence,
                        }
                    )
                    continue
                result.written.append(self.memory_store.write_candidate(candidate, status=write_status))
            result.status = "written" if result.written else "no_candidates"
        except Exception as exc:  # Keep task completion independent from memory reflection.
            result.status = "error"
            result.error = str(exc)
        return result


class RuleMemoryPrefilter:
    def collect(self, run_log: RunLog) -> list[MemorySignal]:
        signals: list[MemorySignal] = []
        text = _normalize(" ".join([run_log.task, run_log.answer]))
        modified_files = _modified_files(run_log)
        tool_names = [step.tool_name for step in run_log.steps]
        invalid_count = sum(1 for step in run_log.steps if step.invalid_command)
        dangerous_count = sum(1 for step in run_log.steps if step.dangerous_command)

        project_evidence = self._project_evidence(text, modified_files)
        if project_evidence:
            signals.append(
                MemorySignal(
                    memory_type="project_memory",
                    reason="project structure, architecture, docs, or configuration signal",
                    evidence=project_evidence,
                    priority=3,
                )
            )

        procedural_evidence = self._procedural_evidence(run_log, text, tool_names, invalid_count, dangerous_count)
        if procedural_evidence:
            signals.append(
                MemorySignal(
                    memory_type="procedural_memory",
                    reason="reusable workflow, repair pattern, validation pattern, or tool-use lesson",
                    evidence=procedural_evidence,
                    priority=2,
                )
            )

        experience_evidence = self._experience_evidence(text)
        if experience_evidence:
            signals.append(
                MemorySignal(
                    memory_type="experience_memory",
                    reason="explicit collaboration experience or stable working preference",
                    evidence=experience_evidence,
                    priority=3,
                )
            )

        return sorted(signals, key=lambda item: (-item.priority, item.memory_type))

    def _project_evidence(self, text: str, modified_files: list[str]) -> list[str]:
        evidence: list[str] = []
        project_patterns = [
            r"\barchitecture\b",
            r"\breadme\b",
            r"\bmermaid\b",
            r"\bcontext\b",
            r"\bskill\b",
            r"\bmemory\b",
            r"\bagent loop\b",
            r"架构",
            r"流程",
            r"项目",
            r"上下文",
            r"记忆",
            r"技能",
        ]
        if any(re.search(pattern, text) for pattern in project_patterns):
            evidence.append("task or answer mentions project architecture/docs/context/skill/memory")
        for path in modified_files:
            if path == "README.md" or path == "pyproject.toml" or path.startswith(".skills/"):
                evidence.append(f"modified project-level file: {path}")
            elif path.startswith("minicode/") and path.split("/")[-1] in {
                "agent.py",
                "context.py",
                "memory.py",
                "evolution.py",
                "tools.py",
                "cli.py",
            }:
                evidence.append(f"modified runtime file: {path}")
        return evidence[:6]

    def _procedural_evidence(
        self,
        run_log: RunLog,
        text: str,
        tool_names: list[str],
        invalid_count: int,
        dangerous_count: int,
    ) -> list[str]:
        evidence: list[str] = []
        if run_log.final_test_result and run_log.final_test_result.passed:
            evidence.append(f"final test passed: {run_log.final_test_result.command}")
        if "run_tests" in tool_names:
            evidence.append("agent used run_tests during the task")
        if invalid_count:
            evidence.append(f"invalid command count: {invalid_count}")
        if dangerous_count:
            evidence.append(f"dangerous command count: {dangerous_count}")
        procedural_patterns = [
            r"\bfix\b",
            r"\btest\b",
            r"\brefactor\b",
            r"\bvalidation\b",
            r"\bapi\b",
            r"修复",
            r"测试",
            r"重构",
            r"校验",
            r"验证",
            r"流程",
        ]
        if any(re.search(pattern, text) for pattern in procedural_patterns):
            evidence.append("task or answer mentions repair/test/refactor/validation workflow")
        return evidence[:6]

    def _experience_evidence(self, text: str) -> list[str]:
        evidence: list[str] = []
        explicit_patterns = [
            r"不要",
            r"别",
            r"不想",
            r"不喜欢",
            r"我想",
            r"我希望",
            r"希望",
            r"偏好",
            r"以后",
            r"默认",
            r"太.*复杂",
            r"太.*花",
            r"精简",
            r"清晰",
            r"simple",
            r"concise",
            r"prefer",
            r"do not",
            r"don't",
        ]
        matched = [pattern for pattern in explicit_patterns if re.search(pattern, text)]
        if matched:
            evidence.append("user phrasing contains explicit preference or collaboration guidance")
            evidence.extend(f"matched pattern: {pattern}" for pattern in matched[:3])
        return evidence[:5]


class LlmMemoryJudge:
    def __init__(self, llm: ChatClient, model: str, max_candidates: int):
        self.llm = llm
        self.model = model
        self.max_candidates = max_candidates

    def judge(
        self,
        run_log: RunLog,
        signals: list[MemorySignal],
    ) -> tuple[list[MemoryCandidate], TokenUsage, list[dict[str, Any]]]:
        messages = [
            {
                "role": "system",
                "content": _reflection_system_prompt(self.max_candidates),
            },
            {
                "role": "user",
                "content": json.dumps(
                    {
                        "signals": [asdict(signal) for signal in signals],
                        "run": _run_summary(run_log),
                    },
                    ensure_ascii=False,
                ),
            },
        ]
        response = self.llm.chat_response(model=self.model, messages=messages)
        data = _parse_json_object(response.content)
        raw_candidates = data.get("candidates", [])
        if not isinstance(raw_candidates, list):
            raw_candidates = []

        candidates: list[MemoryCandidate] = []
        skipped: list[dict[str, Any]] = []
        source_run = _source_run_id(run_log)
        for raw in raw_candidates[: self.max_candidates]:
            if not isinstance(raw, dict):
                skipped.append({"reason": "candidate_not_object"})
                continue
            sensitive = bool(raw.get("sensitive", False))
            memory_type = str(raw.get("type") or raw.get("memory_type") or "").strip()
            title = str(raw.get("title") or "").strip()
            body = str(raw.get("summary") or raw.get("body") or "").strip()
            if sensitive:
                skipped.append({"title": title, "type": memory_type, "reason": "sensitive"})
                continue
            if memory_type not in {"project_memory", "procedural_memory", "experience_memory"}:
                skipped.append({"title": title, "type": memory_type, "reason": "invalid_memory_type"})
                continue
            if not title or not body:
                skipped.append({"title": title, "type": memory_type, "reason": "empty_title_or_body"})
                continue
            candidates.append(
                MemoryCandidate(
                    memory_type=memory_type,
                    title=title,
                    body=body,
                    tags=_as_string_list(raw.get("tags"))[:8],
                    confidence=_as_float(raw.get("confidence")),
                    source_run=source_run,
                    evidence=_as_string_list(raw.get("evidence"))[:5],
                )
            )
        return candidates, response.token_usage, skipped


def _reflection_system_prompt(max_candidates: int) -> str:
    return (
        "You judge whether a completed coding-agent run contains durable memory worth saving. "
        "Return one JSON object only. Schema: "
        '{"candidates":[{"type":"project_memory|procedural_memory|experience_memory",'
        '"title":"short title","summary":"durable reusable memory","tags":["tag"],'
        '"confidence":0.0,"evidence":["short evidence"],"sensitive":false}]}. '
        f"Return at most {max_candidates} candidates. "
        "Only save information that is likely reusable in future runs. "
        "project_memory is for stable project facts, architecture, conventions, or decisions. "
        "procedural_memory is for reusable workflow/tool/process lessons. "
        "experience_memory is for explicit collaboration experience or stable user guidance; "
        "do not infer personality or preferences that were not clearly stated. "
        "Do not save secrets, API keys, private credentials, raw logs, or one-off trivia. "
        "Use concise summaries, not transcripts. If nothing is worth saving, return {\"candidates\":[]}."
    )


def _run_summary(run_log: RunLog) -> dict[str, Any]:
    steps: list[dict[str, Any]] = []
    for step in run_log.steps:
        steps.append(
            {
                "step": step.step,
                "tool": step.tool_name,
                "args": _redact_tool_args(step.tool_args),
                "exit_code": step.exit_code,
                "modified_files": step.modified_files,
                "dangerous_command": step.dangerous_command,
                "invalid_command": step.invalid_command,
                "output_preview": _preview(" ".join([step.stdout, step.stderr]), limit=500),
            }
        )
    final_test = None
    if run_log.final_test_result:
        final_test = {
            "command": run_log.final_test_result.command,
            "passed": run_log.final_test_result.passed,
            "exit_code": run_log.final_test_result.exit_code,
        }
    return {
        "task": _redact(run_log.task),
        "answer": _redact(_preview(run_log.answer, limit=1000)),
        "started_at": run_log.started_at,
        "duration_ms": run_log.duration_ms,
        "final_test": final_test,
        "modified_files": _modified_files(run_log),
        "steps": steps,
    }


def _modified_files(run_log: RunLog) -> list[str]:
    return sorted({path for step in run_log.steps for path in step.modified_files})


def _redact_tool_args(args: dict[str, Any]) -> dict[str, Any]:
    redacted: dict[str, Any] = {}
    for key, value in args.items():
        if key in {"content", "api_key", "token", "password", "secret"}:
            redacted[key] = f"<redacted {len(str(value))} chars>"
        else:
            redacted[key] = _redact(_preview(str(value), limit=300))
    return redacted


def _looks_duplicate(memory_store: FileMemoryStore, candidate: MemoryCandidate) -> bool:
    query = " ".join([candidate.title, candidate.body, *candidate.tags])
    for result in memory_store.search(query, limit=3, include_drafts=True):
        if result.item.memory_type == candidate.memory_type and result.score >= 12:
            return True
    return False


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
        raise ValueError("memory reflection response must be a JSON object")
    return data


def _source_run_id(run_log: RunLog) -> str:
    slug = re.sub(r"[^0-9A-Za-z._-]+", "-", run_log.task).strip("-._")[:40] or "run"
    return f"{run_log.started_at}-{slug}"


def _normalize_mode(mode: str) -> str:
    normalized = mode.strip().lower()
    if normalized in MEMORY_TRIGGER_MODES:
        return normalized
    return "draft"


def _as_string_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if value is None:
        return []
    text = str(value).strip()
    return [text] if text else []


def _as_float(value: Any) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        parsed = 0.0
    return max(0.0, min(1.0, parsed))


def _preview(value: str, limit: int) -> str:
    text = " ".join(value.split())
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."


def _redact(value: str) -> str:
    text = re.sub(r"sk-[A-Za-z0-9_\-]{8,}", "sk-<redacted>", value)
    text = re.sub(r"(?i)(api[_-]?key|token|password|secret)\s*[:=]\s*\S+", r"\1=<redacted>", text)
    return text


def _normalize(value: str) -> str:
    return value.casefold()
