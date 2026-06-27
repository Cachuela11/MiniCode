from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from typing import Any, Protocol

from .memory import FileMemoryStore, MemoryCandidate, MemoryWriteResult
from .observability import RunLog, TokenUsage


MEMORY_TRIGGER_MODES = {"off", "draft", "auto"}
LONG_TERM_MEMORY_TYPES = {"project_memory", "procedural_memory", "experience_memory"}


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

    Current online loop:
    session summary -> rule prefilter over that summary -> LLM long-term classification.
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

        result = MemoryEvolutionResult(mode=self.mode, status="started")
        session_candidate = _session_candidate(run_log)
        try:
            result.written.append(self.memory_store.write_candidate(session_candidate, status="active"))
        except Exception as exc:
            result.status = "error"
            result.error = f"session memory write failed: {exc}"
            return result

        signals = self.prefilter.collect(session_candidate)
        result.signals = signals
        if not signals:
            result.status = "session_written"
            return result

        try:
            candidates, token_usage, skipped = LongTermMemoryJudge(
                llm=self.llm,
                model=self.model,
                max_candidates=self.max_candidates,
            ).judge(session_candidate=session_candidate, signals=signals)
            result.token_usage = token_usage
            result.skipped.extend(skipped)

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
                write_status = "active" if self.mode == "auto" else "draft"
                result.written.append(self.memory_store.write_candidate(candidate, status=write_status))

            long_term_written = any(item.memory_type in LONG_TERM_MEMORY_TYPES for item in result.written)
            result.status = "long_term_written" if long_term_written else "session_written_no_long_term"
        except Exception as exc:
            result.status = "session_written_with_error"
            result.error = str(exc)
        return result


class RuleMemoryPrefilter:
    def collect(self, session_memory: MemoryCandidate) -> list[MemorySignal]:
        text = _normalize(
            " ".join(
                [
                    session_memory.title,
                    session_memory.body,
                    " ".join(session_memory.tags),
                    " ".join(session_memory.evidence),
                ]
            )
        )
        signals: list[MemorySignal] = []

        project_matches = _matched_patterns(
            text,
            [
                r"\barchitecture\b",
                r"\breadme\b",
                r"\bmermaid\b",
                r"\bcontext\b",
                r"\bskill\b",
                r"\bmemory\b",
                r"\bagent loop\b",
                r"\bpyproject\.toml\b",
                r"\breadme\.md\b",
                r"\.skills/",
                r"minicode/(agent|context|memory|evolution|tools|cli)\.py",
                r"架构",
                r"流程",
                r"项目",
                r"上下文",
                r"记忆",
                r"技能",
            ],
        )
        if project_matches:
            signals.append(
                MemorySignal(
                    memory_type="project_memory",
                    reason="session summary contains project architecture, docs, runtime, skill, or memory signals",
                    evidence=project_matches,
                    priority=3,
                )
            )

        procedural_matches = _matched_patterns(
            text,
            [
                r"\bfix\b",
                r"\btest\b",
                r"\brun_tests\b",
                r"\brefactor\b",
                r"\bvalidation\b",
                r"\bapi\b",
                r"final test: passed",
                r"invalid commands: [1-9]",
                r"dangerous commands: [1-9]",
                r"修复",
                r"测试",
                r"重构",
                r"校验",
                r"验证",
                r"流程",
            ],
        )
        if procedural_matches:
            signals.append(
                MemorySignal(
                    memory_type="procedural_memory",
                    reason="session summary contains reusable repair, test, validation, or tool-use signals",
                    evidence=procedural_matches,
                    priority=2,
                )
            )

        experience_matches = _matched_patterns(
            text,
            [
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
                r"\bsimple\b",
                r"\bconcise\b",
                r"\bprefer\b",
                r"\bdo not\b",
                r"\bdon't\b",
            ],
        )
        if experience_matches:
            signals.append(
                MemorySignal(
                    memory_type="experience_memory",
                    reason="session summary contains explicit collaboration experience or stable user guidance",
                    evidence=experience_matches,
                    priority=3,
                )
            )

        return sorted(signals, key=lambda item: (-item.priority, item.memory_type))


class LongTermMemoryJudge:
    def __init__(self, llm: ChatClient, model: str, max_candidates: int):
        self.llm = llm
        self.model = model
        self.max_candidates = max_candidates

    def judge(
        self,
        session_candidate: MemoryCandidate,
        signals: list[MemorySignal],
    ) -> tuple[list[MemoryCandidate], TokenUsage, list[dict[str, Any]]]:
        messages = [
            {
                "role": "system",
                "content": _long_term_judge_prompt(self.max_candidates),
            },
            {
                "role": "user",
                "content": json.dumps(
                    {
                        "session_memory": {
                            "title": session_candidate.title,
                            "summary": session_candidate.body,
                            "tags": session_candidate.tags,
                            "source_run": session_candidate.source_run,
                        },
                        "regex_signals": [asdict(signal) for signal in signals],
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
        for raw in raw_candidates[: self.max_candidates]:
            if not isinstance(raw, dict):
                skipped.append({"reason": "candidate_not_object"})
                continue
            sensitive = bool(raw.get("sensitive", False))
            memory_type = str(raw.get("type") or raw.get("memory_type") or "").strip().lower()
            title = str(raw.get("title") or "").strip()
            body = str(raw.get("summary") or raw.get("body") or "").strip()
            if sensitive:
                skipped.append({"title": title, "type": memory_type, "reason": "sensitive"})
                continue
            if memory_type not in LONG_TERM_MEMORY_TYPES:
                skipped.append({"title": title, "type": memory_type, "reason": "invalid_long_term_memory_type"})
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
                    source_run=session_candidate.source_run,
                    evidence=_as_string_list(raw.get("evidence"))[:5],
                )
            )
        return candidates, response.token_usage, skipped


def _long_term_judge_prompt(max_candidates: int) -> str:
    return (
        "You classify a session_memory summary into durable long-term memories. "
        "Return one JSON object only. Schema: "
        '{"candidates":[{"type":"project_memory|procedural_memory|experience_memory",'
        '"title":"short title","summary":"durable reusable memory","tags":["tag"],'
        '"confidence":0.0,"evidence":["short evidence"],"sensitive":false}]}. '
        f"Return at most {max_candidates} candidates. "
        "Use only these long-term types. Do not return session_memory. "
        "project_memory is for stable project facts, architecture, conventions, or decisions. "
        "procedural_memory is for reusable workflow, test, repair, validation, or tool-use lessons. "
        "experience_memory is for explicit collaboration experience or stable user guidance; "
        "do not infer personality or preferences that were not clearly stated. "
        "Do not save secrets, API keys, private credentials, raw logs, or one-off trivia. "
        "If the session summary has no durable long-term memory, return {\"candidates\":[]}."
    )


def _session_candidate(run_log: RunLog) -> MemoryCandidate:
    summary = _run_summary(run_log)
    modified_files = summary["modified_files"]
    tool_names = [step["tool"] for step in summary["steps"] if step["tool"] != "finish"]
    final_test = summary["final_test"]
    invalid_commands = sum(1 for step in summary["steps"] if step["invalid_command"])
    dangerous_commands = sum(1 for step in summary["steps"] if step["dangerous_command"])
    rows = [
        f"Task: {summary['task']}",
        f"Outcome: {summary['answer'] or 'No final answer recorded.'}",
        f"Tools used: {', '.join(tool_names) if tool_names else 'none'}",
        f"Modified files: {', '.join(modified_files) if modified_files else 'none'}",
        f"Invalid commands: {invalid_commands}",
        f"Dangerous commands: {dangerous_commands}",
    ]
    if final_test:
        status = "passed" if final_test["passed"] else "failed"
        rows.append(f"Final test: {status} ({final_test['command']}, exit_code={final_test['exit_code']})")
    rows.append(f"Duration ms: {summary['duration_ms']}")
    return MemoryCandidate(
        memory_type="session_memory",
        title=_session_title(run_log),
        body="\n".join(rows),
        tags=_session_tags(tool_names, modified_files),
        confidence=1.0,
        source_run=_source_run_id(run_log),
        evidence=["local session summary generated after run completion"],
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


def _session_title(run_log: RunLog) -> str:
    task = re.sub(r"\s+", " ", _redact(run_log.task)).strip()
    if len(task) > 56:
        task = task[:53] + "..."
    return f"Session: {task or 'MiniCode run'}"


def _session_tags(tool_names: list[str], modified_files: list[str]) -> list[str]:
    tags = ["session", "run"]
    tags.extend(tool for tool in tool_names[:6] if tool)
    for path in modified_files[:6]:
        if path == "README.md":
            tags.append("readme")
        elif path.startswith("minicode/"):
            tags.append("minicode")
        elif path.startswith(".skills/"):
            tags.append("skill")
    return _unique(tags)[:12]


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


def _matched_patterns(text: str, patterns: list[str]) -> list[str]:
    matches: list[str] = []
    for pattern in patterns:
        if re.search(pattern, text):
            matches.append(f"matched pattern: {pattern}")
    return matches[:6]


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
    return re.sub(r"\s+", " ", value.casefold()).strip()


def _unique(values: list[str]) -> list[str]:
    seen: set[str] = set()
    unique_values: list[str] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        unique_values.append(value)
    return unique_values
