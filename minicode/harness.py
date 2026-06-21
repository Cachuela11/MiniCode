from __future__ import annotations

from dataclasses import dataclass


@dataclass
class Harness:
    """Placeholder for future task harness, tests, scoring, and repair loops."""

    name: str = "default"

    def describe(self) -> str:
        return "No harness is configured yet."
