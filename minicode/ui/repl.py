from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable

from ..agent import CodingAgent, CodingSession
from ..resume import (
    build_resume_result,
    delete_session_log,
    find_resume_log,
    list_resume_candidates,
    load_resume_log,
    resolve_resume_selection,
)
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
            for event in session.iter_turn(user_message):
                self.renderer.event(event)
                if event.kind == "turn_finish":
                    self.renderer.answer(str(event.data.get("answer") or event.message))
        except RuntimeError as exc:
            self.renderer.error(str(exc))
            return

    def _handle_command(self, command, session: CodingSession) -> None:
        if command.name == "help":
            self.renderer.help()
            return
        if command.name == "status":
            self.renderer.status(session)
            return
        if command.name == "resume":
            self._handle_resume(command.args, session)
            return
        if command.name == "sessions":
            self._handle_sessions(command.args)
            return
        if not is_known_command(command):
            self.renderer.error(f"unknown command /{command.name}; try /help")
            return
        self.renderer.help()

    def _handle_resume(self, raw_path: str, session: CodingSession) -> None:
        try:
            path = self._select_resume_path(raw_path)
            payload = load_resume_log(path)
            result = build_resume_result(payload, path)
            if result.restored_turns == 0 and result.restored_steps == 0 and not payload.get("answer"):
                self.renderer.error(f"resume failed: selected session has no recoverable content: {path}")
                return
            session.resume(result)
        except Exception as exc:
            self.renderer.error(f"resume failed: {exc}")
            return
        self.renderer.note(
            f"Resumed {result.restored_turns} turn(s), {result.restored_steps} step(s) from {result.source_path}"
        )

    def _handle_sessions(self, args: str) -> None:
        command, _, rest = args.strip().partition(" ")
        command = command or "list"
        target = rest.strip()
        if command == "list":
            self._list_sessions(target)
            return
        if command == "delete":
            self._delete_session(target)
            return
        self.renderer.error("unknown /sessions command; use /sessions, /sessions delete <number|path>")

    def _list_sessions(self, raw_path: str = "") -> list[Any]:
        try:
            candidates = list_resume_candidates(
                raw_path,
                workspace=self.agent.sandbox.workspace,
                default_target=self.args.run_log or ".minicode/runs",
            )
        except Exception as exc:
            self.renderer.error(f"sessions failed: {exc}")
            return []
        if not candidates:
            self.renderer.note("No saved sessions found.")
            return []
        self.renderer.resume_candidates(candidates)
        return candidates

    def _delete_session(self, target: str) -> None:
        try:
            path = self._select_session_delete_path(target)
            payload = load_resume_log(path)
            run_id = payload.get("run_id") or path.name
            if not self._confirm(f"Delete session {run_id} and archive linked memories? [y/N] "):
                self.renderer.note("Session delete cancelled.")
                return
            result = delete_session_log(path, self.agent.memory_store)
        except Exception as exc:
            self.renderer.error(f"session delete failed: {exc}")
            return
        self.renderer.session_deleted(result)

    def _select_session_delete_path(self, target: str) -> Path:
        target = target.strip()
        if target:
            path = Path(target)
            if not path.is_absolute():
                path = self.agent.sandbox.workspace / path
            if path.is_file():
                return path
            if target.isdigit():
                candidates = list_resume_candidates(
                    "",
                    workspace=self.agent.sandbox.workspace,
                    default_target=self.args.run_log or ".minicode/runs",
                )
                return resolve_resume_selection(candidates, target).path
        candidates = self._list_sessions("")
        if not candidates:
            raise FileNotFoundError("no saved sessions found")
        selection = self._prompt("delete session> ")
        return resolve_resume_selection(candidates, selection).path

    def _select_resume_path(self, raw_path: str) -> Path:
        target_text = raw_path.strip()
        if target_text:
            target = Path(target_text)
            if not target.is_absolute():
                target = self.agent.sandbox.workspace / target
            if target.is_file():
                return find_resume_log(
                    raw_path,
                    workspace=self.agent.sandbox.workspace,
                    default_target=self.args.run_log or ".minicode/runs",
                )

        candidates = list_resume_candidates(
            raw_path,
            workspace=self.agent.sandbox.workspace,
            default_target=self.args.run_log or ".minicode/runs",
        )
        if not candidates:
            raise FileNotFoundError("no session run logs found")
        self.renderer.resume_candidates(candidates)
        selection = self._read_resume_selection()
        candidate = resolve_resume_selection(candidates, selection)
        if not candidate.resumable:
            raise ValueError(f"session #{candidate.index} has no recoverable content")
        return candidate.path

    def _read_resume_selection(self) -> str:
        return self._prompt("resume> ")

    def _prompt(self, prompt: str) -> str:
        if self.prompt_session is not None:
            return self.prompt_session.prompt(prompt).strip()
        return input(prompt).strip()

    def _confirm(self, prompt: str) -> bool:
        return self._prompt(prompt).lower() in {"y", "yes"}

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
