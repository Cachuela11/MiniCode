from __future__ import annotations

import json
import re
from typing import Any


def parse_action(raw: str) -> dict[str, Any]:
    text = raw.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.startswith("json"):
            text = text[4:].strip()

    value = load_action_json(text)
    if value is None:
        salvaged_answer = extract_answer_from_malformed_json(text)
        if salvaged_answer:
            return {
                "thought": "The model returned malformed finish JSON; extracted args.answer.",
                "action": "finish",
                "args": {"answer": salvaged_answer},
            }
        return {
            "thought": "The model returned malformed JSON.",
            "action": "finish",
            "args": {"answer": raw.strip()},
        }

    if not isinstance(value, dict):
        raise ValueError("Agent action must be a JSON object.")
    if "action" not in value and "answer" in value:
        value = {
            "thought": value.get("thought", "The model returned a bare answer."),
            "action": "finish",
            "args": {"answer": value.get("answer", "")},
        }
    return value


def load_action_json(text: str) -> Any | None:
    decoder = json.JSONDecoder()
    candidates = [text]
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1 and end > start:
        candidates.append(text[start : end + 1])
    if start != -1:
        candidates.append(text[start:])

    for candidate in candidates:
        candidate = candidate.strip()
        if not candidate:
            continue
        for repaired in json_repair_candidates(candidate):
            try:
                return json.loads(repaired)
            except json.JSONDecodeError:
                pass
            try:
                value, _ = decoder.raw_decode(repaired)
                return value
            except json.JSONDecodeError:
                pass
    return None


def json_repair_candidates(text: str) -> list[str]:
    candidates = [text]
    balance = json_object_balance(text)
    if balance > 0 and balance <= 4:
        candidates.append(text + ("}" * balance))
    return candidates


def json_object_balance(text: str) -> int:
    balance = 0
    in_string = False
    escaped = False
    for char in text:
        if escaped:
            escaped = False
            continue
        if char == "\\" and in_string:
            escaped = True
            continue
        if char == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if char == "{":
            balance += 1
        elif char == "}":
            balance -= 1
    return balance


def extract_answer_from_malformed_json(text: str) -> str:
    match = re.search(r'"answer"\s*:\s*"((?:\\.|[^"\\])*)"', text, flags=re.DOTALL)
    if not match:
        return ""
    encoded = '"' + match.group(1) + '"'
    try:
        value = json.loads(encoded)
    except json.JSONDecodeError:
        value = match.group(1)
    return str(value).strip()


def extract_finish_answer(action: dict[str, Any], args: dict[str, Any]) -> str:
    answer = args.get("answer")
    if not answer:
        answer = action.get("answer")
    return str(answer or "The model finished without providing an answer.").strip()
