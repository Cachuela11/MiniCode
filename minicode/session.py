from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Iterator

from .actions import extract_finish_answer as _extract_finish_answer
from .actions import parse_action as _parse_action
from .context import ContextConfig, ContextManager, build_initial_context, render_context_layer_prompt
from .llm import LLMStreamDelta, LLMStreamDone
from .observability import FileSnapshot, RunLog, StepLog, Timer, TokenUsage, make_run_id, summarize_messages
from .prompts import SYSTEM_PROMPT_TEMPLATE, build_turn_message
from .skills import SkillRoute, render_skill_prompt

if TYPE_CHECKING:
    from .agent import CodingAgent


@dataclass
class SessionTurnResult:
    answer: str
    turn: int
    steps: int
    transcript: list[dict[str, Any]] = field(default_factory=list)


@dataclass(frozen=True)
class SessionEvent:
    kind: str
    turn: int
    step: int | None = None
    message: str = ""
    data: dict[str, Any] = field(default_factory=dict)


class CodingSession:
    """Interactive multi-turn wrapper around one CodingAgent instance."""

    def __init__(self, agent: CodingAgent):
        self.agent = agent
        self.started_at = datetime.now(timezone.utc).isoformat()
        self.run_timer = Timer()
        self.file_snapshot = FileSnapshot(agent.sandbox.workspace)
        self.run_log = RunLog(
            task="Interactive session",
            model=agent.config.model,
            started_at=self.started_at,
            run_id=make_run_id(self.started_at, "interactive-session"),
        )
        self.context_manager = ContextManager(
            workspace=agent.sandbox.workspace,
            config=ContextConfig(
                artifact_dir=agent.config.context_artifact_dir,
                observation_inline_limit=agent.config.observation_inline_limit,
                observation_preview_chars=agent.config.observation_preview_chars,
                history_char_limit=agent.config.context_history_char_limit,
                keep_recent_messages=agent.config.context_keep_recent_messages,
                note_char_limit=agent.config.context_note_char_limit,
            ),
            run_id=self.run_log.run_id,
        )
        agent.tools.set_context_manager(self.context_manager)
        self.messages: list[dict[str, str]] = [
            {
                "role": "system",
                "content": SYSTEM_PROMPT_TEMPLATE.format(
                    tool_descriptions=agent.tools.describe(),
                    skill_instructions=render_skill_prompt(SkillRoute(intent="interactive_session")),
                    context_layer_instructions=render_context_layer_prompt(),
                ),
            },
            {
                "role": "user",
                "content": "Interactive session started.\n\nInitial context:\n"
                + build_initial_context(agent.sandbox),
            },
        ]
        self.turn = 0
        self.transcript: list[dict[str, Any]] = []
        self.answers: list[dict[str, Any]] = []
        self.skill_routes: list[dict[str, Any]] = []
        self.closed = False

    def run_turn(self, user_message: str) -> SessionTurnResult:
        final: SessionTurnResult | None = None
        for event in self.iter_turn(user_message):
            if event.kind == "turn_finish":
                final = SessionTurnResult(
                    answer=str(event.data.get("answer") or event.message),
                    turn=event.turn,
                    steps=int(event.data.get("steps") or 0),
                    transcript=list(event.data.get("transcript") or []),
                )
        if final is None:
            raise RuntimeError("turn ended without a final event")
        return final

    def iter_turn(self, user_message: str) -> Iterator[SessionEvent]:
        if self.closed:
            raise RuntimeError("session is already closed")
        self.turn += 1
        turn = self.turn
        turn_transcript: list[dict[str, Any]] = []
        yield SessionEvent(kind="turn_start", turn=turn, message=user_message)
        skill_route = self.agent._route_skills(user_message)
        route_log = skill_route.to_log_dict()
        route_log["turn"] = turn
        self.skill_routes.append(route_log)
        self.run_log.skill_route = {"mode": "per_turn", "turns": self.skill_routes}
        yield SessionEvent(
            kind="skill_route",
            turn=turn,
            message=f"selected {len(skill_route.selected)} skill(s)",
            data={
                "selected": [item.skill.name for item in skill_route.selected],
                "recalled": [item.skill.name for item in skill_route.recalled],
                "reranker": skill_route.reranker,
                "error": skill_route.rerank_error,
            },
        )
        if skill_route.rerank_token_usage:
            self.run_log.token_usage.add(
                TokenUsage(
                    prompt_tokens=skill_route.rerank_token_usage.get("prompt_tokens", 0),
                    completion_tokens=skill_route.rerank_token_usage.get("completion_tokens", 0),
                    total_tokens=skill_route.rerank_token_usage.get("total_tokens", 0),
                )
            )
        self.messages.append(
            {
                "role": "user",
                "content": build_turn_message(
                    turn=turn,
                    user_message=user_message,
                    skill_prompt=render_skill_prompt(skill_route),
                ),
            }
        )

        for local_step in range(1, self.agent.config.max_steps + 1):
            compaction_count = len(self.context_manager.compactions)
            self.messages = self.context_manager.compact_messages(self.messages)
            if len(self.context_manager.compactions) > compaction_count:
                yield SessionEvent(
                    kind="context_compacted",
                    turn=turn,
                    message="context history compacted",
                    data=self.context_manager.compactions[-1],
                )
            step = len(self.run_log.steps) + 1
            step_timer = Timer()
            model_input_summary = summarize_messages(self.messages)
            yield SessionEvent(kind="model_start", turn=turn, step=step, message="calling model")
            llm_response = None
            if self.agent.config.stream_model_responses:
                stream_method = getattr(self.agent.llm, "chat_response_stream", None)
                if callable(stream_method):
                    try:
                        for stream_event in stream_method(model=self.agent.config.model, messages=self.messages):
                            if isinstance(stream_event, LLMStreamDelta):
                                yield SessionEvent(
                                    kind="model_delta",
                                    turn=turn,
                                    step=step,
                                    message=stream_event.content,
                                    data={"delta": stream_event.content},
                                )
                            elif isinstance(stream_event, LLMStreamDone):
                                llm_response = stream_event.response
                    except RuntimeError as exc:
                        yield SessionEvent(
                            kind="model_stream_fallback",
                            turn=turn,
                            step=step,
                            message=str(exc),
                        )
            if llm_response is None:
                llm_response = self.agent.llm.chat_response(model=self.agent.config.model, messages=self.messages)
            raw = llm_response.content
            action = _parse_action(raw)
            turn_transcript.append({"turn": turn, "step": step, "model": raw, "action": action})
            self.transcript.append({"turn": turn, "step": step, "model": raw, "action": action})

            name = action.get("action")
            args = action.get("args") or {}
            if not isinstance(args, dict):
                args = {}
            yield SessionEvent(
                kind="model_action",
                turn=turn,
                step=step,
                message=str(name),
                data={
                    "action": action,
                    "args": args,
                    "token_usage": {
                        "prompt_tokens": llm_response.token_usage.prompt_tokens,
                        "completion_tokens": llm_response.token_usage.completion_tokens,
                        "total_tokens": llm_response.token_usage.total_tokens,
                    },
                },
            )

            if name == "finish":
                answer = _extract_finish_answer(action, args)
                self.run_log.steps.append(
                    StepLog(
                        step=step,
                        model_input_summary=model_input_summary,
                        model_action=action,
                        tool_name="finish",
                        tool_args=args,
                        permission_decision="not_applicable",
                        permission_reason="",
                        stdout=answer,
                        stderr="",
                        exit_code=0,
                        modified_files=self.file_snapshot.diff(),
                        token_usage=llm_response.token_usage,
                        duration_ms=step_timer.elapsed_ms(),
                    )
                )
                self.run_log.token_usage.add(llm_response.token_usage)
                self.messages.append({"role": "assistant", "content": json.dumps(action)})
                self.answers.append({"turn": turn, "answer": answer})
                self.run_log.answer = _render_session_answers(self.answers)
                yield SessionEvent(
                    kind="turn_finish",
                    turn=turn,
                    step=step,
                    message=answer,
                    data={
                        "answer": answer,
                        "steps": local_step,
                        "transcript": turn_transcript,
                    },
                )
                return

            yield SessionEvent(
                kind="tool_start",
                turn=turn,
                step=step,
                message=str(name),
                data={"tool_name": str(name), "tool_args": args},
            )
            tool_result = self.agent.tools.execute(str(name), args)
            modified_files = self.file_snapshot.diff()
            context_event = self.context_manager.record_observation(
                step=step,
                tool_name=str(name),
                output=tool_result.output,
                exit_code=tool_result.exit_code,
                modified_files=modified_files,
            )
            observation = context_event.message_content
            self.run_log.steps.append(
                StepLog(
                    step=step,
                    model_input_summary=model_input_summary,
                    model_action=action,
                    tool_name=str(name),
                    tool_args=args,
                    permission_decision=tool_result.permission_decision,
                    permission_reason=tool_result.permission_reason,
                    stdout=tool_result.stdout,
                    stderr=tool_result.stderr,
                    exit_code=tool_result.exit_code,
                    modified_files=modified_files,
                    token_usage=llm_response.token_usage,
                    duration_ms=step_timer.elapsed_ms(),
                    dangerous_command=tool_result.dangerous_command,
                    invalid_command=tool_result.invalid_command,
                    context_event=context_event.to_log_dict(),
                    retrieval_trace=tool_result.retrieval_trace,
                )
            )
            self.run_log.token_usage.add(llm_response.token_usage)
            self.messages.append({"role": "assistant", "content": json.dumps(action)})
            self.messages.append({"role": "user", "content": f"Observation:\n{observation}"})
            yield SessionEvent(
                kind="tool_result",
                turn=turn,
                step=step,
                message=str(name),
                data={
                    "tool_name": str(name),
                    "tool_args": args,
                    "ok": tool_result.ok,
                    "exit_code": tool_result.exit_code,
                    "duration_ms": tool_result.duration_ms,
                    "modified_files": modified_files,
                    "stdout_preview": _preview_for_event(tool_result.stdout or tool_result.output),
                    "stderr_preview": _preview_for_event(tool_result.stderr),
                    "permission_decision": tool_result.permission_decision,
                    "permission_reason": tool_result.permission_reason,
                    "dangerous_command": tool_result.dangerous_command,
                    "invalid_command": tool_result.invalid_command,
                    "context_event": context_event.to_log_dict(),
                    "retrieval_trace": tool_result.retrieval_trace,
                },
            )

        answer = f"Stopped after {self.agent.config.max_steps} steps without finish."
        self.answers.append({"turn": turn, "answer": answer})
        self.run_log.answer = _render_session_answers(self.answers)
        self.messages.append({"role": "user", "content": f"Turn {turn} stopped: {answer}"})
        yield SessionEvent(
            kind="turn_finish",
            turn=turn,
            message=answer,
            data={
                "answer": answer,
                "steps": self.agent.config.max_steps,
                "transcript": turn_transcript,
            },
        )

    def close(self) -> RunLog:
        if self.closed:
            return self.run_log
        self.closed = True
        self.run_log.context = self.context_manager.to_log_dict()
        self.run_log.duration_ms = self.run_timer.elapsed_ms()
        if self.run_log.steps:
            self.run_log.final_test_result = self.agent._run_final_test()
            self.agent._finalize_run_log(self.run_log, self.context_manager, self.run_timer)
        return self.run_log


def _render_session_answers(answers: list[dict[str, Any]]) -> str:
    return "\n\n".join(f"Turn {item['turn']}: {item['answer']}" for item in answers)


def _preview_for_event(value: str, limit: int = 500) -> str:
    text = " ".join((value or "").split())
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."
