from __future__ import annotations

from dataclasses import asdict
from typing import Any

try:
    from rich.console import Console
    from rich.markdown import Markdown
    from rich.panel import Panel
    from rich.table import Table
except ImportError:  # pragma: no cover - exercised only without optional UI deps
    Console = None
    Markdown = None
    Panel = None
    Table = None


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
        if self.console and Markdown:
            self.console.print(Markdown(answer or "Done."))
            return
        print(answer or "Done.")

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

    def saved(self, path: Any, label: str = "Session run log") -> None:
        self.note(f"{label} written to {path}")
