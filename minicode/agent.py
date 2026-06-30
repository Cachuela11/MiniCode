from __future__ import annotations

import json
from datetime import datetime, timezone
from dataclasses import dataclass, field
from typing import Any, Protocol

from .context import ContextConfig, ContextManager, build_initial_context, render_context_layer_prompt
from .dreaming import DreamingConfig, MemoryDreamer
from .evolution import SelfEvolution
from .llm import LLMResponse
from .memory import FileMemoryStore
from .observability import (
    FileSnapshot,
    RunLog,
    StepLog,
    TestResult,
    Timer,
    TokenUsage,
    make_run_id,
    summarize_messages,
)
from .sandbox import DockerSandbox
from .skills import SkillCatalog, SkillRoute, TwoStageSkillRouter, render_skill_prompt
from .tools import ToolRegistry


SYSTEM_PROMPT_TEMPLATE = """You are MiniCode, a coding agent inspired by Claude Code.

You work in a workspace mounted into Docker at /workspace. Prefer structured
tools for file operations and tests. Use run_shell only when the structured
tools are not enough. Keep changes focused on the user request.

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


@dataclass
class AgentConfig:
    model: str
    max_steps: int = 8
    final_test_command: str | None = None
    skills_enabled: bool = True
    max_skills: int = 2
    skill_recall_k: int = 8
    context_artifact_dir: str = ".minicode/context-artifacts"
    observation_inline_limit: int = 6000
    observation_preview_chars: int = 1200
    context_history_char_limit: int = 24000
    context_keep_recent_messages: int = 6
    context_note_char_limit: int = 6000
    memory_dir: str = ".minicode/memory"
    memory_trigger_mode: str = "on"
    memory_min_confidence: float = 0.7
    memory_max_candidates: int = 5
    dreaming_mode: str = "auto"
    dreaming_session_threshold: int = 8
    dreaming_session_token_threshold: int = 12000
    dreaming_memory_threshold: int = 40
    dreaming_memory_token_threshold: int = 12000
    dreaming_interval_hours: int = 24
    dreaming_max_batch_size: int = 20
    dreaming_min_confidence: float = 0.75
    dreaming_session_hot_days: float = 2.0


@dataclass
class AgentResult:
    answer: str
    steps: int
    transcript: list[dict[str, Any]] = field(default_factory=list)
    run_log: RunLog | None = None


@dataclass
class SessionTurnResult:
    answer: str
    turn: int
    steps: int
    transcript: list[dict[str, Any]] = field(default_factory=list)


class ChatClient(Protocol):
    def chat_response(self, model: str, messages: list[dict[str, str]]) -> LLMResponse:
        ...


class CodingAgent:
    def __init__(
        self,
        llm: ChatClient,
        sandbox: DockerSandbox,
        config: AgentConfig,
        tools: ToolRegistry | None = None,
        skill_catalog: SkillCatalog | None = None,
    ):
        self.llm = llm
        self.sandbox = sandbox
        self.config = config
        self.skill_catalog = skill_catalog or SkillCatalog.empty()
        self.memory_store = FileMemoryStore(workspace=sandbox.workspace, memory_dir=config.memory_dir)
        self.tools = tools or ToolRegistry(
            workspace=sandbox.workspace,
            sandbox=sandbox,
            skill_catalog=self.skill_catalog,
            memory_store=self.memory_store,
            llm=self.llm,
            model=self.config.model,
            skill_recall_k=self.config.skill_recall_k,
        )
        self.tools.set_skill_catalog(self.skill_catalog)
        self.tools.set_memory_store(self.memory_store)

    def start_session(self) -> "CodingSession":
        return CodingSession(self)

    def run(self, task: str) -> AgentResult:
        run_timer = Timer()
        file_snapshot = FileSnapshot(self.sandbox.workspace)
        started_at = datetime.now(timezone.utc).isoformat()
        run_log = RunLog(
            task=task,
            model=self.config.model,
            started_at=started_at,
            run_id=make_run_id(started_at, task),
        )
        skill_route = self._route_skills(task)
        run_log.skill_route = skill_route.to_log_dict()
        if skill_route.rerank_token_usage:
            run_log.token_usage.add(
                TokenUsage(
                    prompt_tokens=skill_route.rerank_token_usage.get("prompt_tokens", 0),
                    completion_tokens=skill_route.rerank_token_usage.get("completion_tokens", 0),
                    total_tokens=skill_route.rerank_token_usage.get("total_tokens", 0),
                )
            )
        context_manager = ContextManager(
            workspace=self.sandbox.workspace,
            config=ContextConfig(
                artifact_dir=self.config.context_artifact_dir,
                observation_inline_limit=self.config.observation_inline_limit,
                observation_preview_chars=self.config.observation_preview_chars,
                history_char_limit=self.config.context_history_char_limit,
                keep_recent_messages=self.config.context_keep_recent_messages,
                note_char_limit=self.config.context_note_char_limit,
            ),
            run_id=run_log.run_id,
        )
        self.tools.set_context_manager(context_manager)
        messages: list[dict[str, str]] = [
            {
                "role": "system",
                "content": SYSTEM_PROMPT_TEMPLATE.format(
                    tool_descriptions=self.tools.describe(),
                    skill_instructions=render_skill_prompt(skill_route),
                    context_layer_instructions=render_context_layer_prompt(),
                ),
            },
            {
                "role": "user",
                "content": f"Task:\n{task}\n\nInitial context:\n{build_initial_context(self.sandbox)}",
            },
        ]
        transcript: list[dict[str, Any]] = []

        for step in range(1, self.config.max_steps + 1):
            messages = context_manager.compact_messages(messages)
            step_timer = Timer()
            model_input_summary = summarize_messages(messages)
            llm_response = self.llm.chat_response(model=self.config.model, messages=messages)
            raw = llm_response.content
            action = _parse_action(raw)
            transcript.append({"step": step, "model": raw, "action": action})

            name = action.get("action")
            args = action.get("args") or {}
            if not isinstance(args, dict):
                args = {}

            if name == "finish":
                answer = _extract_finish_answer(action, args)
                run_log.steps.append(
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
                        modified_files=file_snapshot.diff(),
                        token_usage=llm_response.token_usage,
                        duration_ms=step_timer.elapsed_ms(),
                    )
                )
                run_log.token_usage.add(llm_response.token_usage)
                run_log.answer = answer
                run_log.final_test_result = self._run_final_test()
                self._finalize_run_log(run_log, context_manager, run_timer)
                return AgentResult(
                    answer=answer,
                    steps=step,
                    transcript=transcript,
                    run_log=run_log,
                )

            tool_result = self.tools.execute(str(name), args)
            modified_files = file_snapshot.diff()
            context_event = context_manager.record_observation(
                step=step,
                tool_name=str(name),
                output=tool_result.output,
                exit_code=tool_result.exit_code,
                modified_files=modified_files,
            )
            observation = context_event.message_content
            run_log.steps.append(
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
            run_log.token_usage.add(llm_response.token_usage)

            messages.append({"role": "assistant", "content": json.dumps(action)})
            messages.append({"role": "user", "content": f"Observation:\n{observation}"})

        answer = f"Stopped after {self.config.max_steps} steps without finish."
        run_log.answer = answer
        run_log.final_test_result = self._run_final_test()
        self._finalize_run_log(run_log, context_manager, run_timer)
        return AgentResult(
            answer=answer,
            steps=self.config.max_steps,
            transcript=transcript,
            run_log=run_log,
        )

    def _finalize_run_log(self, run_log: RunLog, context_manager: ContextManager, run_timer: Timer) -> None:
        run_log.context = context_manager.to_log_dict()
        run_log.duration_ms = run_timer.elapsed_ms()
        memory_result = SelfEvolution(
            llm=self.llm,
            model=self.config.model,
            memory_store=self.memory_store,
            mode=self.config.memory_trigger_mode,
            min_confidence=self.config.memory_min_confidence,
            max_candidates=self.config.memory_max_candidates,
        ).on_run_complete(run_log)
        run_log.memory_evolution = memory_result.to_log_dict()
        run_log.token_usage.add(memory_result.token_usage)
        dreaming_result = MemoryDreamer(
            llm=self.llm,
            model=self.config.model,
            memory_store=self.memory_store,
            config=DreamingConfig(
                mode=self.config.dreaming_mode,
                session_threshold=self.config.dreaming_session_threshold,
                session_token_threshold=self.config.dreaming_session_token_threshold,
                memory_threshold=self.config.dreaming_memory_threshold,
                memory_token_threshold=self.config.dreaming_memory_token_threshold,
                interval_hours=self.config.dreaming_interval_hours,
                max_batch_size=self.config.dreaming_max_batch_size,
                min_confidence=self.config.dreaming_min_confidence,
                session_hot_days=self.config.dreaming_session_hot_days,
            ),
        ).run(force=False)
        run_log.memory_dreaming = dreaming_result.to_log_dict()
        run_log.token_usage.add(dreaming_result.token_usage)
        run_log.duration_ms = run_timer.elapsed_ms()

    def _run_final_test(self) -> TestResult | None:
        if not self.config.final_test_command:
            return None
        timer = Timer()
        result = self.sandbox.run(self.config.final_test_command)
        return TestResult(
            command=self.config.final_test_command,
            passed=result.exit_code == 0,
            stdout=result.stdout,
            stderr=result.stderr,
            exit_code=result.exit_code,
            duration_ms=timer.elapsed_ms(),
        )

    def _route_skills(self, task: str) -> SkillRoute:
        if not self.config.skills_enabled:
            return SkillRoute(intent="disabled", rejected=self.skill_catalog.names())
        router = TwoStageSkillRouter(
            self.skill_catalog,
            max_skills=self.config.max_skills,
            recall_k=self.config.skill_recall_k,
            llm=self.llm,
            model=self.config.model,
        )
        return router.route(task)


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
        try:
            value = json.loads(text[start : end + 1])
        except json.JSONDecodeError:
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


def _extract_finish_answer(action: dict[str, Any], args: dict[str, Any]) -> str:
    answer = args.get("answer")
    if not answer:
        answer = action.get("answer")
    return str(answer or "Done.").strip()


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
        if self.closed:
            raise RuntimeError("session is already closed")
        self.turn += 1
        turn = self.turn
        turn_transcript: list[dict[str, Any]] = []
        skill_route = self.agent._route_skills(user_message)
        route_log = skill_route.to_log_dict()
        route_log["turn"] = turn
        self.skill_routes.append(route_log)
        self.run_log.skill_route = {"mode": "per_turn", "turns": self.skill_routes}
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
                "content": "\n\n".join(
                    [
                        f"User turn {turn}:",
                        user_message,
                        "Relevant skills for this turn:",
                        render_skill_prompt(skill_route),
                    ]
                ),
            }
        )

        for local_step in range(1, self.agent.config.max_steps + 1):
            self.messages = self.context_manager.compact_messages(self.messages)
            step = len(self.run_log.steps) + 1
            step_timer = Timer()
            model_input_summary = summarize_messages(self.messages)
            llm_response = self.agent.llm.chat_response(model=self.agent.config.model, messages=self.messages)
            raw = llm_response.content
            action = _parse_action(raw)
            turn_transcript.append({"turn": turn, "step": step, "model": raw, "action": action})
            self.transcript.append({"turn": turn, "step": step, "model": raw, "action": action})

            name = action.get("action")
            args = action.get("args") or {}
            if not isinstance(args, dict):
                args = {}

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
                return SessionTurnResult(
                    answer=answer,
                    turn=turn,
                    steps=local_step,
                    transcript=turn_transcript,
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

        answer = f"Stopped after {self.agent.config.max_steps} steps without finish."
        self.answers.append({"turn": turn, "answer": answer})
        self.run_log.answer = _render_session_answers(self.answers)
        self.messages.append({"role": "user", "content": f"Turn {turn} stopped: {answer}"})
        return SessionTurnResult(answer=answer, turn=turn, steps=self.agent.config.max_steps, transcript=turn_transcript)

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
