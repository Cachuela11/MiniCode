from __future__ import annotations


class NullMemory:
    """Placeholder memory implementation used until persistent memory is added."""

    def recall(self, task: str) -> str:
        return ""

    def remember(self, task: str, summary: str) -> None:
        return None
