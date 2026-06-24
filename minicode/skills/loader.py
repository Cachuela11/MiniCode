from __future__ import annotations

from pathlib import Path

from .schema import Skill


def load_skill(path: Path) -> Skill:
    text = path.read_text(encoding="utf-8")
    metadata, body = _split_frontmatter(text)
    return Skill(
        name=str(metadata.get("name") or path.stem),
        description=str(metadata.get("description") or ""),
        tags=_as_list(metadata.get("tags")),
        intents=_as_list(metadata.get("intents")),
        tools=_as_list(metadata.get("tools")),
        triggers=_as_list(metadata.get("triggers")),
        body=body.strip(),
        source_path=str(path),
    )


def load_skills(directory: Path) -> list[Skill]:
    if not directory.exists():
        return []
    return [load_skill(path) for path in sorted(directory.glob("*.md"))]


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
        key = key.strip()
        value = raw_value.strip()
        if value.startswith("[") and value.endswith("]"):
            metadata[key] = _parse_inline_list(value)
        else:
            metadata[key] = value.strip("\"'")
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
