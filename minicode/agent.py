from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from .context import ContextManager
from .dreaming import DreamingConfig, MemoryDreamer
from .injection import PromptInjectionClassifier
from .llm import LLMResponse
from .memory import FileMemoryStore
from .memory_trigger import MemoryTrigger
from .observability import (
    RunLog,
    TestResult,
    Timer,
)
from .runtime import AgentResult, run_agent
from .sandbox import DockerSandbox
from .session import CodingSession, SessionEvent, SessionTurnResult
from .skills import SkillCatalog, SkillRoute, TwoStageSkillRouter
from .tools import ToolRegistry


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
    stream_model_responses: bool = True


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
        self.prompt_injection_classifier = PromptInjectionClassifier(llm=self.llm, model=self.config.model)

    def start_session(self) -> "CodingSession":
        return CodingSession(self)

    def run(self, task: str) -> AgentResult:
        return run_agent(self, task)

    def _finalize_run_log(self, run_log: RunLog, context_manager: ContextManager, run_timer: Timer) -> None:
        run_log.context = context_manager.to_log_dict()
        run_log.duration_ms = run_timer.elapsed_ms()
        memory_result = MemoryTrigger(
            llm=self.llm,
            model=self.config.model,
            memory_store=self.memory_store,
            mode=self.config.memory_trigger_mode,
            min_confidence=self.config.memory_min_confidence,
            max_candidates=self.config.memory_max_candidates,
        ).on_run_complete(run_log)
        run_log.memory_trigger = memory_result.to_log_dict()
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
