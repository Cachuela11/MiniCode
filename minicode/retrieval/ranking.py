from __future__ import annotations

import re
import unicodedata


def normalize(value: str) -> str:
    text = unicodedata.normalize("NFKC", value).casefold()
    return re.sub(r"\s+", " ", text).strip()


def tokenize(value: str) -> list[str]:
    return [part for part in re.split(r"[^0-9A-Za-z_\u4e00-\u9fff]+", normalize(value)) if part]


def split_reasons(reason: str) -> list[str]:
    return [part.strip() for part in reason.split(",") if part.strip()]
