from __future__ import annotations

import json
from dataclasses import asdict
from typing import Any

from .matching import contains, normalize, tokenize
from .schema import RankResult, RecalledSkill, SelectedSkill, Skill


class RuleBasedSkillRanker:
    def __init__(self, max_skills: int = 2):
        self.max_skills = max(0, max_skills)

    def rank(self, task: str, candidates: list[RecalledSkill]) -> list[SelectedSkill]:
        if self.max_skills == 0:
            return []

        ranked: list[SelectedSkill] = []
        needed_tools = _infer_needed_tools(task)
        for candidate in candidates:
            score, reason = _rank_score(task, candidate, needed_tools)
            ranked.append(
                SelectedSkill(
                    skill=candidate.skill,
                    score=score,
                    reason=reason,
                    recall_score=candidate.score,
                )
            )

        ranked = sorted(ranked, key=lambda item: (-item.score, -item.recall_score, item.skill.name))
        return ranked[: self.max_skills]


class LlmSkillRanker:
    def __init__(self, llm: Any, model: str, max_skills: int = 2):
        self.llm = llm
        self.model = model
        self.max_skills = max(0, max_skills)

    def rank(self, task: str, candidates: list[RecalledSkill]) -> RankResult:
        if self.max_skills == 0 or not candidates:
            return RankResult(intent="general", reranker="deepseek")

        try:
            response = self.llm.chat_response(
                model=self.model,
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "You rerank MiniCode skills for a coding agent. "
                            "Return exactly one JSON object with this shape: "
                            '{"intent":"short_intent","selected":[{"name":"skill_name","score":0,"reason":"why"}]}. '
                            "Select only skills from the provided candidates. Select at most the requested number. "
                            "If no skill is useful, return an empty selected list."
                        ),
                    },
                    {
                        "role": "user",
                        "content": json.dumps(
                            {
                                "task": task,
                                "max_skills": self.max_skills,
                                "candidates": [_candidate_for_llm(candidate) for candidate in candidates],
                            },
                            ensure_ascii=False,
                        ),
                    },
                ],
            )
            payload = _parse_json_object(response.content)
            selected = _selected_from_payload(payload, candidates, self.max_skills)
            intent = str(payload.get("intent") or _infer_intent(selected))
            return RankResult(
                intent=intent,
                selected=selected,
                reranker="deepseek",
                token_usage=asdict(response.token_usage),
            )
        except Exception as exc:
            fallback = RuleBasedSkillRanker(max_skills=self.max_skills).rank(task, candidates)
            return RankResult(
                intent=_infer_intent(fallback),
                selected=fallback,
                reranker="rule_fallback",
                error=str(exc),
            )


def _rank_score(task: str, candidate: RecalledSkill, needed_tools: set[str]) -> tuple[int, str]:
    skill = candidate.skill
    task_text = normalize(task)
    score = candidate.score
    reasons = [f"recall:{candidate.score}({candidate.reason})"]

    trigger_hits = [trigger for trigger in skill.triggers if contains(task_text, trigger)]
    if trigger_hits:
        bonus = 6 * len(trigger_hits)
        score += bonus
        reasons.append(f"trigger_bonus:{bonus}")

    intent_hits = [intent for intent in skill.intents if contains(task_text, intent.replace("_", " "))]
    if intent_hits:
        bonus = 4 * len(intent_hits)
        score += bonus
        reasons.append(f"intent_bonus:{bonus}")

    tag_hits = [tag for tag in skill.tags if contains(task_text, tag)]
    if tag_hits:
        bonus = 2 * len(tag_hits)
        score += bonus
        reasons.append(f"tag_bonus:{bonus}")

    tool_overlap = sorted(needed_tools & set(skill.tools))
    if tool_overlap:
        bonus = 3 * len(tool_overlap)
        score += bonus
        reasons.append(f"tool_overlap:{','.join(tool_overlap)}")

    body_hits = _body_keyword_hits(task, skill)
    if body_hits:
        bonus = min(3, body_hits)
        score += bonus
        reasons.append(f"body_keyword_bonus:{bonus}")

    return score, "; ".join(reasons)


def _infer_needed_tools(task: str) -> set[str]:
    task_text = normalize(task)
    needed = {"read_file", "list_files"}

    test_keywords = ["\u6d4b\u8bd5", "test", "pytest", "unittest", "\u5931\u8d25", "failure"]
    edit_keywords = [
        "\u65b0\u589e",
        "\u521b\u5efa",
        "\u5199\u5165",
        "\u5b9e\u73b0",
        "\u4fee\u6539",
        "\u4fee\u590d",
        "fix",
        "add",
        "create",
    ]
    refactor_keywords = ["\u91cd\u6784", "refactor", "cleanup"]
    review_keywords = ["review", "\u5ba1\u67e5"]
    shell_keywords = ["shell", "\u547d\u4ee4", "command"]

    if any(keyword in task_text for keyword in test_keywords):
        needed.update({"run_tests", "read_file", "write_file"})
    if any(keyword in task_text for keyword in edit_keywords):
        needed.update({"write_file", "read_file"})
    if any(keyword in task_text for keyword in refactor_keywords):
        needed.update({"read_file", "write_file", "run_tests"})
    if any(keyword in task_text for keyword in review_keywords):
        needed.update({"read_file", "list_files"})
    if any(keyword in task_text for keyword in shell_keywords):
        needed.add("run_shell")

    return needed


def _body_keyword_hits(task: str, skill: Skill) -> int:
    body_text = normalize(skill.body)
    hits = 0
    for token in tokenize(task):
        if len(token) >= 4 and token in body_text:
            hits += 1
    return hits


def _candidate_for_llm(candidate: RecalledSkill) -> dict[str, Any]:
    skill = candidate.skill
    return {
        "name": skill.name,
        "description": skill.description,
        "tags": skill.tags,
        "intents": skill.intents,
        "tools": skill.tools,
        "triggers": skill.triggers,
        "recall_score": candidate.score,
        "recall_reason": candidate.reason,
        "body_excerpt": skill.body[:800],
    }


def _parse_json_object(content: str) -> dict[str, Any]:
    text = content.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.startswith("json"):
            text = text[4:].strip()
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start == -1 or end == -1 or end <= start:
            raise
        payload = json.loads(text[start : end + 1])
    if not isinstance(payload, dict):
        raise ValueError("LLM reranker response must be a JSON object.")
    return payload


def _selected_from_payload(
    payload: dict[str, Any],
    candidates: list[RecalledSkill],
    max_skills: int,
) -> list[SelectedSkill]:
    candidate_by_name = {candidate.skill.name: candidate for candidate in candidates}
    selected: list[SelectedSkill] = []
    raw_selected = payload.get("selected") or []
    if not isinstance(raw_selected, list):
        return selected

    for item in raw_selected:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "")
        candidate = candidate_by_name.get(name)
        if candidate is None:
            continue
        score = _safe_int(item.get("score"), default=candidate.score)
        reason = str(item.get("reason") or "selected by LLM reranker")
        selected.append(
            SelectedSkill(
                skill=candidate.skill,
                score=score,
                reason=f"llm:{reason}; recall:{candidate.score}({candidate.reason})",
                recall_score=candidate.score,
            )
        )
        if len(selected) >= max_skills:
            break
    return selected


def _safe_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _infer_intent(selected: list[SelectedSkill]) -> str:
    if selected and selected[0].skill.intents:
        return selected[0].skill.intents[0]
    return "general"
