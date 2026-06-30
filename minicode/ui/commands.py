from __future__ import annotations

from dataclasses import dataclass


EXIT_COMMANDS = {"exit", "quit", "q"}
KNOWN_COMMANDS = {"help", "status", *EXIT_COMMANDS}


@dataclass(frozen=True)
class SlashCommand:
    name: str
    args: str = ""

    @property
    def is_exit(self) -> bool:
        return self.name in EXIT_COMMANDS


def parse_slash_command(value: str) -> SlashCommand | None:
    text = value.strip()
    if not text.startswith("/"):
        return None
    command_text = text[1:].strip()
    if not command_text:
        return SlashCommand(name="help")
    name, _, args = command_text.partition(" ")
    return SlashCommand(name=name.lower(), args=args.strip())


def is_known_command(command: SlashCommand) -> bool:
    return command.name in KNOWN_COMMANDS
