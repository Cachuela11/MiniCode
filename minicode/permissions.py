from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum


class Decision(str, Enum):
    ALLOW = "allow"
    DENY = "deny"
    ASK = "ask"


@dataclass(frozen=True)
class PolicyDecision:
    decision: Decision
    reason: str
    dangerous: bool = False


class CommandPolicy:
    """Small, conservative policy gate before a command reaches Docker."""

    deny_patterns = [
        (re.compile(r"\brm\s+(-[^\s]*r[^\s]*f|-rf|-fr)\b"), "recursive force delete is blocked"),
        (re.compile(r"\bfind\b.*\b-delete\b"), "find -delete is blocked"),
        (re.compile(r">\s*/dev/(sd|nvme|zero|random)"), "raw device writes are blocked"),
        (re.compile(r"\bmkfs(\.[a-z0-9]+)?\b"), "filesystem formatting is blocked"),
        (re.compile(r"\bdd\b.*\bof=/dev/"), "raw disk writes are blocked"),
        (re.compile(r"\bshutdown\b|\breboot\b|\bpoweroff\b"), "host lifecycle commands are blocked"),
        (re.compile(r"\bdocker\b|\bpodman\b"), "nested container control is blocked"),
    ]
    ask_patterns = [
        (re.compile(r"\b(git\s+push|git\s+reset|git\s+clean)\b"), "git history or remote changes need approval"),
        (re.compile(r"\b(curl|wget|ssh|scp|rsync|nc|ncat|telnet)\b"), "network-capable commands need approval"),
        (re.compile(r"\b(pip|npm|pnpm|yarn|cargo|go)\s+(install|add|get)\b"), "dependency installs need approval"),
        (re.compile(r"\brm\b|\bmv\b"), "destructive or moving file operations need approval"),
        (re.compile(r"\bchmod\b|\bchown\b"), "permission or ownership changes need approval"),
    ]

    def check(self, command: str) -> PolicyDecision:
        normalized = " ".join(command.strip().split())
        if not normalized:
            return PolicyDecision(Decision.DENY, "empty command", dangerous=False)

        for segment in _split_shell_segments(normalized):
            for pattern, reason in self.deny_patterns:
                if pattern.search(segment):
                    return PolicyDecision(Decision.DENY, reason, dangerous=True)

        for segment in _split_shell_segments(normalized):
            for pattern, reason in self.ask_patterns:
                if pattern.search(segment):
                    return PolicyDecision(Decision.ASK, reason, dangerous=True)

        return PolicyDecision(Decision.ALLOW, "command allowed", dangerous=False)


class ApprovalProvider:
    def approve(self, command: str, reason: str) -> bool:
        raise NotImplementedError


class NeverApprove(ApprovalProvider):
    def approve(self, command: str, reason: str) -> bool:
        return False


class AlwaysApprove(ApprovalProvider):
    def approve(self, command: str, reason: str) -> bool:
        return True


class ConsoleApproval(ApprovalProvider):
    def approve(self, command: str, reason: str) -> bool:
        print(f"Command needs approval: {reason}")
        print(command)
        answer = input("Allow this command? [y/N] ").strip().lower()
        return answer in {"y", "yes"}


def _split_shell_segments(command: str) -> list[str]:
    segments = [segment.strip() for segment in re.split(r"\s*(?:&&|\|\||;|\|)\s*", command)]
    return [segment for segment in segments if segment]
