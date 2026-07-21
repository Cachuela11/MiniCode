from __future__ import annotations

from .catalog import SkillCatalog
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


def render_skill_catalog_prompt(catalog: SkillCatalog, route: SkillRoute | None = None, max_skills: int = 50) -> str:
    skills = catalog.all()[:max_skills]
    if not skills:
        return (
            "No skills are installed. Use common tools directly, and call search_tools when you need "
            "an extended tool schema."
        )

    selected = {item.skill.name for item in route.selected} if route else set()
    rows = [
        "Skill catalog summaries. These are summaries only, not the full workflows.",
        "If a listed skill matches the task, call load_skill with its name before following the workflow.",
        "If no skill matches, choose common tools directly or call search_tools for extra tool schemas.",
        "",
    ]
    for skill in skills:
        marker = "selected_hint" if skill.name in selected else "available"
        rows.extend(
            [
                f"- name: {skill.name}",
                f"  status: {marker}",
                f"  description: {skill.description}",
                f"  intents: {', '.join(skill.intents) or 'none'}",
                f"  triggers: {', '.join(skill.triggers) or 'none'}",
                f"  common_tools: {', '.join(skill.tools) or 'none'}",
            ]
        )
    if route and route.selected:
        rows.append("")
        rows.append("Router hint: likely relevant skills: " + ", ".join(item.skill.name for item in route.selected))
    return "\n".join(rows)


def render_skill_route_prompt(route: SkillRoute) -> str:
    if not route.selected:
        return "No specific skill selected for this turn. Use common tools directly or call search_tools."
    rows = [
        "Skill route hint for this turn:",
        "The router found possible skills. Call load_skill for the matching skill before following its workflow.",
    ]
    for item in route.selected:
        rows.append(
            "\n".join(
                [
                    f"- name: {item.skill.name}",
                    f"  reason: {item.reason}",
                    f"  recommended_tools: {', '.join(item.skill.tools) or 'none'}",
                ]
            )
        )
    return "\n".join(rows)
