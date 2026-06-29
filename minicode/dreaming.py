from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol

from .memory import FileMemoryStore, MemoryArchiveResult, MemoryCandidate, MemoryItem, MemoryWriteResult
from .observability import TokenUsage


DREAMING_MODES = {"off", "auto"}
LONG_TERM_MEMORY_TYPES = {"project_memory", "procedural_memory", "experience_memory"}
MEMORY_LAYER_ORDER = ["session_memory", "project_memory", "procedural_memory", "experience_memory"]
NEXT_MEMORY_LAYER = {
    "session_memory": "project_memory",
    "project_memory": "procedural_memory",
    "procedural_memory": "experience_memory",
}
SESSION_SUMMARY_SUBTYPE = "session_summary"


@dataclass(frozen=True)
class DreamingConfig:
    mode: str = "auto"
    session_threshold: int = 8
    session_token_threshold: int = 12000
    memory_threshold: int = 40
    memory_token_threshold: int = 12000
    interval_hours: int = 24
    max_batch_size: int = 20
    min_confidence: float = 0.75
    session_hot_days: float = 2.0


@dataclass(frozen=True)
class DreamingDecision:
    triggered: bool
    reason: str
    metrics: dict[str, Any]


@dataclass
class DreamingResult:
    mode: str
    status: str
    triggered: bool = False
    reason: str = ""
    metrics: dict[str, Any] = field(default_factory=dict)
    written: list[MemoryWriteResult] = field(default_factory=list)
    archived: list[MemoryArchiveResult] = field(default_factory=list)
    skipped: list[dict[str, Any]] = field(default_factory=list)
    layers: list[dict[str, Any]] = field(default_factory=list)
    token_usage: TokenUsage = field(default_factory=TokenUsage)
    state_path: str = ""
    error: str = ""

    def to_log_dict(self) -> dict[str, Any]:
        return asdict(self)


class ChatClient(Protocol):
    def chat_response(self, model: str, messages: list[dict[str, str]]):
        ...


class MemoryDreamer:
    """Offline memory maintenance.

    First version:
    - trigger by force, new session count, active memory pressure, time interval, or exact duplicates
    - archive exact duplicate memories deterministically
    - ask the LLM to summarize old session memories into active session summaries
    - separately ask the LLM for durable long-term memory candidates
    """

    def __init__(
        self,
        llm: ChatClient,
        model: str,
        memory_store: FileMemoryStore,
        config: DreamingConfig | None = None,
    ):
        self.llm = llm
        self.model = model
        self.memory_store = memory_store
        self.config = config or DreamingConfig()
        self.mode = _normalize_mode(self.config.mode)
        self.state_path = self.memory_store.memory_dir / "dreaming-state.json"

    def check_trigger(self, force: bool = False) -> DreamingDecision:
        items = self.memory_store.all()
        state = _read_state(self.state_path)
        eligible_by_layer = self._eligible_items_by_layer(items, state)
        duplicate_groups_by_layer = {
            layer: _exact_duplicate_groups(items)
            for layer, items in eligible_by_layer.items()
        }
        hours_since_last = _hours_since(state.get("last_dream_at"))
        has_last_dream = bool(state.get("last_dream_at"))
        layer_metrics = {
            layer: _layer_metrics(
                layer=layer,
                items=eligible_by_layer[layer],
                duplicate_groups=duplicate_groups_by_layer[layer],
                config=self.config,
                hours_since_last=hours_since_last,
                has_last_dream=has_last_dream,
            )
            for layer in MEMORY_LAYER_ORDER
        }

        metrics = {
            "active_memory_count": len(items),
            "session_memory_count": sum(1 for item in items if item.memory_type == "session_memory"),
            "eligible_memory_count": sum(len(items) for items in eligible_by_layer.values()),
            "exact_duplicate_group_count": sum(len(groups) for groups in duplicate_groups_by_layer.values()),
            "hours_since_last_dream": hours_since_last,
            "session_threshold": self.config.session_threshold,
            "session_token_threshold": self.config.session_token_threshold,
            "memory_threshold": self.config.memory_threshold,
            "memory_token_threshold": self.config.memory_token_threshold,
            "interval_hours": self.config.interval_hours,
            "session_hot_days": self.config.session_hot_days,
            "layers": layer_metrics,
        }

        if self.mode == "off" and not force:
            return DreamingDecision(False, "dreaming_off", metrics)
        if force:
            return DreamingDecision(True, "forced", metrics)
        for trigger in ["duplicates", "count", "tokens", "time"]:
            triggered_layers = [
                layer
                for layer, layer_metric in layer_metrics.items()
                if layer_metric["triggers"][trigger]
            ]
            if triggered_layers:
                return DreamingDecision(True, f"{trigger}_threshold:{','.join(triggered_layers)}", metrics)
        return DreamingDecision(False, "below_threshold", metrics)

    def run(self, force: bool = False) -> DreamingResult:
        decision = self.check_trigger(force=force)
        result = DreamingResult(
            mode="manual" if force else self.mode,
            status="started" if decision.triggered else "skipped",
            triggered=decision.triggered,
            reason=decision.reason,
            metrics=decision.metrics,
            state_path=str(self.state_path),
        )
        if not decision.triggered:
            return result

        state = _read_state(self.state_path)
        try:
            eligible_by_layer = self._eligible_items_by_layer(self.memory_store.all(), state)
            duplicate_archive_ids = _unique(
                [
                    memory_id
                    for layer_items in eligible_by_layer.values()
                    for memory_id in _duplicate_archive_ids(layer_items)
                ]
            )
            if duplicate_archive_ids:
                result.archived.extend(
                    self.memory_store.archive(duplicate_archive_ids, reason="dreaming_exact_duplicate")
                )

            layers_to_process = self._layers_to_process(state, force=force)
            if not layers_to_process:
                result.status = "completed_dedup_only" if result.archived else "completed_noop"
                self._write_state(state, processed_items=[], result=result)
                return result

            processed_items: list[MemoryItem] = []
            for layer in layers_to_process:
                batch = self._select_layer_batch(layer, state)
                if not batch:
                    result.layers.append({"layer": layer, "status": "skipped_empty"})
                    continue

                candidates, token_usage, skipped, archive_ids = self._reflect_layer(layer, batch)
                result.token_usage.add(token_usage)
                result.skipped.extend(skipped)

                layer_written: list[MemoryWriteResult] = []
                session_summary_parent_ids: set[str] = set()
                written_parent_ids: set[str] = set()
                for candidate in candidates:
                    if candidate.confidence < _clamp(self.config.min_confidence):
                        result.skipped.append(
                            {
                                "layer": layer,
                                "title": candidate.title,
                                "type": candidate.memory_type,
                                "reason": "confidence_below_threshold",
                                "confidence": candidate.confidence,
                            }
                        )
                        continue
                    if self._looks_duplicate(candidate):
                        result.skipped.append(
                            {
                                "layer": layer,
                                "title": candidate.title,
                                "type": candidate.memory_type,
                                "reason": "possible_duplicate",
                                "confidence": candidate.confidence,
                            }
                        )
                        continue
                    write_result = self.memory_store.write_candidate(candidate)
                    result.written.append(write_result)
                    layer_written.append(write_result)
                    written_parent_ids.update(candidate.parent_memory_ids)
                    if candidate.memory_type == "session_memory" and candidate.subtype == SESSION_SUMMARY_SUBTYPE:
                        session_summary_parent_ids.update(candidate.parent_memory_ids)

                semantic_archive_ids = [
                    memory_id
                    for memory_id in archive_ids
                    if memory_id in written_parent_ids and _is_archivable_same_layer(memory_id, batch, layer)
                ]
                session_archive_ids = [
                    memory_id
                    for memory_id in session_summary_parent_ids
                    if _is_archivable_raw_session(memory_id, batch, self.config.session_hot_days)
                ]
                if semantic_archive_ids:
                    result.archived.extend(
                        self.memory_store.archive(semantic_archive_ids, reason=f"dreaming_{layer}_semantic_merge")
                    )
                archived_session_ids: set[str] = set()
                if session_archive_ids:
                    session_archive_results = self.memory_store.archive(
                        session_archive_ids,
                        reason="dreaming_session_summary",
                    )
                    result.archived.extend(session_archive_results)
                    archived_session_ids = {item.memory_id for item in session_archive_results}

                processed_items.extend(
                    item
                    for item in batch
                    if (item.memory_type in LONG_TERM_MEMORY_TYPES and item.memory_type == layer)
                    or item.memory_id in archived_session_ids
                )
                result.layers.append(
                    {
                        "layer": layer,
                        "status": "completed",
                        "batch_count": len(batch),
                        "written": [item.memory_id for item in layer_written],
                        "archive_request_count": len(archive_ids),
                        "token_usage": asdict(token_usage),
                    }
                )

            result.status = "completed"
            self._write_state(state, processed_items=processed_items, result=result)
        except Exception as exc:
            result.status = "error"
            result.error = str(exc)
        return result

    def _eligible_items_by_layer(self, items: list[MemoryItem], state: dict[str, Any]) -> dict[str, list[MemoryItem]]:
        processed_session_ids = set(_as_string_list(state.get("processed_session_ids")))
        processed_memory_ids = set(_as_string_list(state.get("processed_memory_ids")))

        eligible: dict[str, list[MemoryItem]] = {layer: [] for layer in MEMORY_LAYER_ORDER}
        for item in items:
            if item.memory_type == "session_memory":
                if (
                    _is_raw_session(item)
                    and item.memory_id not in processed_session_ids
                    and _is_older_than_days(item, self.config.session_hot_days)
                ):
                    eligible["session_memory"].append(item)
                continue
            if item.memory_type in LONG_TERM_MEMORY_TYPES and item.memory_id not in processed_memory_ids:
                eligible[item.memory_type].append(item)
        return eligible

    def _layers_to_process(self, state: dict[str, Any], force: bool) -> list[str]:
        eligible_by_layer = self._eligible_items_by_layer(self.memory_store.all(), state)
        if force:
            return [layer for layer in MEMORY_LAYER_ORDER if eligible_by_layer[layer]]

        decision = self.check_trigger(force=False)
        layer_metrics = decision.metrics.get("layers", {})
        layers: list[str] = []
        if isinstance(layer_metrics, dict):
            for layer in MEMORY_LAYER_ORDER:
                metrics = layer_metrics.get(layer, {})
                triggers = metrics.get("triggers", {}) if isinstance(metrics, dict) else {}
                if isinstance(triggers, dict) and any(bool(value) for value in triggers.values()):
                    layers.append(layer)
        return layers

    def _select_layer_batch(self, layer: str, state: dict[str, Any]) -> list[MemoryItem]:
        items = self.memory_store.all()
        limit = max(1, self.config.max_batch_size)

        eligible = self._eligible_items_by_layer(items, state).get(layer, [])
        if layer == "session_memory":
            return eligible[:limit]
        return _diverse_same_layer(eligible, limit)

    def _reflect_layer(
        self,
        layer: str,
        batch: list[MemoryItem],
    ) -> tuple[list[MemoryCandidate], TokenUsage, list[dict[str, Any]], list[str]]:
        batch_by_id = {item.memory_id: item for item in batch}
        messages = [
            {
                "role": "system",
                "content": _dreaming_prompt(
                    layer=layer,
                    next_layer=NEXT_MEMORY_LAYER.get(layer),
                    max_candidates=max(1, self.config.max_batch_size // 2),
                ),
            },
            {
                "role": "user",
                "content": json.dumps(
                    {
                        "current_layer": layer,
                        "next_layer": NEXT_MEMORY_LAYER.get(layer),
                        "memory_batch": [_serialize_memory(item) for item in batch],
                        "instructions": [
                            "First consolidate the current layer when useful.",
                            "Then judge whether the consolidated result should be promoted to the next layer.",
                            "Only write promotion candidates when the evidence is durable and reusable.",
                        ],
                    },
                    ensure_ascii=False,
                ),
            },
        ]
        response = self.llm.chat_response(model=self.model, messages=messages)
        data = _parse_json_object(response.content)
        raw_candidates = data.get("candidates") or data.get("memories") or []
        raw_archive_ids = _as_string_list(data.get("archive_memory_ids"))

        candidates: list[MemoryCandidate] = []
        skipped: list[dict[str, Any]] = []
        if not isinstance(raw_candidates, list):
            return [], response.token_usage, [{"reason": "candidates_not_list"}], raw_archive_ids

        for raw in raw_candidates:
            if not isinstance(raw, dict):
                skipped.append({"reason": "candidate_not_object"})
                continue
            memory_type = str(raw.get("type") or raw.get("memory_type") or "").strip().lower()
            subtype = str(raw.get("subtype") or "").strip().lower()
            title = str(raw.get("title") or "").strip()
            body = str(raw.get("summary") or raw.get("body") or "").strip()
            source_ids = [memory_id for memory_id in _as_string_list(raw.get("source_memory_ids")) if memory_id in batch_by_id]
            if bool(raw.get("sensitive", False)):
                skipped.append({"title": title, "type": memory_type, "reason": "sensitive"})
                continue
            if not _is_allowed_output_type(layer, memory_type):
                skipped.append({"title": title, "type": memory_type, "reason": "invalid_output_type_for_layer"})
                continue
            if memory_type == "session_memory":
                if subtype != SESSION_SUMMARY_SUBTYPE:
                    skipped.append({"title": title, "type": memory_type, "reason": "invalid_session_subtype"})
                    continue
                if not any(_is_raw_session(batch_by_id[memory_id]) for memory_id in source_ids):
                    skipped.append({"title": title, "type": memory_type, "reason": "session_summary_requires_raw_session_sources"})
                    continue
            if memory_type != "session_memory" and subtype:
                subtype = ""
            if _is_promotion(layer, memory_type) and not _promotion_has_enough_evidence(source_ids, batch_by_id):
                skipped.append({"title": title, "type": memory_type, "reason": "promotion_requires_source_evidence"})
                continue
            if not title or not body:
                skipped.append({"title": title, "type": memory_type, "reason": "empty_title_or_body"})
                continue
            if not source_ids:
                skipped.append({"title": title, "type": memory_type, "reason": "missing_source_memory_ids"})
                continue
            sources = [batch_by_id[memory_id] for memory_id in source_ids]
            candidates.append(
                MemoryCandidate(
                    memory_type=memory_type,
                    title=title,
                    body=body,
                    subtype=subtype,
                    tags=_unique(_as_string_list(raw.get("tags")))[:8],
                    confidence=_as_float(raw.get("confidence")),
                    source_run=_first_nonempty([item.source_run for item in sources]),
                    source_run_id=_first_nonempty([item.source_run_id for item in sources]),
                    source_trace_ids=_unique([trace_id for item in sources for trace_id in item.source_trace_ids])[:32],
                    source_step_ids=_unique([step_id for item in sources for step_id in item.source_step_ids])[:32],
                    source_tool_names=_unique([tool for item in sources for tool in item.source_tool_names])[:32],
                    source_modified_files=_unique([path for item in sources for path in item.source_modified_files])[:32],
                    parent_memory_ids=_unique(source_ids)[:32],
                    evidence=_as_string_list(raw.get("evidence"))[:5],
                )
            )
        return candidates, response.token_usage, skipped, raw_archive_ids

    def _looks_duplicate(self, candidate: MemoryCandidate) -> bool:
        query = " ".join([candidate.title, candidate.body, *candidate.tags])
        parent_ids = set(candidate.parent_memory_ids)
        for result in self.memory_store.search(query, limit=3):
            if result.item.memory_id in parent_ids:
                continue
            if result.item.memory_type == candidate.memory_type and result.score >= 14:
                return True
        return False

    def _write_state(self, state: dict[str, Any], processed_items: list[MemoryItem], result: DreamingResult) -> None:
        now = datetime.now(timezone.utc).isoformat()
        processed_session_ids = set(_as_string_list(state.get("processed_session_ids")))
        processed_memory_ids = set(_as_string_list(state.get("processed_memory_ids")))
        for item in processed_items:
            processed_memory_ids.add(item.memory_id)
            if item.memory_type == "session_memory":
                processed_session_ids.add(item.memory_id)

        payload = {
            "last_dream_at": now,
            "processed_session_ids": sorted(processed_session_ids),
            "processed_memory_ids": sorted(processed_memory_ids),
            "last_result": {
                "status": result.status,
                "reason": result.reason,
                "written": [item.memory_id for item in result.written],
                "archived": [item.memory_id for item in result.archived],
                "token_usage": asdict(result.token_usage),
            },
        }
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.state_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")


def _dreaming_prompt(layer: str, next_layer: str | None, max_candidates: int) -> str:
    same_layer = "session_memory with subtype=session_summary" if layer == "session_memory" else layer
    promotion_text = (
        f"Also judge whether the result should be promoted to {next_layer}."
        if next_layer
        else "This is the top layer; do not promote to another memory type."
    )
    allowed_types = [layer]
    if next_layer:
        allowed_types.append(next_layer)
    return (
        "You are MiniCode's offline memory dreaming pass. Return one JSON object only. "
        f"Current layer: {layer}. Same-layer consolidation output: {same_layer}. "
        f"{promotion_text} "
        f"Allowed output memory types for this pass: {', '.join(allowed_types)}. "
        "For session_memory outputs, subtype must be session_summary. "
        "For non-session outputs, subtype must be empty. "
        "Do not save secrets, API keys, raw credentials, or one-off trivia. "
        "Prefer fewer, clearer memories over many small fragments. "
        "Use source_memory_ids to cite the memories that support each candidate. "
        "Only include archive_memory_ids for current-layer memories that are fully covered by a same-layer candidate; "
        "do not archive source memories merely because a promotion candidate was written. "
        "Schema: "
        '{"candidates":[{"type":"session_memory|project_memory|procedural_memory|experience_memory",'
        '"subtype":"session_summary for session_memory, otherwise empty",'
        '"title":"short title","summary":"consolidated memory","tags":["tag"],'
        '"confidence":0.0,"source_memory_ids":["memory-id"],"evidence":["short evidence"],"sensitive":false}],'
        '"archive_memory_ids":["covered-long-term-memory-id"]}. '
        f"Return at most {max_candidates} candidates."
    )


def _serialize_memory(item: MemoryItem) -> dict[str, Any]:
    return {
        "id": item.memory_id,
        "type": item.memory_type,
        "subtype": item.subtype,
        "title": item.title,
        "tags": item.tags,
        "body": _preview(item.body, limit=1600),
        "source_run_id": item.source_run_id,
        "source_trace_ids": item.source_trace_ids[:12],
        "parent_memory_ids": item.parent_memory_ids[:12],
        "created_at": item.created_at,
    }


def _estimate_memory_tokens(item: MemoryItem) -> int:
    text = " ".join([item.title, item.body, " ".join(item.tags)])
    ascii_chars = sum(1 for char in text if ord(char) < 128)
    non_ascii_chars = len(text) - ascii_chars
    return max(1, (ascii_chars + 3) // 4 + non_ascii_chars)


def _exact_duplicate_groups(items: list[MemoryItem]) -> list[list[MemoryItem]]:
    groups: dict[str, list[MemoryItem]] = {}
    for item in items:
        key = _memory_fingerprint(item)
        groups.setdefault(key, []).append(item)
    return [group for group in groups.values() if len(group) > 1]


def _duplicate_archive_ids(items: list[MemoryItem]) -> list[str]:
    archive_ids: list[str] = []
    for group in _exact_duplicate_groups(items):
        for item in sorted(group, key=lambda memory: memory.source_path)[1:]:
            archive_ids.append(item.memory_id)
    return archive_ids


def _memory_fingerprint(item: MemoryItem) -> str:
    body = re.sub(r"\s+", " ", item.body).strip().casefold()
    title = re.sub(r"\s+", " ", item.title).strip().casefold()
    raw = f"{item.memory_type}|{item.subtype}|{title}|{body}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _diverse_same_layer(items: list[MemoryItem], limit: int) -> list[MemoryItem]:
    return sorted(items, key=lambda item: (item.created_at, item.memory_id))[:limit]


def _is_archivable_same_layer(memory_id: str, batch: list[MemoryItem], layer: str) -> bool:
    for item in batch:
        if item.memory_id == memory_id:
            if layer == "session_memory":
                return _is_raw_session(item)
            return item.memory_type == layer
    return False


def _layer_metrics(
    layer: str,
    items: list[MemoryItem],
    duplicate_groups: list[list[MemoryItem]],
    config: DreamingConfig,
    hours_since_last: float,
    has_last_dream: bool,
) -> dict[str, Any]:
    estimated_tokens = sum(_estimate_memory_tokens(item) for item in items)
    count_threshold = config.session_threshold if layer == "session_memory" else config.memory_threshold
    token_threshold = config.session_token_threshold if layer == "session_memory" else config.memory_token_threshold
    return {
        "eligible_count": len(items),
        "estimated_tokens": estimated_tokens,
        "duplicate_group_count": len(duplicate_groups),
        "count_threshold": count_threshold,
        "token_threshold": token_threshold,
        "triggers": {
            "duplicates": bool(duplicate_groups),
            "count": len(items) >= max(1, count_threshold),
            "tokens": estimated_tokens >= max(1, token_threshold),
            "time": has_last_dream and bool(items) and hours_since_last >= max(1, config.interval_hours),
        },
    }


def _is_allowed_output_type(layer: str, memory_type: str) -> bool:
    if memory_type == layer:
        return True
    return NEXT_MEMORY_LAYER.get(layer) == memory_type


def _is_promotion(layer: str, memory_type: str) -> bool:
    return NEXT_MEMORY_LAYER.get(layer) == memory_type


def _promotion_has_enough_evidence(source_ids: list[str], batch_by_id: dict[str, MemoryItem]) -> bool:
    return any(memory_id in batch_by_id for memory_id in source_ids)


def _is_archivable_raw_session(memory_id: str, batch: list[MemoryItem], hot_days: float) -> bool:
    for item in batch:
        if item.memory_id == memory_id:
            return _is_raw_session(item) and _is_older_than_days(item, hot_days)
    return False


def _dedup_eligible_items(items: list[MemoryItem], hot_days: float) -> list[MemoryItem]:
    return [
        item
        for item in items
        if not _is_raw_session(item) or _is_older_than_days(item, hot_days)
    ]


def _is_raw_session(item: MemoryItem) -> bool:
    return item.memory_type == "session_memory" and item.subtype != SESSION_SUMMARY_SUBTYPE


def _is_older_than_days(item: MemoryItem, days: float) -> bool:
    created_at = _parse_datetime(item.created_at)
    if created_at is None:
        return False
    return (datetime.now(timezone.utc) - created_at).total_seconds() >= max(0.0, days) * 86400


def _parse_datetime(value: str) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _read_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _parse_json_object(text: str) -> dict[str, Any]:
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = stripped.strip("`")
        if stripped.startswith("json"):
            stripped = stripped[4:].strip()
    try:
        data = json.loads(stripped)
    except json.JSONDecodeError:
        start = stripped.find("{")
        end = stripped.rfind("}")
        if start == -1 or end == -1 or end <= start:
            raise
        data = json.loads(stripped[start : end + 1])
    if not isinstance(data, dict):
        raise ValueError("dreaming response must be a JSON object")
    return data


def _hours_since(value: object) -> float:
    if not value:
        return 10**9
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return 10**9
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - parsed).total_seconds() / 3600


def _normalize_mode(mode: str) -> str:
    normalized = str(mode).strip().lower()
    if normalized in DREAMING_MODES:
        return normalized
    return "auto"


def _as_string_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if value is None:
        return []
    text = str(value).strip()
    return [text] if text else []


def _as_float(value: Any) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        parsed = 0.0
    return _clamp(parsed)


def _clamp(value: float) -> float:
    return max(0.0, min(1.0, value))


def _first_nonempty(values: list[str]) -> str:
    for value in values:
        if value:
            return value
    return ""


def _preview(value: str, limit: int) -> str:
    text = " ".join(value.split())
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."


def _unique(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result
