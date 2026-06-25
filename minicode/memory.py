from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class MemoryItem:
    memory_id: str
    title: str
    body: str
    tags: list[str]
    source_path: str


@dataclass(frozen=True)
class MemorySearchResult:
    item: MemoryItem
    score: int
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

    def all(self) -> list[MemoryItem]:
        if not self.memory_dir.exists():
            return []

        items: list[MemoryItem] = []
        for path in sorted(self.memory_dir.rglob("*")):
            if path.suffix.lower() not in {".md", ".txt"} or not path.is_file():
                continue
            item = _load_memory_item(path, self.memory_dir)
            items.append(item)
        return items

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

    def all(self) -> list[MemoryItem]:
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
    title = str(metadata.get("title") or _first_heading(body) or path.stem)
    tags = _as_list(metadata.get("tags"))
    return MemoryItem(
        memory_id=memory_id,
        title=title,
        body=body.strip(),
        tags=tags,
        source_path=str(path),
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
