from __future__ import annotations

import re
import json
import hashlib
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path


MEMORY_TYPES = {"project_memory", "procedural_memory", "experience_memory", "session_memory"}
MEMORY_STATUSES = {"draft", "active"}
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
    status: str = "active"


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
    tags: list[str] = field(default_factory=list)
    confidence: float = 0.0
    source_run: str = ""
    evidence: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class MemoryWriteResult:
    memory_id: str
    status: str
    memory_type: str
    path: str
    index_path: str


class FileMemoryStore:
    def __init__(self, workspace: Path, memory_dir: str = ".minicode/memory"):
        self.workspace = workspace.resolve()
        self.memory_dir = self._resolve_memory_dir(memory_dir)

    def search(self, query: str, limit: int = 5, include_drafts: bool = False) -> list[MemorySearchResult]:
        limit = max(1, min(limit, 20))
        results: list[MemorySearchResult] = []
        for item in self.all(include_drafts=include_drafts):
            score, reason = _score_memory(query, item)
            if score > 0:
                results.append(MemorySearchResult(item=item, score=score, reason=reason))
        results = sorted(results, key=lambda result: (-result.score, result.item.memory_id))
        return results[:limit]

    def get(self, memory_id: str, include_drafts: bool = False) -> MemoryItem | None:
        normalized_id = _normalize_id(memory_id)
        for item in self.all(include_drafts=include_drafts):
            if item.memory_id == normalized_id:
                return item
        return None

    def all(self, include_drafts: bool = False) -> list[MemoryItem]:
        if not self.memory_dir.exists():
            return []

        items: list[MemoryItem] = []
        for path in sorted(self.memory_dir.rglob("*")):
            if path.suffix.lower() not in {".md", ".txt"} or not path.is_file():
                continue
            item = _load_memory_item(path, self.memory_dir)
            if item.status == "draft" and not include_drafts:
                continue
            items.append(item)
        return items

    def write_candidate(self, candidate: MemoryCandidate, status: str = "draft") -> MemoryWriteResult:
        status = _normalize_status(status)
        memory_type = _normalize_memory_type(candidate.memory_type)
        folder = MEMORY_TYPE_FOLDERS[memory_type]
        now = datetime.now(timezone.utc).isoformat()
        memory_id = _candidate_id(candidate, now)
        if memory_type == "session_memory":
            status = "active"
        directory = self.memory_dir / ("_drafts" if status == "draft" else folder)
        if status == "draft":
            directory = directory / folder
        directory.mkdir(parents=True, exist_ok=True)
        path = _avoid_overwrite(directory / f"{_slugify(candidate.title)}.md")

        frontmatter = {
            "id": memory_id,
            "type": memory_type,
            "status": status,
            "title": candidate.title.strip() or memory_id,
            "tags": [_slugify(tag, limit=32) for tag in candidate.tags if str(tag).strip()],
            "confidence": f"{_clamp_confidence(candidate.confidence):.2f}",
            "source_run": candidate.source_run,
            "created_at": now,
            "updated_at": now,
            "evidence": [item.strip() for item in candidate.evidence if item.strip()][:5],
        }
        path.write_text(_render_memory_markdown(frontmatter, candidate.body), encoding="utf-8")
        index_path = self.update_index()
        return MemoryWriteResult(
            memory_id=memory_id,
            status=status,
            memory_type=memory_type,
            path=str(path),
            index_path=str(index_path),
        )

    def update_index(self) -> Path:
        self.memory_dir.mkdir(parents=True, exist_ok=True)
        path = self.memory_dir / "index.json"
        items = self.all(include_drafts=True)
        payload = {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "memory_count": len(items),
            "memories": [
                {
                    "id": item.memory_id,
                    "type": item.memory_type,
                    "status": item.status,
                    "title": item.title,
                    "tags": item.tags,
                    "path": _relative_or_absolute(Path(item.source_path), self.workspace),
                }
                for item in items
            ],
        }
        path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
        return path

    def _resolve_memory_dir(self, raw_path: str) -> Path:
        path = Path(raw_path)
        if path.is_absolute():
            return path
        return self.workspace / path


class NullMemory:
    def search(self, query: str, limit: int = 5, include_drafts: bool = False) -> list[MemorySearchResult]:
        return []

    def get(self, memory_id: str, include_drafts: bool = False) -> MemoryItem | None:
        return None

    def all(self, include_drafts: bool = False) -> list[MemoryItem]:
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
    status = _normalize_status(str(metadata.get("status") or _infer_status_from_path(path, root)))
    title = str(metadata.get("title") or _first_heading(body) or path.stem)
    tags = _as_list(metadata.get("tags"))
    return MemoryItem(
        memory_id=memory_id,
        title=title,
        body=body.strip(),
        tags=tags,
        source_path=str(path),
        memory_type=memory_type,
        status=status,
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
        score = max(1, int(round(score * 0.6)))
        reasons.append("type_weight:session_memory:0.6")

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


def _normalize_status(value: str) -> str:
    normalized = value.strip().lower()
    if normalized in MEMORY_STATUSES:
        return normalized
    return "draft"


def _infer_type_from_path(path: Path, root: Path) -> str:
    parts = path.relative_to(root).parts
    for part in parts:
        if part in FOLDER_MEMORY_TYPES:
            return FOLDER_MEMORY_TYPES[part]
    return "project_memory"


def _infer_status_from_path(path: Path, root: Path) -> str:
    return "draft" if "_drafts" in path.relative_to(root).parts else "active"


def _candidate_id(candidate: MemoryCandidate, timestamp: str) -> str:
    memory_type = _normalize_memory_type(candidate.memory_type)
    prefix = {
        "project_memory": "proj",
        "procedural_memory": "proc",
        "experience_memory": "exp",
        "session_memory": "sess",
    }[memory_type]
    digest_source = "|".join([candidate.title, candidate.body, candidate.source_run, timestamp])
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


def _relative_or_absolute(path: Path, root: Path) -> str:
    try:
        return path.resolve().relative_to(root).as_posix()
    except ValueError:
        return str(path)
