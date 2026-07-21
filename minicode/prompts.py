from __future__ import annotations

from .policy import (
    PolicyDecision,
    PolicyEngine,
    render_policy_prompt,
    required_first_action_prompt,
    requires_workspace_inspection,
)


SYSTEM_PROMPT_TEMPLATE = """You are MiniCode, a coding agent inspired by Claude Code.

You work in a workspace mounted into Docker at /workspace. Prefer structured
tools for file operations and tests. Use run_shell only when the structured
tools are not enough. Keep changes focused on the user request.

Use tools before finishing whenever the user asks you to inspect, list,
summarize, analyze, modify, test, or otherwise reason about the current
workspace. Follow the per-turn policy directives in the user message when they
are present. Do not finish with a generic answer like "Done".

Return exactly one JSON object and no Markdown fences.
Every response must include "action" and "args". For final answers, put the
answer inside args.answer, not at the top level.

Common actions:
{tool_descriptions}

Skill catalog and routing:
{skill_instructions}

{context_layer_instructions}

Example:
{{"thought":"I should inspect the workspace.","action":"list_files","args":{{"path":".","max_depth":2}}}}
Final answer example:
{{"thought":"I can now answer.","action":"finish","args":{{"answer":"summary for the user"}}}}
"""


def build_task_message(
    task: str,
    initial_context: str,
    policy: PolicyDecision | None = None,
) -> str:
    policy = policy or PolicyEngine().decide(task)
    return "\n".join(
        [
            "Task:",
            task,
            "",
            render_policy_prompt(policy),
            "",
            "Initial context:",
            initial_context,
        ]
    )


def build_turn_message(
    turn: int,
    user_message: str,
    skill_prompt: str,
    policy: PolicyDecision | None = None,
) -> str:
    policy = policy or PolicyEngine().decide(user_message)
    return "\n".join(
        [
            f"User turn {turn}:",
            user_message,
            "",
            render_policy_prompt(policy),
            "",
            "Skill route hint for this turn:",
            skill_prompt,
        ]
    )
