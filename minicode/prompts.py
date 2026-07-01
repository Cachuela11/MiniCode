from __future__ import annotations


SYSTEM_PROMPT_TEMPLATE = """You are MiniCode, a coding agent inspired by Claude Code.

You work in a workspace mounted into Docker at /workspace. Prefer structured
tools for file operations and tests. Use run_shell only when the structured
tools are not enough. Keep changes focused on the user request.

Use tools before finishing whenever the user asks you to inspect, list,
summarize, analyze, modify, test, or otherwise reason about the current
workspace. For project structure questions, call list_files first and read
relevant files when needed. Do not finish with a generic answer like "Done".
When answering project structure or file listing questions, format the final
answer with readable line breaks as a short tree or bullet list. Do not put the
whole directory structure on one long line.

Return exactly one JSON object and no Markdown fences.
Every response must include "action" and "args". For final answers, put the
answer inside args.answer, not at the top level.

Available actions:
{tool_descriptions}

Relevant skills:
{skill_instructions}

{context_layer_instructions}

Example:
{{"thought":"I should inspect the workspace.","action":"list_files","args":{{"path":".","max_depth":2}}}}
Final answer example:
{{"thought":"I can now answer.","action":"finish","args":{{"answer":"summary for the user"}}}}
"""


def build_task_message(task: str, initial_context: str) -> str:
    parts = [
        "Task:",
        task,
    ]
    requirement = required_first_action_prompt(task)
    if requirement:
        parts.extend(["", requirement])
    parts.extend(["", "Initial context:", initial_context])
    return "\n".join(parts)


def build_turn_message(turn: int, user_message: str, skill_prompt: str) -> str:
    parts = [
        f"User turn {turn}:",
        user_message,
    ]
    requirement = required_first_action_prompt(user_message)
    if requirement:
        parts.extend(["", requirement])
    parts.extend(
        [
            "",
            "Relevant skills for this turn:",
            skill_prompt,
        ]
    )
    return "\n".join(parts)


def required_first_action_prompt(value: str) -> str:
    if not requires_workspace_inspection(value):
        return ""
    return (
        "Mandatory first action for this request:\n"
        "Your next assistant response must call list_files before any finish or other action, "
        "even if Initial context already contains a file index.\n"
        "Return this action shape:\n"
        '{"thought":"I need fresh workspace structure.","action":"list_files",'
        '"args":{"path":".","max_depth":2,"limit":200}}\n'
        "After receiving the Observation, answer the user or call more tools if needed."
    )


def requires_workspace_inspection(value: str) -> bool:
    text = value.lower()
    needles = [
        "项目结构",
        "目录结构",
        "文件结构",
        "当前项目",
        "当前目录",
        "查看项目",
        "查看一下",
        "看一下",
        "列出",
        "有哪些文件",
        "project structure",
        "workspace structure",
        "inspect workspace",
        "inspect project",
        "list files",
        "summarize this project",
        "summarize project",
    ]
    return any(needle in text for needle in needles)
