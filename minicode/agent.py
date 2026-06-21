from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from .context import build_initial_context
from .llm import OllamaClient
from .sandbox import DockerSandbox
from .tools import ToolRegistry


SYSTEM_PROMPT_TEMPLATE = """You are MiniCode, a coding agent inspired by Claude Code.

You work in a workspace mounted into Docker at /workspace. Prefer structured
tools for file operations and tests. Use run_shell only when the structured
tools are not enough. Keep changes focused on the user request.

Return exactly one JSON object and no Markdown fences.

Available actions:
{tool_descriptions}

Example:
{{"thought":"I should inspect the workspace.","action":"list_files","args":{{"path":".","max_depth":2}}}}
"""


@dataclass
class AgentConfig:
    model: str
    max_steps: int = 8


@dataclass
class AgentResult:
    answer: str
    steps: int
    transcript: list[dict[str, Any]] = field(default_factory=list)


class CodingAgent:
    def __init__(
        self,
        llm: OllamaClient,
        sandbox: DockerSandbox,
        config: AgentConfig,
        tools: ToolRegistry | None = None,
    ):
        self.llm = llm
        self.sandbox = sandbox
        self.config = config
        self.tools = tools or ToolRegistry(workspace=sandbox.workspace, sandbox=sandbox)

    def run(self, task: str) -> AgentResult:
        messages: list[dict[str, str]] = [
            {
                "role": "system",
                "content": SYSTEM_PROMPT_TEMPLATE.format(tool_descriptions=self.tools.describe()),
            },
            {
                "role": "user",
                "content": f"Task:\n{task}\n\nInitial context:\n{build_initial_context(self.sandbox)}",
            },
        ]
        transcript: list[dict[str, Any]] = []

        for step in range(1, self.config.max_steps + 1):
            raw = self.llm.chat(model=self.config.model, messages=messages)
            action = _parse_action(raw)
            transcript.append({"step": step, "model": raw, "action": action})

            name = action.get("action")
            args = action.get("args") or {}
            if not isinstance(args, dict):
                args = {}

            if name == "finish":
                return AgentResult(
                    answer=str(args.get("answer", "")).strip() or "Done.",
                    steps=step,
                    transcript=transcript,
                )

            tool_result = self.tools.execute(str(name), args)
            observation = tool_result.output

            messages.append({"role": "assistant", "content": json.dumps(action)})
            messages.append({"role": "user", "content": f"Observation:\n{observation}"})

        return AgentResult(
            answer=f"Stopped after {self.config.max_steps} steps without finish.",
            steps=self.config.max_steps,
            transcript=transcript,
        )


def _parse_action(raw: str) -> dict[str, Any]:
    text = raw.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.startswith("json"):
            text = text[4:].strip()

    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start == -1 or end == -1 or end <= start:
            return {
                "thought": "The model did not return JSON.",
                "action": "finish",
                "args": {"answer": raw.strip()},
            }
        value = json.loads(text[start : end + 1])

    if not isinstance(value, dict):
        raise ValueError("Agent action must be a JSON object.")
    return value
