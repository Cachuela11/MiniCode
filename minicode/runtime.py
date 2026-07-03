from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from .action_parser import extract_finish_answer, parse_action
from .context import ContextConfig, ContextManager, build_initial_context, render_context_layer_prompt
from .injection import protect_observation
from .observability import (
    FileSnapshot,
    RunLog,
    StepLog,
    Timer,
    TokenUsage,
    make_run_id,
    summarize_messages,
)
from .policy import PolicyEngine
from .prompts import SYSTEM_PROMPT_TEMPLATE, build_task_message
from .skills import render_skill_prompt
from .task_mode import TaskModeRouter

if TYPE_CHECKING:
    from .agent import CodingAgent


@dataclass
class AgentResult:
    answer: str
    steps: int
    transcript: list[dict[str, Any]] = field(default_factory=list)
    run_log: RunLog | None = None


def run_agent(agent: CodingAgent, task: str) -> AgentResult:
    run_timer = Timer()
    file_snapshot = FileSnapshot(agent.sandbox.workspace)
    started_at = datetime.now(timezone.utc).isoformat()
    run_log = RunLog(
        task=task,
        model=agent.config.model,
        started_at=started_at,
        run_id=make_run_id(started_at, task),
    )
    skill_route = agent._route_skills(task)
    run_log.skill_route = skill_route.to_log_dict()
    task_mode = TaskModeRouter(
        llm=agent.llm,
        model=agent.config.model,
        mode=agent.config.subagent_mode,
    ).decide(task)
    run_log.token_usage.add(task_mode.token_usage)
    policy = PolicyEngine().decide(task, task_mode=task_mode)
    run_log.policies.append(
        {
            "scope": "task",
            "task_mode": task_mode.to_log_dict(),
            **policy.to_log_dict(),
        }
    )
    if skill_route.rerank_token_usage:
        run_log.token_usage.add(
            TokenUsage(
                prompt_tokens=skill_route.rerank_token_usage.get("prompt_tokens", 0),
                completion_tokens=skill_route.rerank_token_usage.get("completion_tokens", 0),
                total_tokens=skill_route.rerank_token_usage.get("total_tokens", 0),
            )
        )
    context_manager = ContextManager(
        workspace=agent.sandbox.workspace,
        config=ContextConfig(
            artifact_dir=agent.config.context_artifact_dir,
            observation_inline_limit=agent.config.observation_inline_limit,
            observation_preview_chars=agent.config.observation_preview_chars,
            history_char_limit=agent.config.context_history_char_limit,
            keep_recent_messages=agent.config.context_keep_recent_messages,
            note_char_limit=agent.config.context_note_char_limit,
        ),
        run_id=run_log.run_id,
    )
    agent.tools.set_context_manager(context_manager)
    messages: list[dict[str, str]] = [
        {
            "role": "system",
            "content": SYSTEM_PROMPT_TEMPLATE.format(
                tool_descriptions=agent.tools.describe(),
                skill_instructions=render_skill_prompt(skill_route),
                context_layer_instructions=render_context_layer_prompt(),
            ),
        },
        {
            "role": "user",
            "content": build_task_message(task, build_initial_context(agent.sandbox), policy=policy),
        },
    ]
    transcript: list[dict[str, Any]] = []

    for step in range(1, agent.config.max_steps + 1):
        messages = context_manager.compact_messages(messages)
        step_timer = Timer()
        model_input_summary = summarize_messages(messages)
        llm_response = agent.llm.chat_response(model=agent.config.model, messages=messages)
        raw = llm_response.content
        action = parse_action(raw)
        transcript.append({"step": step, "model": raw, "action": action})

        name = action.get("action")
        args = action.get("args") or {}
        if not isinstance(args, dict):
            args = {}

        if name == "finish":
            answer = extract_finish_answer(action, args)
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
            run_log.final_test_result = agent._run_final_test()
            agent._finalize_run_log(run_log, context_manager, run_timer)
            return AgentResult(
                answer=answer,
                steps=step,
                transcript=transcript,
                run_log=run_log,
            )

        tool_result = agent.tools.execute(str(name), args)
        modified_files = file_snapshot.diff()
        injection_review = agent.prompt_injection_classifier.classify(
            tool_name=str(name),
            text=tool_result.output,
        )
        run_log.token_usage.add(injection_review.token_usage)
        protected_output = protect_observation(tool_result.output, injection_review)
        context_event = context_manager.record_observation(
            step=step,
            tool_name=str(name),
            output=protected_output,
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
                subagent_trace=tool_result.subagent_trace,
                prompt_injection_review=injection_review.to_log_dict(),
            )
        )
        run_log.token_usage.add(llm_response.token_usage)

        messages.append({"role": "assistant", "content": json.dumps(action)})
        messages.append({"role": "user", "content": f"Observation:\n{observation}"})

    answer = f"Stopped after {agent.config.max_steps} steps without finish."
    run_log.answer = answer
    run_log.final_test_result = agent._run_final_test()
    agent._finalize_run_log(run_log, context_manager, run_timer)
    return AgentResult(
        answer=answer,
        steps=agent.config.max_steps,
        transcript=transcript,
        run_log=run_log,
    )
