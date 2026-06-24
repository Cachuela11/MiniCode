from __future__ import annotations

import re
import unicodedata


def contains(text: str, needle: str) -> bool:
    normalized = normalize(needle)
    return bool(normalized and normalized in text)


def normalize(value: str) -> str:
    text = unicodedata.normalize("NFKC", value).casefold()
    return re.sub(r"\s+", " ", text).strip()


def tokenize(value: str) -> list[str]:
    return [part for part in re.split(r"[^0-9A-Za-z_]+", normalize(value)) if part]
