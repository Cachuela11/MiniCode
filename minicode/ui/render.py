from __future__ import annotations

import json
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


class CliRenderer:
    def __init__(self) -> None:
        self.console = Console() if Console is not None else None

    def banner(self, session: Any) -> None:
        lines = [
            "MiniCode interactive session",
            f"model: {session.agent.config.model}",
            f"workspace: {session.agent.sandbox.workspace}",
            f"run_id: {session.run_log.run_id}",
            "",
            "Commands: /help, /status, /exit",
        ]
        if self.console and Panel:
            self.console.print(Panel("\n".join(lines), title="MiniCode", border_style="cyan"))
            return
        print("\n".join(lines))

    def help(self) -> None:
        rows = [
            ("/help", "Show available commands."),
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
            self.trace(f"turn {event.turn} started")
            return
        if event.kind == "skill_route":
            selected = event.data.get("selected") or []
            reranker = event.data.get("reranker") or "none"
            skills = ", ".join(selected) if selected else "none"
            self.trace(f"skills: {skills} ({reranker})")
            return
        if event.kind == "context_compacted":
            after_chars = event.data.get("after_chars", "?")
            before_chars = event.data.get("before_chars", "?")
            self.trace(f"context compacted: {before_chars} -> {after_chars} chars")
            return
        if event.kind == "model_start":
            self.trace(f"model step {event.step}")
            return
        if event.kind == "model_action":
            action = event.data.get("action") or {}
            args = event.data.get("args") or {}
            action_name = action.get("action") or event.message
            if action_name == "finish":
                answer = args.get("answer") or action.get("answer") or ""
                preview = _text_preview(str(answer), limit=120)
                suffix = f": {preview}" if preview else ""
                self.trace(f"finish{suffix}")
                return
            self._action(action_name, args)
            return
        if event.kind == "tool_start":
            return
        if event.kind == "tool_result":
            self._tool_result(event)
            return
        if event.kind == "turn_finish":
            return

    def _action(self, action_name: str, args: dict[str, Any]) -> None:
        preview = _json_preview(args)
        if self.console:
            self.console.print(f"[dim]trace[/dim] [cyan]*[/cyan] {action_name} [dim]{preview}[/dim]")
            return
        print(f"[trace] * {action_name} {preview}")

    def _tool_result(self, event: Any) -> None:
        data = event.data
        tool_name = data.get("tool_name") or event.message
        exit_code = data.get("exit_code")
        ok = bool(data.get("ok"))
        modified = data.get("modified_files") or []
        status = "ok" if ok else "failed"
        details = [f"exit={exit_code}", status]
        if modified:
            details.append(f"modified={','.join(modified)}")
        if data.get("dangerous_command"):
            details.append("dangerous")
        if data.get("invalid_command"):
            details.append("invalid")
        line = f"{tool_name}: " + ", ".join(details)
        if self.console:
            style = "green" if ok else "red"
            marker = "OK" if ok else "FAIL"
            self.console.print(f"[dim]trace[/dim] [{style}]{marker}[/{style}] {line}")
        else:
            print(f"[trace] {line}")
        stderr = data.get("stderr_preview")
        if stderr:
            self.trace(f"stderr: {stderr}")

    def error(self, message: str) -> None:
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
            self.console.print(f"[dim]trace[/dim] {message}")
            return
        print(f"[trace] {message}")

    def saved(self, path: Any, label: str = "Session run log") -> None:
        self.note(f"{label} written to {path}")


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
