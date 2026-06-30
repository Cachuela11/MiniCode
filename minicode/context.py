from __future__ import annotations

import hashlib
import os
import re
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .sandbox import DockerSandbox


CONTEXT_LAYERS: list[dict[str, str]] = [
    {
        "level": "L0",
        "name": "runtime contract",
        "load": "static",
        "content": "system role, JSON action protocol, tool list, context policy",
    },
    {
        "level": "L1",
        "name": "workspace file index",
        "load": "static at run start",
        "content": "current Docker working directory and bounded file path list",
    },
    {
        "level": "L2",
        "name": "initial skill context",
        "load": "task-routed before first model call",
        "content": "top selected skill docs from two-stage skill router",
    },
    {
        "level": "L3",
        "name": "dynamic working memory",
        "load": "updated during the agent loop",
        "content": "full action JSON and observations; observations may come from files, shell, tests, artifact reads, skill loads, or memory loads",
    },
]


def render_context_layer_prompt() -> str:
    rows = [
        "Context layers:",
        "- L0 static runtime contract: always present and cache-friendly.",
        "- L1 workspace file index: Docker pwd plus a bounded list of file paths, not file contents.",
        "- L2 initial skills: task-routed skill docs injected before the first action.",
        "- L3 dynamic working memory: recent action JSON plus observations. Tool results from files, shell, tests, skills, and memory are all observations.",
        "Large observations may be represented by artifact placeholders; old action/observation history may be represented by structured notes.",
        "Use read_context_artifact, search_skills/load_skill, or search_memory/load_memory when you need a new observation that is not already in context.",
    ]
    return "\n".join(rows)


def build_initial_context(sandbox: DockerSandbox) -> str:
    result = sandbox.run(
        "pwd && find . -maxdepth 2 "
        "\\( -path './.git' -o -path './.minicode' -o -path '*/__pycache__' \\) -prune "
        "-o -type f -print | sort | head -200"
    )
    if result.exit_code != 0:
        return f"Could not inspect workspace:\n{result.stderr}"
    return result.stdout.strip() or "Workspace has no files."


@dataclass(frozen=True)
class ContextConfig:
    artifact_dir: str = ".minicode/context-artifacts"
    observation_inline_limit: int = 6000
    observation_preview_chars: int = 1200
    history_char_limit: int = 24000
    keep_recent_messages: int = 6
    note_char_limit: int = 6000


@dataclass(frozen=True)
class ContextArtifact:
    artifact_id: str
    path: str
    chars: int
    lines: int
    sha256: str
    source: str
    step: int


@dataclass(frozen=True)
class ContextNote:
    step: int
    tool_name: str
    status: str
    exit_code: int | None
    chars: int
    lines: int
    mode: str
    artifact_id: str | None = None
    modified_files: list[str] = field(default_factory=list)
    preview: str = ""


@dataclass(frozen=True)
class ContextEvent:
    mode: str
    message_content: str
    original_chars: int
    original_lines: int
    artifact_id: str | None = None
    preview: str = ""
    detached: bool = False

    def to_log_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data.pop("message_content", None)
        return data


class ContextManager:
    def __init__(
        self,
        workspace: Path,
        config: ContextConfig | None = None,
        run_id: str | None = None,
    ):
        self.workspace = workspace.resolve()
        self.config = config or ContextConfig()
        self.run_id = run_id or _make_run_id()
        self.artifact_root = self._resolve_artifact_root(self.config.artifact_dir) / self.run_id
        self.artifacts: dict[str, ContextArtifact] = {}
        self.notes: list[ContextNote] = []
        self.compactions: list[dict[str, Any]] = []
        self._artifact_counter = 0

    def record_observation(
        self,
        *,
        step: int,
        tool_name: str,
        output: str,
        exit_code: int | None,
        modified_files: list[str],
    ) -> ContextEvent:
        text = output or ""
        line_count = _line_count(text)
        preview = _preview_text(text, self.config.observation_preview_chars)
        status = "ok" if exit_code == 0 else "error"
        inline_limit = self.config.observation_inline_limit
        if tool_name == "read_context_artifact":
            inline_limit = max(inline_limit, self.config.observation_preview_chars * 4, 8000)

        if len(text) <= inline_limit:
            self.notes.append(
                ContextNote(
                    step=step,
                    tool_name=tool_name,
                    status=status,
                    exit_code=exit_code,
                    chars=len(text),
                    lines=line_count,
                    mode="inline",
                    modified_files=modified_files,
                    preview=preview,
                )
            )
            return ContextEvent(
                mode="inline",
                message_content=text,
                original_chars=len(text),
                original_lines=line_count,
                preview=preview,
            )

        artifact = self._write_artifact(step=step, tool_name=tool_name, text=text)
        self.notes.append(
            ContextNote(
                step=step,
                tool_name=tool_name,
                status=status,
                exit_code=exit_code,
                chars=len(text),
                lines=line_count,
                mode="artifact",
                artifact_id=artifact.artifact_id,
                modified_files=modified_files,
                preview=preview,
            )
        )
        message_content = self._render_artifact_observation(artifact, preview)
        return ContextEvent(
            mode="artifact",
            message_content=message_content,
            original_chars=len(text),
            original_lines=line_count,
            artifact_id=artifact.artifact_id,
            preview=preview,
            detached=True,
        )

    def compact_messages(self, messages: list[dict[str, str]]) -> list[dict[str, str]]:
        total_chars = _messages_chars(messages)
        if total_chars <= self.config.history_char_limit:
            return messages

        keep_recent = max(0, self.config.keep_recent_messages)
        if len(messages) <= 2 + keep_recent:
            return messages

        head = messages[:2]
        tail = messages[-keep_recent:] if keep_recent else []
        note_message = {
            "role": "user",
            "content": self._render_detached_notes(total_chars),
        }
        compacted = head + [note_message] + tail
        self.compactions.append(
            {
                "before_chars": total_chars,
                "after_chars": _messages_chars(compacted),
                "kept_recent_messages": keep_recent,
                "note_count": len(self.notes),
            }
        )
        return compacted

    def read_artifact(self, artifact_id: str, start_line: int = 1, limit: int = 200) -> str:
        artifact = self.artifacts.get(artifact_id)
        if artifact is None:
            available = ", ".join(sorted(self.artifacts)) or "none"
            raise ValueError(f"unknown context artifact {artifact_id!r}; available: {available}")

        path = Path(artifact.path)
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        start = max(1, start_line)
        count = max(1, min(limit, 1000))
        selected = lines[start - 1 : start - 1 + count]
        numbered = [f"{line_no}: {line}" for line_no, line in enumerate(selected, start=start)]
        end = start + len(selected) - 1 if selected else start - 1
        header = (
            f"Context artifact {artifact_id} lines {start}-{end} of {artifact.lines} "
            f"(chars={artifact.chars}, sha256={artifact.sha256[:12]})."
        )
        body = "\n".join(numbered) or "No lines in requested range."
        return f"{header}\n{body}"

    def to_log_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "layers": CONTEXT_LAYERS,
            "config": asdict(self.config),
            "artifact_root": str(self.artifact_root),
            "artifacts": [asdict(artifact) for artifact in self.artifacts.values()],
            "notes": [asdict(note) for note in self.notes],
            "compactions": self.compactions,
        }

    def _write_artifact(self, *, step: int, tool_name: str, text: str) -> ContextArtifact:
        self._artifact_counter += 1
        artifact_id = f"obs-{self._artifact_counter:04d}"
        self.artifact_root.mkdir(parents=True, exist_ok=True)
        path = self.artifact_root / f"{artifact_id}.txt"
        path.write_text(text, encoding="utf-8")
        artifact = ContextArtifact(
            artifact_id=artifact_id,
            path=str(path),
            chars=len(text),
            lines=_line_count(text),
            sha256=hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest(),
            source=tool_name,
            step=step,
        )
        self.artifacts[artifact_id] = artifact
        return artifact

    def _render_artifact_observation(self, artifact: ContextArtifact, preview: str) -> str:
        return "\n".join(
            [
                f"Observation externalized as [[context_artifact:{artifact.artifact_id}]].",
                f"Source: step={artifact.step}, tool={artifact.source}",
                f"Stats: chars={artifact.chars}, lines={artifact.lines}, sha256={artifact.sha256}",
                "Preview:",
                preview or "(empty)",
                "",
                "Need more detail? Call read_context_artifact with "
                f'{{"artifact_id":"{artifact.artifact_id}","start_line":1,"limit":200}}.',
            ]
        )

    def _render_detached_notes(self, before_chars: int) -> str:
        rows = [
            "Earlier agent history was detached because the prompt history exceeded the context budget.",
            f"Detached-history trigger: {before_chars} chars > {self.config.history_char_limit} chars.",
            "Structured notes:",
        ]
        for note in self.notes:
            artifact = f", artifact={note.artifact_id}" if note.artifact_id else ""
            modified = f", modified={','.join(note.modified_files)}" if note.modified_files else ""
            preview = re.sub(r"\s+", " ", note.preview).strip()
            if len(preview) > 240:
                preview = preview[:237] + "..."
            rows.append(
                f"- step {note.step}: {note.tool_name} status={note.status} "
                f"exit={note.exit_code} mode={note.mode} chars={note.chars} lines={note.lines}"
                f"{artifact}{modified}; preview={preview}"
            )
        rows.append("Use read_context_artifact for artifact details when needed.")
        text = "\n".join(rows)
        if len(text) <= self.config.note_char_limit:
            return text
        return text[: self.config.note_char_limit - 3] + "..."

    def _resolve_artifact_root(self, raw_path: str) -> Path:
        path = Path(raw_path)
        if path.is_absolute():
            return path
        return self.workspace / path


def _line_count(text: str) -> int:
    if not text:
        return 0
    return text.count("\n") + (0 if text.endswith("\n") else 1)


def _messages_chars(messages: list[dict[str, str]]) -> int:
    return sum(len(message.get("content", "")) for message in messages)


def _preview_text(text: str, limit: int) -> str:
    normalized = text.strip()
    if len(normalized) <= limit:
        return normalized
    return normalized[: max(0, limit - 3)] + "..."


def _make_run_id() -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    return f"{stamp}-{os.getpid()}-{uuid.uuid4().hex[:8]}"
