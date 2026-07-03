from __future__ import annotations

import json
import time
from dataclasses import asdict
from typing import Any

try:
    from rich.console import Console
    from rich.panel import Panel
    from rich.table import Table
    from rich.text import Text
except ImportError:  # pragma: no cover - exercised only without optional UI deps
    Console = None
    Panel = None
    Table = None
    Text = None


MINICODE_LOGO_LINES = [
    "█▄ ▄█ ▀▀█▀▀ █▄  █ ▀▀█▀▀ ▄████ ▄██▄  ████▄ █████",
    "██▄██   █   ██▄ █   █   █     █  █  █   █ █",
    "█ ▀ █   █   █ ▀▄█   █   █     █  █  █   █ ████",
    "█   █   █   █  ██   █   █     █  █  █   █ █",
    "█   █ ▄▄█▄▄ █   █ ▄▄█▄▄ ▀████ ▀██▀  ████▀ █████",
]
MINICODE_LOGO_WIDTH = max(len(line) for line in MINICODE_LOGO_LINES)
MINICODE_LOGO = "\n".join(line.ljust(MINICODE_LOGO_WIDTH) for line in MINICODE_LOGO_LINES)


class CliRenderer:
    def __init__(self) -> None:
        self.console = Console() if Console is not None else None
        self._status: Any | None = None
        self._status_started_at: float | None = None
        self._stream_chars = 0

    def banner(self, session: Any) -> None:
        if self.console and Panel and Text:
            content = Text()
            content.append(_gradient_text(MINICODE_LOGO))
            content.append("\n\nMiniCode interactive session", style="bold white")
            content.append(f"\nmodel     {session.agent.config.model}", style="white")
            content.append(f"\nworkspace {session.agent.sandbox.workspace}", style="white")
            content.append(f"\nrun_id    {session.run_log.run_id}", style="white")
            content.append("\n\n/help  /status  /exit", style="dim")
            self.console.print(Panel(content, border_style="#38bdf8", padding=(1, 2)))
            return
        lines = [
            MINICODE_LOGO,
            "",
            "MiniCode interactive session",
            f"model: {session.agent.config.model}",
            f"workspace: {session.agent.sandbox.workspace}",
            f"run_id: {session.run_log.run_id}",
            "",
            "Commands: /help, /status, /exit",
        ]
        print("\n".join(lines))

    def help(self) -> None:
        rows = [
            ("/help", "Show available commands."),
            ("/resume [path]", "Resume context from a previous session run log."),
            ("/sessions", "List or delete saved sessions."),
            ("/status", "Show current session state."),
            ("/exit", "Save and close the session."),
            ("/quit", "Alias for /exit."),
        ]
        if self.console and Table:
            table = Table(title="MiniCode Commands")
            table.add_column("Command", style="cyan")
            table.add_column("Description")
            for command, description in rows:
                table.add_row(command, description)
            self.console.print(table)
            return
        print("MiniCode commands:")
        for command, description in rows:
            print(f"  {command:<8} {description}")

    def status(self, session: Any) -> None:
        context = session.context_manager.to_log_dict()
        usage = session.run_log.token_usage
        rows = [
            ("model", session.agent.config.model),
            ("workspace", str(session.agent.sandbox.workspace)),
            ("run_id", session.run_log.run_id),
            ("turns", str(session.turn)),
            ("steps", str(len(session.run_log.steps))),
            ("messages", str(len(session.messages))),
            ("artifacts", str(len(context.get("artifacts", [])))),
            ("notes", str(len(context.get("notes", [])))),
            ("compactions", str(len(context.get("compactions", [])))),
            ("memory", session.agent.config.memory_trigger_mode),
            ("dreaming", session.agent.config.dreaming_mode),
            ("tokens", str(asdict(usage))),
        ]
        if self.console and Table:
            table = Table(title="Session Status")
            table.add_column("Field", style="cyan")
            table.add_column("Value")
            for name, value in rows:
                table.add_row(name, value)
            self.console.print(table)
            return
        print("Session status:")
        for name, value in rows:
            print(f"  {name}: {value}")

    def resume_candidates(self, candidates: list[Any]) -> None:
        if self.console and Table:
            table = Table(title="Resume Sessions")
            table.add_column("#", style="cyan", justify="right")
            table.add_column("State")
            table.add_column("Turns", justify="right")
            table.add_column("Steps", justify="right")
            table.add_column("Started")
            table.add_column("Task")
            for candidate in candidates:
                state = "ready" if candidate.resumable else "empty"
                table.add_row(
                    str(candidate.index),
                    state,
                    str(candidate.turns),
                    str(candidate.steps),
                    _text_preview(candidate.started_at, limit=22),
                    _text_preview(candidate.task or candidate.path.name, limit=44),
                )
            self.console.print(table)
            return
        print("Resume sessions:")
        for candidate in candidates:
            state = "ready" if candidate.resumable else "empty"
            print(
                f"  {candidate.index:>2}. [{state}] turns={candidate.turns} "
                f"steps={candidate.steps} started={candidate.started_at} "
                f"task={candidate.task or candidate.path.name}"
            )

    def session_deleted(self, result: Any) -> None:
        message = (
            f"Deleted session {result.run_id}; archived {len(result.archived_memories)} memory item(s). "
            f"Run log moved to {result.deleted_log_path}"
        )
        self.note(message)

    def answer(self, answer: str) -> None:
        text = answer or "The model finished without providing an answer."
        border_style = "yellow" if text.startswith("The model finished without") else "green"
        if self.console and Panel:
            content = Text(text) if Text is not None else text
            self.console.print(Panel(content, title="Answer", border_style=border_style))
            return
        print("\n=== Answer ===")
        print(text)
        print("==============")

    def event(self, event: Any) -> None:
        if event.kind == "turn_start":
            self._stop_status()
            self._section(f"Turn {event.turn}")
            return
        if event.kind == "skill_route":
            selected = event.data.get("selected") or []
            reranker = event.data.get("reranker") or "none"
            skills = ", ".join(selected) if selected else "none"
            self._kv("skills", f"{skills} ({reranker})")
            return
        if event.kind == "task_mode":
            source = event.data.get("source") or "none"
            reason = event.data.get("reason") or ""
            hints = event.data.get("planning_hints") or []
            suffix = f", hints={len(hints)}" if hints else ""
            detail = f"{event.message} ({source}{suffix})"
            if reason:
                detail += f": {_text_preview(str(reason), limit=80)}"
            self._kv("mode", detail)
            return
        if event.kind == "policy":
            rules = event.data.get("rules") or []
            required = event.data.get("required_first_action") or None
            suffix = f", first={required.get('action')}" if isinstance(required, dict) and required else ""
            self._kv("policy", f"{event.message}, rules={len(rules)}{suffix}")
            return
        if event.kind == "context_compacted":
            after_chars = event.data.get("after_chars", "?")
            before_chars = event.data.get("before_chars", "?")
            self._kv("context", f"compacted {before_chars} -> {after_chars} chars")
            return
        if event.kind == "model_start":
            self._stream_chars = 0
            self._section(f"Step {event.step}")
            self._start_status(f"step {event.step}: waiting for model")
            return
        if event.kind == "model_delta":
            self._stream_chars += len(event.data.get("delta") or event.message or "")
            self._update_status(f"step {event.step}: streaming model output ({self._stream_chars} chars)")
            return
        if event.kind == "model_stream_fallback":
            self._stop_status()
            self._kv("stream", f"fallback: {_text_preview(event.message, limit=120)}")
            self._start_status(f"step {event.step}: waiting for model")
            return
        if event.kind == "model_action":
            elapsed = self._stop_status()
            action = event.data.get("action") or {}
            args = event.data.get("args") or {}
            action_name = action.get("action") or event.message
            token_usage = event.data.get("token_usage") or {}
            token_text = _format_token_usage(token_usage)
            elapsed_text = f", {elapsed}ms" if elapsed is not None else ""
            if action_name == "finish":
                self._row("model", "finish", f"{token_text}{elapsed_text}".strip(", "))
                return
            self._action(event.step, action_name, args, token_text=token_text, elapsed_text=elapsed_text)
            return
        if event.kind == "tool_start":
            tool_name = event.data.get("tool_name") or event.message
            self._row("tool", str(tool_name), "")
            self._start_status(f"step {event.step}: running {tool_name}")
            return
        if event.kind == "tool_result":
            self._stop_status()
            self._tool_result(event)
            return
        if event.kind == "prompt_injection":
            self._stop_status()
            level = event.message
            if level not in {"safe", "low"}:
                reason = event.data.get("reason") or ""
                self._kv("risk", f"prompt-injection {level}: {_text_preview(str(reason), limit=90)}")
            return
        if event.kind == "turn_finish":
            self._stop_status()
            return

    def _action(
        self,
        step: int | None,
        action_name: str,
        args: dict[str, Any],
        *,
        token_text: str = "",
        elapsed_text: str = "",
    ) -> None:
        preview = _json_preview(args)
        meta = f"{token_text}{elapsed_text}".strip(", ")
        self._row("model", action_name, meta)
        self._kv("args", preview, indent=4)

    def _tool_result(self, event: Any) -> None:
        data = event.data
        tool_name = data.get("tool_name") or event.message
        exit_code = data.get("exit_code")
        ok = bool(data.get("ok"))
        modified = data.get("modified_files") or []
        details = [f"exit={exit_code}"]
        if modified:
            details.append(f"modified={','.join(modified)}")
        if data.get("dangerous_command"):
            details.append("dangerous")
        if data.get("invalid_command"):
            details.append("invalid")
        duration = data.get("duration_ms")
        if duration is not None:
            details.append(f"{duration}ms")
        meta = ", ".join(details)
        self._row("result", str(tool_name), meta, ok=ok)
        stdout = data.get("stdout_preview")
        if stdout:
            self._kv("stdout", _output_preview(tool_name, str(stdout)), indent=4)
        stderr = data.get("stderr_preview")
        if stderr:
            self._kv("stderr", _text_preview(str(stderr), limit=120), indent=4)

    def error(self, message: str) -> None:
        self._stop_status()
        if self.console:
            self.console.print(f"[red]ERROR:[/red] {message}")
            return
        print(f"ERROR: {message}")

    def note(self, message: str) -> None:
        if self.console:
            self.console.print(f"[dim]{message}[/dim]")
            return
        print(message)

    def trace(self, message: str) -> None:
        if self.console:
            self.console.print(f"[dim]{message}[/dim]")
            return
        print(message)

    def saved(self, path: Any, label: str = "Session run log") -> None:
        self.note(f"{label} written to {path}")

    def _section(self, title: str) -> None:
        if self.console:
            self.console.print(f"\n[bold cyan]{title}[/bold cyan]")
            return
        print(f"\n{title}")

    def _row(self, label: str, value: str, meta: str = "", ok: bool | None = None) -> None:
        marker = ""
        if ok is True:
            marker = "OK "
        elif ok is False:
            marker = "FAIL "
        line = f"  {label:<7} {marker}{value}"
        if meta:
            line += f"  ({meta})"
        if self.console:
            label_style = "green" if ok is True else "red" if ok is False else "cyan"
            suffix = f" [dim]{meta}[/dim]" if meta else ""
            self.console.print(f"  [{label_style}]{label:<7}[/{label_style}] {marker}{value}{suffix}")
            return
        print(line)

    def _kv(self, key: str, value: str, indent: int = 2) -> None:
        pad = " " * indent
        line = f"{pad}{key:<7} {value}"
        if self.console:
            self.console.print(f"[dim]{pad}{key:<7}[/dim] {value}")
            return
        print(line)

    def _start_status(self, message: str) -> None:
        self._stop_status()
        self._status_started_at = time.perf_counter()
        if self.console is None:
            return
        self._status = self.console.status(message, spinner="dots")
        self._status.start()

    def _update_status(self, message: str) -> None:
        if self._status is not None:
            self._status.update(message)

    def _stop_status(self) -> int | None:
        elapsed = None
        if self._status_started_at is not None:
            elapsed = int((time.perf_counter() - self._status_started_at) * 1000)
        if self._status is not None:
            self._status.stop()
            self._status = None
        self._status_started_at = None
        return elapsed


def _json_preview(value: dict[str, Any], limit: int = 140) -> str:
    text = json.dumps(value, ensure_ascii=False, sort_keys=True)
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."


def _text_preview(value: str, limit: int = 120) -> str:
    text = " ".join(value.split())
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."


def _output_preview(tool_name: str, value: str) -> str:
    text = value.strip()
    if not text:
        return "(empty)"
    if tool_name == "list_files":
        paths = [item for item in text.split() if item]
        shown = ", ".join(paths[:5])
        suffix = ", ..." if len(paths) > 5 or text.endswith("...") else ""
        return f"{shown}{suffix}"
    return _text_preview(text, limit=100)


def _format_token_usage(value: dict[str, Any]) -> str:
    total = value.get("total_tokens")
    prompt = value.get("prompt_tokens")
    completion = value.get("completion_tokens")
    if total:
        return f", tokens={total}"
    if prompt or completion:
        return f", tokens={prompt or 0}+{completion or 0}"
    return ""


def _gradient_text(value: str) -> Any:
    if Text is None:
        return value
    colors = [
        (34, 211, 238),
        (99, 102, 241),
        (217, 70, 239),
        (251, 146, 60),
    ]
    text = Text()
    printable = [char for char in value if char != "\n"]
    total = max(1, len(printable) - 1)
    index = 0
    for char in value:
        if char == "\n":
            text.append(char)
            continue
        color = _gradient_color(colors, index / total)
        style = f"bold #{color[0]:02x}{color[1]:02x}{color[2]:02x}"
        text.append(char, style=style)
        index += 1
    return text


def _gradient_color(colors: list[tuple[int, int, int]], position: float) -> tuple[int, int, int]:
    if position <= 0:
        return colors[0]
    if position >= 1:
        return colors[-1]
    scaled = position * (len(colors) - 1)
    left = int(scaled)
    right = min(left + 1, len(colors) - 1)
    mix = scaled - left
    return tuple(
        int(colors[left][channel] + (colors[right][channel] - colors[left][channel]) * mix)
        for channel in range(3)
    )
