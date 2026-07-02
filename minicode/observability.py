from __future__ import annotations

import hashlib
import re
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


IGNORED_DIRS = {".git", ".minicode", "__pycache__", ".pytest_cache", ".mypy_cache"}


@dataclass
class TokenUsage:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0

    def add(self, other: "TokenUsage") -> None:
        self.prompt_tokens += other.prompt_tokens
        self.completion_tokens += other.completion_tokens
        self.total_tokens += other.total_tokens


@dataclass
class StepLog:
    step: int
    model_input_summary: str
    model_action: dict[str, Any]
    tool_name: str
    tool_args: dict[str, Any]
    permission_decision: str
    permission_reason: str
    stdout: str
    stderr: str
    exit_code: int | None
    modified_files: list[str]
    token_usage: TokenUsage
    duration_ms: int
    dangerous_command: bool = False
    invalid_command: bool = False
    context_event: dict[str, Any] | None = None
    retrieval_trace: dict[str, Any] | None = None
    prompt_injection_review: dict[str, Any] | None = None


@dataclass
class TestResult:
    command: str
    passed: bool
    stdout: str
    stderr: str
    exit_code: int
    duration_ms: int


@dataclass
class RunLog:
    task: str
    model: str
    started_at: str
    run_id: str = ""
    duration_ms: int = 0
    answer: str = ""
    skill_route: dict[str, Any] | None = None
    policies: list[dict[str, Any]] = field(default_factory=list)
    session_turns: list[dict[str, Any]] = field(default_factory=list)
    context: dict[str, Any] = field(default_factory=dict)
    memory_trigger: dict[str, Any] | None = None
    memory_dreaming: dict[str, Any] | None = None
    steps: list[StepLog] = field(default_factory=list)
    token_usage: TokenUsage = field(default_factory=TokenUsage)
    final_test_result: TestResult | None = None

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["memory_evolution"] = data.get("memory_trigger")
        return data


class Timer:
    def __init__(self) -> None:
        self.started = time.perf_counter()

    def elapsed_ms(self) -> int:
        return int((time.perf_counter() - self.started) * 1000)


class FileSnapshot:
    def __init__(self, workspace: Path):
        self.workspace = workspace.resolve()
        self.files = _scan_files(self.workspace)

    def diff(self) -> list[str]:
        current = _scan_files(self.workspace)
        changed = sorted(
            path
            for path in set(self.files) | set(current)
            if self.files.get(path) != current.get(path)
        )
        self.files = current
        return changed


def summarize_messages(messages: list[dict[str, str]], limit: int = 800) -> str:
    parts: list[str] = []
    for message in messages[-4:]:
        content = " ".join(message.get("content", "").split())
        role = message.get("role", "unknown")
        parts.append(f"{role}: {content}")
    summary = "\n".join(parts)
    if len(summary) <= limit:
        return summary
    return summary[: limit - 3] + "..."


def make_run_id(started_at: str, task: str) -> str:
    timestamp = re.sub(r"[^0-9]", "", started_at)[:14] or "00000000000000"
    digest = hashlib.sha256(f"{started_at}|{task}".encode("utf-8")).hexdigest()[:10]
    return f"run_{timestamp}_{digest}"


def _scan_files(root: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    if not root.exists():
        return result

    for path in root.rglob("*"):
        if not path.is_file():
            continue
        rel_parts = path.relative_to(root).parts
        if any(part in IGNORED_DIRS for part in rel_parts):
            continue
        rel = path.relative_to(root).as_posix()
        result[rel] = _fingerprint(path)
    return result


def _fingerprint(path: Path) -> str:
    stat = path.stat()
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return f"{stat.st_size}:{digest.hexdigest()}"
