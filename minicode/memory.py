from __future__ import annotations

import re
import json
import hashlib
import shutil
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path


MEMORY_TYPES = {"project_memory", "procedural_memory", "experience_memory", "session_memory"}
MEMORY_ARCHIVE_DIR = "_archive"
MEMORY_TYPE_FOLDERS = {
    "project_memory": "project",
    "procedural_memory": "procedural",
    "experience_memory": "experience",
    "session_memory": "sessions",
}
FOLDER_MEMORY_TYPES = {folder: memory_type for memory_type, folder in MEMORY_TYPE_FOLDERS.items()}


@dataclass(frozen=True)
class MemoryItem:
    memory_id: str
    title: str
    body: str
    tags: list[str]
    source_path: str
    memory_type: str = "project_memory"
    subtype: str = ""
    source_run: str = ""
    source_run_id: str = ""
    source_trace_ids: list[str] = field(default_factory=list)
    source_step_ids: list[str] = field(default_factory=list)
    source_tool_names: list[str] = field(default_factory=list)
    source_modified_files: list[str] = field(default_factory=list)
    parent_memory_ids: list[str] = field(default_factory=list)
    created_at: str = ""
    updated_at: str = ""


@dataclass(frozen=True)
class MemorySearchResult:
    item: MemoryItem
    score: int
    reason: str


@dataclass(frozen=True)
class MemoryCandidate:
    memory_type: str
    title: str
    body: str
    subtype: str = ""
    tags: list[str] = field(default_factory=list)
    confidence: float = 0.0
    source_run: str = ""
    source_run_id: str = ""
    source_trace_ids: list[str] = field(default_factory=list)
    source_step_ids: list[str] = field(default_factory=list)
    source_tool_names: list[str] = field(default_factory=list)
    source_modified_files: list[str] = field(default_factory=list)
    parent_memory_ids: list[str] = field(default_factory=list)
    evidence: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class MemoryWriteResult:
    memory_id: str
    memory_type: str
    path: str
    index_path: str


@dataclass(frozen=True)
class MemoryArchiveResult:
    memory_id: str
    memory_type: str
    source_path: str
    archive_path: str
    reason: str


class FileMemoryStore:
    def __init__(self, workspace: Path, memory_dir: str = ".minicode/memory"):
        self.workspace = workspace.resolve()
        self.memory_dir = self._resolve_memory_dir(memory_dir)

    def search(self, query: str, limit: int = 5) -> list[MemorySearchResult]:
        limit = max(1, min(limit, 20))
        results: list[MemorySearchResult] = []
        for item in self.all():
            score, reason = _score_memory(query, item)
            if score > 0:
                results.append(MemorySearchResult(item=item, score=score, reason=reason))
        results = sorted(results, key=lambda result: (-result.score, result.item.memory_id))
        return results[:limit]

    def get(self, memory_id: str) -> MemoryItem | None:
        normalized_id = _normalize_id(memory_id)
        for item in self.all():
            if item.memory_id == normalized_id:
                return item
        return None

    def all(self, include_archived: bool = False) -> list[MemoryItem]:
        if not self.memory_dir.exists():
            return []

        items: list[MemoryItem] = []
        for path in sorted(self.memory_dir.rglob("*")):
            if path.suffix.lower() not in {".md", ".txt"} or not path.is_file():
                continue
            if not include_archived and MEMORY_ARCHIVE_DIR in path.relative_to(self.memory_dir).parts:
                continue
            item = _load_memory_item(path, self.memory_dir)
            items.append(item)
        return items

    def write_candidate(self, candidate: MemoryCandidate) -> MemoryWriteResult:
        memory_type = _normalize_memory_type(candidate.memory_type)
        folder = MEMORY_TYPE_FOLDERS[memory_type]
        now = datetime.now(timezone.utc).isoformat()
        memory_id = _candidate_id(candidate, now)
        directory = self.memory_dir / folder
        directory.mkdir(parents=True, exist_ok=True)
        path = _avoid_overwrite(directory / f"{_slugify(candidate.title)}.md")

        frontmatter = {
            "id": memory_id,
            "type": memory_type,
            "subtype": _slugify(candidate.subtype, limit=32) if candidate.subtype else "",
            "title": candidate.title.strip() or memory_id,
            "tags": [_slugify(tag, limit=32) for tag in candidate.tags if str(tag).strip()],
            "confidence": f"{_clamp_confidence(candidate.confidence):.2f}",
            "source_run": candidate.source_run,
            "source_run_id": candidate.source_run_id,
            "source_trace_ids": _clean_list(candidate.source_trace_ids, limit=32),
            "source_step_ids": _clean_list(candidate.source_step_ids, limit=32),
            "source_tool_names": _clean_list(candidate.source_tool_names, limit=32),
            "source_modified_files": _clean_list(candidate.source_modified_files, limit=32),
            "parent_memory_ids": _clean_list(candidate.parent_memory_ids, limit=32),
            "created_at": now,
            "updated_at": now,
            "evidence": [item.strip() for item in candidate.evidence if item.strip()][:5],
        }
        path.write_text(_render_memory_markdown(frontmatter, candidate.body), encoding="utf-8")
        index_path = self.update_index()
        return MemoryWriteResult(
            memory_id=memory_id,
            memory_type=memory_type,
            path=str(path),
            index_path=str(index_path),
        )

    def archive(self, memory_ids: list[str], reason: str) -> list[MemoryArchiveResult]:
        normalized_ids = {_normalize_id(memory_id) for memory_id in memory_ids if str(memory_id).strip()}
        if not normalized_ids:
            return []

        timestamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        archive_dir = self.memory_dir / MEMORY_ARCHIVE_DIR
        archive_dir.mkdir(parents=True, exist_ok=True)

        results: list[MemoryArchiveResult] = []
        for item in self.all():
            if item.memory_id not in normalized_ids:
                continue
            source = Path(item.source_path).resolve()
            if not _is_relative_to(source, self.memory_dir.resolve()):
                continue
            archive_path = _avoid_overwrite(archive_dir / f"{timestamp}-{source.name}")
            shutil.move(str(source), str(archive_path))
            results.append(
                MemoryArchiveResult(
                    memory_id=item.memory_id,
                    memory_type=item.memory_type,
                    source_path=str(source),
                    archive_path=str(archive_path),
                    reason=reason,
                )
            )

        if results:
            self._append_archive_log(results)
            self.update_index()
        return results

    def update_index(self) -> Path:
        self.memory_dir.mkdir(parents=True, exist_ok=True)
        path = self.memory_dir / "index.json"
        items = self.all()
        payload = {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "memory_count": len(items),
            "memories": [
                {
                    "id": item.memory_id,
                    "type": item.memory_type,
                    "subtype": item.subtype,
                    "title": item.title,
                    "tags": item.tags,
                    "path": _relative_or_absolute(Path(item.source_path), self.workspace),
                    "source_run": item.source_run,
                    "source_run_id": item.source_run_id,
                    "source_trace_ids": item.source_trace_ids,
                    "parent_memory_ids": item.parent_memory_ids,
                    "created_at": item.created_at,
                    "updated_at": item.updated_at,
                }
                for item in items
            ],
        }
        path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
        return path

    def _append_archive_log(self, results: list[MemoryArchiveResult]) -> None:
        path = self.memory_dir / MEMORY_ARCHIVE_DIR / "archive-log.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            for result in results:
                handle.write(json.dumps(result.__dict__, ensure_ascii=False) + "\n")

    def _resolve_memory_dir(self, raw_path: str) -> Path:
        path = Path(raw_path)
        if path.is_absolute():
            return path
        return self.workspace / path


class NullMemory:
    def search(self, query: str, limit: int = 5) -> list[MemorySearchResult]:
        return []

    def get(self, memory_id: str) -> MemoryItem | None:
        return None

    def all(self, include_archived: bool = False) -> list[MemoryItem]:
        return []

    def recall(self, task: str) -> str:
        return ""

    def remember(self, task: str, summary: str) -> None:
        return None


def _load_memory_item(path: Path, root: Path) -> MemoryItem:
    text = path.read_text(encoding="utf-8", errors="replace")
    metadata, body = _split_frontmatter(text)
    rel = path.relative_to(root).with_suffix("").as_posix()
    memory_id = _normalize_id(str(metadata.get("id") or rel))
    memory_type = _normalize_memory_type(str(metadata.get("type") or _infer_type_from_path(path, root)))
    subtype = str(metadata.get("subtype") or "")
    title = str(metadata.get("title") or _first_heading(body) or path.stem)
    tags = _as_list(metadata.get("tags"))
    source_run = str(metadata.get("source_run") or "")
    source_run_id = str(metadata.get("source_run_id") or source_run)
    created_at = str(metadata.get("created_at") or _mtime_iso(path))
    updated_at = str(metadata.get("updated_at") or created_at)
    return MemoryItem(
        memory_id=memory_id,
        title=title,
        body=body.strip(),
        tags=tags,
        source_path=str(path),
        memory_type=memory_type,
        subtype=subtype,
        source_run=source_run,
        source_run_id=source_run_id,
        source_trace_ids=_as_list(metadata.get("source_trace_ids")),
        source_step_ids=_as_list(metadata.get("source_step_ids")),
        source_tool_names=_as_list(metadata.get("source_tool_names")),
        source_modified_files=_as_list(metadata.get("source_modified_files")),
        parent_memory_ids=_as_list(metadata.get("parent_memory_ids")),
        created_at=created_at,
        updated_at=updated_at,
    )


def _score_memory(query: str, item: MemoryItem) -> tuple[int, str]:
    query_text = _normalize(query)
    if not query_text:
        return 0, ""

    title_text = _normalize(item.title)
    body_text = _normalize(item.body)
    tag_text = " ".join(_normalize(tag) for tag in item.tags)
    tokens = _tokens(query)
    score = 0
    reasons: list[str] = []

    for token in tokens:
        if token in title_text:
            score += 4
            reasons.append(f"title:{token}")
        if token in tag_text:
            score += 3
            reasons.append(f"tag:{token}")
        if len(token) >= 3 and token in body_text:
            score += 1
            reasons.append(f"body:{token}")

    if query_text in title_text:
        score += 6
        reasons.append("title_phrase")
    if query_text in body_text:
        score += 2
        reasons.append("body_phrase")

    if item.memory_type == "session_memory":
        weight = 0.75 if item.subtype == "session_summary" else 0.6
        score = max(1, int(round(score * weight)))
        reasons.append(f"type_weight:session_memory:{weight}")

    return score, ", ".join(reasons[:8])


def _split_frontmatter(text: str) -> tuple[dict[str, object], str]:
    if not text.startswith("---"):
        return {}, text
    parts = text.split("---", 2)
    if len(parts) < 3:
        return {}, text
    return _parse_frontmatter(parts[1]), parts[2]


def _parse_frontmatter(text: str) -> dict[str, object]:
    metadata: dict[str, object] = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or ":" not in line:
            continue
        key, raw_value = line.split(":", 1)
        value = raw_value.strip()
        if value.startswith("[") and value.endswith("]"):
            metadata[key.strip()] = _parse_inline_list(value)
        else:
            metadata[key.strip()] = value.strip("\"'")
    return metadata


def _parse_inline_list(value: str) -> list[str]:
    inner = value[1:-1].strip()
    if not inner:
        return []
    return [item.strip().strip("\"'") for item in inner.split(",") if item.strip()]


def _as_list(value: object) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item) for item in value]
    return [str(value)]


def _first_heading(text: str) -> str:
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            return stripped.lstrip("#").strip()
    return ""


def _normalize(value: str) -> str:
    text = unicodedata.normalize("NFKC", value).casefold()
    return re.sub(r"\s+", " ", text).strip()


def _tokens(value: str) -> list[str]:
    return [part for part in re.split(r"[^0-9A-Za-z_\u4e00-\u9fff]+", _normalize(value)) if part]


def _normalize_id(value: str) -> str:
    normalized = re.sub(r"[^0-9A-Za-z_.\-/]+", "-", value.strip()).strip("-/")
    return normalized or "memory"


def _normalize_memory_type(value: str) -> str:
    normalized = value.strip().lower()
    if normalized in MEMORY_TYPES:
        return normalized
    if normalized in FOLDER_MEMORY_TYPES:
        return FOLDER_MEMORY_TYPES[normalized]
    return "project_memory"


def _infer_type_from_path(path: Path, root: Path) -> str:
    parts = path.relative_to(root).parts
    for part in parts:
        if part in FOLDER_MEMORY_TYPES:
            return FOLDER_MEMORY_TYPES[part]
    return "project_memory"


def _candidate_id(candidate: MemoryCandidate, timestamp: str) -> str:
    memory_type = _normalize_memory_type(candidate.memory_type)
    prefix = {
        "project_memory": "proj",
        "procedural_memory": "proc",
        "experience_memory": "exp",
        "session_memory": "sess",
    }[memory_type]
    source_run = candidate.source_run_id or candidate.source_run
    digest_source = "|".join([candidate.title, candidate.subtype, candidate.body, source_run, timestamp])
    digest = hashlib.sha256(digest_source.encode("utf-8")).hexdigest()[:10]
    date = timestamp[:10].replace("-", "")
    return f"{prefix}_{date}_{digest}"


def _render_memory_markdown(metadata: dict[str, object], body: str) -> str:
    rows = ["---"]
    for key, value in metadata.items():
        rows.append(f"{key}: {_format_frontmatter_value(value)}")
    rows.extend(["---", "", body.strip(), ""])
    return "\n".join(rows)


def _format_frontmatter_value(value: object) -> str:
    if isinstance(value, list):
        return "[" + ", ".join(_quote_frontmatter_scalar(str(item)) for item in value) + "]"
    return _quote_frontmatter_scalar(str(value))


def _quote_frontmatter_scalar(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _slugify(value: str, limit: int = 60) -> str:
    slug = re.sub(r"[^0-9A-Za-z_.\-\u4e00-\u9fff]+", "-", value.strip()).strip("-._")
    return (slug or "memory")[:limit]


def _avoid_overwrite(path: Path) -> Path:
    if not path.exists():
        return path
    stem = path.stem
    suffix = path.suffix
    for index in range(1, 1000):
        candidate = path.with_name(f"{stem}-{index}{suffix}")
        if not candidate.exists():
            return candidate
    raise RuntimeError(f"could not find available memory filename for {path}")


def _clamp_confidence(value: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        parsed = 0.0
    return max(0.0, min(1.0, parsed))


def _clean_list(values: list[str], limit: int) -> list[str]:
    return [str(value).strip() for value in values if str(value).strip()][:limit]


def _relative_or_absolute(path: Path, root: Path) -> str:
    try:
        return path.resolve().relative_to(root).as_posix()
    except ValueError:
        return str(path)


def _mtime_iso(path: Path) -> str:
    return datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc).isoformat()


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True
