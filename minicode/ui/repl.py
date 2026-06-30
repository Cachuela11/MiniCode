from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable

from ..agent import CodingAgent, CodingSession
from .commands import is_known_command, parse_slash_command
from .render import CliRenderer

try:
    from prompt_toolkit import PromptSession
    from prompt_toolkit.auto_suggest import AutoSuggestFromHistory
    from prompt_toolkit.history import FileHistory
except ImportError:  # pragma: no cover - exercised only without optional UI deps
    PromptSession = None
    AutoSuggestFromHistory = None
    FileHistory = None


RunLogWriter = Callable[[Path, dict], Path]


class MiniCodeRepl:
    def __init__(
        self,
        *,
        agent: CodingAgent,
        args: Any,
        run_log_writer: RunLogWriter,
        renderer: CliRenderer | None = None,
    ):
        self.agent = agent
        self.args = args
        self.run_log_writer = run_log_writer
        self.renderer = renderer or CliRenderer()
        self.prompt_session = self._build_prompt_session()

    def run(self) -> int:
        session = self.agent.start_session()
        initial_task = " ".join(self.args.task).strip()
        self.renderer.banner(session)
        try:
            if initial_task:
                self._run_turn(session, initial_task)
            while True:
                try:
                    user_message = self._read_input()
                except EOFError:
                    self.renderer.note("Closing chat session.")
                    break
                except KeyboardInterrupt:
                    self.renderer.note("Interrupted. Closing chat session.")
                    break

                if not user_message:
                    continue
                command = parse_slash_command(user_message)
                if command is not None:
                    if command.is_exit:
                        break
                    self._handle_command(command, session)
                    continue
                self._run_turn(session, user_message)
        finally:
            run_log = session.close()
            self._write_transcript(session)
            if self.args.run_log and run_log:
                run_log_path = self.run_log_writer(Path(self.args.run_log), run_log.to_dict())
                self.renderer.saved(run_log_path)
        return 0

    def _run_turn(self, session: CodingSession, user_message: str) -> None:
        try:
            result = session.run_turn(user_message)
        except RuntimeError as exc:
            self.renderer.error(str(exc))
            return
        self.renderer.answer(result.answer)

    def _handle_command(self, command, session: CodingSession) -> None:
        if command.name == "help":
            self.renderer.help()
            return
        if command.name == "status":
            self.renderer.status(session)
            return
        if not is_known_command(command):
            self.renderer.error(f"unknown command /{command.name}; try /help")
            return
        self.renderer.help()

    def _read_input(self) -> str:
        if self.prompt_session is not None:
            return self.prompt_session.prompt("minicode> ").strip()
        return input("minicode> ").strip()

    def _write_transcript(self, session: CodingSession) -> None:
        if not self.args.transcript:
            return
        transcript_path = Path(self.args.transcript)
        transcript_path.parent.mkdir(parents=True, exist_ok=True)
        transcript_path.write_text(
            json.dumps(session.transcript, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        self.renderer.saved(transcript_path, label="Session transcript")

    def _build_prompt_session(self):
        if PromptSession is None or FileHistory is None or AutoSuggestFromHistory is None:
            return None
        history_path = self.agent.sandbox.workspace / ".minicode" / "chat-history.txt"
        history_path.parent.mkdir(parents=True, exist_ok=True)
        return PromptSession(
            history=FileHistory(str(history_path)),
            auto_suggest=AutoSuggestFromHistory(),
        )
