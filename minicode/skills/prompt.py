from __future__ import annotations

from .schema import SkillRoute


def render_skill_prompt(route: SkillRoute, max_chars_per_skill: int = 1600) -> str:
    if not route.selected:
        return "No specific skill selected. Use the available tools directly."

    sections: list[str] = []
    for selected in route.selected:
        skill = selected.skill
        body = skill.body
        if len(body) > max_chars_per_skill:
            body = body[: max_chars_per_skill - 3] + "..."
        sections.append(
            "\n".join(
                [
                    f"Skill: {skill.name}",
                    f"Why selected: {selected.reason}",
                    f"Description: {skill.description}",
                    f"Recommended tools: {', '.join(skill.tools) or 'none'}",
                    body,
                ]
            )
        )
    return "\n\n---\n\n".join(sections)
