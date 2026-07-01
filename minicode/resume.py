from __future__ import annotations

import json
import shutil
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from .memory import FileMemoryStore, MemoryArchiveResult


@dataclass(frozen=True)
class ResumeResult:
    source_path: Path
    restored_turns: int
    restored_steps: int
    message_content: str


@dataclass(frozen=True)
class ResumeCandidate:
    index: int
    path: Path
    run_id: str
    task: str
    started_at: str
    turns: int
    steps: int
    answer_preview: str
    resumable: bool


@dataclass(frozen=True)
class SessionDeleteResult:
    run_id: str
    source_path: Path
    deleted_log_path: Path
    archived_memories: list[MemoryArchiveResult]


def find_resume_log(raw_path: str, *, workspace: Path, default_target: str | Path) -> Path:
    target = Path(raw_path.strip()) if raw_path.strip() else Path(default_target)
    if not target.is_absolute():
        target = workspace / target
    if target.is_file():
        return target
    if target.is_dir():
        candidates = sorted(
            (path for path in target.glob("*.json") if path.is_file()),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
        for candidate in candidates:
            if _is_resumable_log(candidate):
                return candidate
        if candidates:
            raise FileNotFoundError(f"no resumable run logs found in {target}")
        raise FileNotFoundError(f"no JSON run logs found in {target}")
    raise FileNotFoundError(f"resume log not found: {target}")


def list_resume_candidates(
    raw_path: str,
    *,
    workspace: Path,
    default_target: str | Path,
    limit: int = 30,
) -> list[ResumeCandidate]:
    target = _resolve_target(raw_path, workspace=workspace, default_target=default_target)
    paths = [target] if target.is_file() else _json_logs_in_directory(target)
    candidates: list[ResumeCandidate] = []
    for path in paths[: max(1, limit)]:
        try:
            payload = load_resume_log(path)
        except Exception:
            continue
        candidates.append(_candidate_from_payload(len(candidates) + 1, path, payload))
    return candidates


def resolve_resume_selection(candidates: list[ResumeCandidate], selection: str) -> ResumeCandidate:
    text = selection.strip()
    if not text:
        raise ValueError("resume selection is required")
    try:
        index = int(text)
    except ValueError as exc:
        raise ValueError("enter a session number from the list") from exc
    for candidate in candidates:
        if candidate.index == index:
            return candidate
    raise ValueError(f"unknown resume selection: {index}")


def load_resume_log(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"resume log must contain a JSON object: {path}")
    return payload


def _resolve_target(raw_path: str, *, workspace: Path, default_target: str | Path) -> Path:
    target = Path(raw_path.strip()) if raw_path.strip() else Path(default_target)
    if not target.is_absolute():
        target = workspace / target
    if not target.exists():
        raise FileNotFoundError(f"resume target not found: {target}")
    return target


def _json_logs_in_directory(target: Path) -> list[Path]:
    if not target.is_dir():
        raise FileNotFoundError(f"resume target is not a directory: {target}")
    return sorted(
        (path for path in target.glob("*.json") if path.is_file()),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )


def _candidate_from_payload(index: int, path: Path, payload: dict[str, Any]) -> ResumeCandidate:
    turns = payload.get("session_turns") or []
    steps = payload.get("steps") or []
    answer = payload.get("answer") or ""
    return ResumeCandidate(
        index=index,
        path=path,
        run_id=str(payload.get("run_id") or ""),
        task=_preview(payload.get("task") or "", 80),
        started_at=str(payload.get("started_at") or ""),
        turns=len(turns) if isinstance(turns, list) else 0,
        steps=len(steps) if isinstance(steps, list) else 0,
        answer_preview=_preview(answer, 100),
        resumable=bool(answer or steps or turns),
    )


def _is_resumable_log(path: Path) -> bool:
    try:
        payload = load_resume_log(path)
    except Exception:
        return False
    if payload.get("answer"):
        return True
    if payload.get("steps"):
        return True
    if payload.get("session_turns"):
        return True
    return False


def build_resume_result(payload: dict[str, Any], source_path: Path) -> ResumeResult:
    message = render_resume_context(payload, source_path)
    turns = payload.get("session_turns") or []
    steps = payload.get("steps") or []
    return ResumeResult(
        source_path=source_path,
        restored_turns=len(turns) if isinstance(turns, list) else 0,
        restored_steps=len(steps) if isinstance(steps, list) else 0,
        message_content=message,
    )


def delete_session_log(path: Path, memory_store: FileMemoryStore) -> SessionDeleteResult:
    payload = load_resume_log(path)
    run_id = str(payload.get("run_id") or "").strip()
    if not run_id:
        raise ValueError(f"cannot delete session without run_id: {path}")

    memory_ids = related_memory_ids_for_run(memory_store, run_id)
    archived = memory_store.archive(memory_ids, reason=f"deleted_session:{run_id}") if memory_ids else []
    deleted_log_path = _move_run_log_to_deleted(path)
    return SessionDeleteResult(
        run_id=run_id,
        source_path=path,
        deleted_log_path=deleted_log_path,
        archived_memories=archived,
    )


def related_memory_ids_for_run(memory_store: FileMemoryStore, run_id: str) -> list[str]:
    run_id = run_id.strip()
    if not run_id:
        return []
    items = memory_store.all()
    selected: set[str] = set()

    changed = True
    while changed:
        changed = False
        for item in items:
            if item.memory_id in selected:
                continue
            if _memory_matches_run(item, run_id) or any(parent in selected for parent in item.parent_memory_ids):
                selected.add(item.memory_id)
                changed = True
    return sorted(selected)


def render_resume_context(payload: dict[str, Any], source_path: Path) -> str:
    rows = [
        "Resumed historical MiniCode session context.",
        "Treat this as background context only. Do not assume files are unchanged; use tools for fresh workspace facts.",
        f"Source log: {source_path}",
        f"Previous run_id: {payload.get('run_id') or 'unknown'}",
        f"Previous task: {_one_line(payload.get('task') or 'unknown')}",
        f"Previous model: {payload.get('model') or 'unknown'}",
        f"Started at: {payload.get('started_at') or 'unknown'}",
    ]

    turns = payload.get("session_turns")
    if isinstance(turns, list) and turns:
        rows.extend(["", "Recovered conversation turns:"])
        for item in turns[-8:]:
            if not isinstance(item, dict):
                continue
            rows.append(f"- Turn {item.get('turn', '?')} user: {_preview(item.get('user') or '', 320)}")
            answer = _preview(item.get("answer") or "", 500)
            if answer:
                rows.append(f"  answer: {answer}")
    else:
        answer = _preview(payload.get("answer") or "", 1200)
        if answer:
            rows.extend(["", "Recovered answer summary:", answer])

    policies = payload.get("policies")
    if isinstance(policies, list) and policies:
        intents = [_one_line(item.get("intent", "")) for item in policies if isinstance(item, dict)]
        intents = [intent for intent in intents if intent]
        if intents:
            rows.extend(["", "Recovered policy intents:", "- " + ", ".join(intents[-8:])])

    steps = payload.get("steps")
    if isinstance(steps, list) and steps:
        rows.extend(["", "Recent tool/action trace:"])
        for step in steps[-10:]:
            if not isinstance(step, dict):
                continue
            rows.append(_render_step(step))

    context = payload.get("context")
    if isinstance(context, dict):
        notes = context.get("notes") or []
        artifacts = context.get("artifacts") or []
        rows.extend(
            [
                "",
                f"Recovered context metadata: notes={len(notes) if isinstance(notes, list) else 0}, "
                f"artifacts={len(artifacts) if isinstance(artifacts, list) else 0}",
            ]
        )

    return "\n".join(rows)


def _memory_matches_run(item: Any, run_id: str) -> bool:
    if item.source_run_id == run_id or item.source_run == run_id:
        return True
    return any(trace_id == run_id or trace_id.startswith(f"{run_id}:") for trace_id in item.source_trace_ids)


def _move_run_log_to_deleted(path: Path) -> Path:
    if not path.is_file():
        raise FileNotFoundError(f"run log not found: {path}")
    deleted_dir = path.parent / "_deleted"
    deleted_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    target = _avoid_overwrite(deleted_dir / f"{timestamp}-{path.name}")
    shutil.move(str(path), str(target))
    return target


def _avoid_overwrite(path: Path) -> Path:
    if not path.exists():
        return path
    stem = path.stem
    suffix = path.suffix
    for index in range(1, 1000):
        candidate = path.with_name(f"{stem}-{index}{suffix}")
        if not candidate.exists():
            return candidate
    raise RuntimeError(f"could not find available deleted-session filename for {path}")


def _render_step(step: dict[str, Any]) -> str:
    tool_name = step.get("tool_name") or "unknown"
    number = step.get("step") or "?"
    exit_code = step.get("exit_code")
    modified = step.get("modified_files") or []
    stdout = _preview(step.get("stdout") or "", 220)
    stderr = _preview(step.get("stderr") or "", 160)
    parts = [f"- step {number}: {tool_name} exit={exit_code}"]
    if modified:
        parts.append(f"modified={','.join(str(item) for item in modified)}")
    if stdout:
        parts.append(f"stdout={stdout}")
    if stderr:
        parts.append(f"stderr={stderr}")
    return "; ".join(parts)


def _preview(value: Any, limit: int) -> str:
    text = _one_line(str(value or ""))
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."


def _one_line(value: str) -> str:
    return " ".join(value.split())
