from __future__ import annotations

import subprocess
import os
import time
from dataclasses import dataclass
from pathlib import Path

from .permissions import ApprovalProvider, CommandPolicy, Decision, NeverApprove


@dataclass
class SandboxResult:
    command: str
    exit_code: int
    stdout: str
    stderr: str
    permission_decision: str = "allow"
    permission_reason: str = "command allowed"
    dangerous_command: bool = False
    duration_ms: int = 0


class DockerSandbox:
    def __init__(
        self,
        workspace: Path,
        image: str = "python:3.12-slim",
        timeout: int = 60,
        policy: CommandPolicy | None = None,
        approvals: ApprovalProvider | None = None,
    ):
        self.workspace = workspace.resolve()
        self.image = image
        self.timeout = timeout
        self.docker_config = self.workspace / ".minicode" / "docker-config"
        self.policy = policy or CommandPolicy()
        self.approvals = approvals or NeverApprove()

    def run(self, command: str) -> SandboxResult:
        started = time.perf_counter()
        decision = self.policy.check(command)
        if decision.decision == Decision.DENY:
            return SandboxResult(
                command=command,
                exit_code=126,
                stdout="",
                stderr=f"Command blocked by policy: {decision.reason}",
                permission_decision=decision.decision.value,
                permission_reason=decision.reason,
                dangerous_command=decision.dangerous,
                duration_ms=_elapsed_ms(started),
            )
        if decision.decision == Decision.ASK and not self.approvals.approve(command, decision.reason):
            return SandboxResult(
                command=command,
                exit_code=126,
                stdout="",
                stderr=f"Command requires approval: {decision.reason}",
                permission_decision=decision.decision.value,
                permission_reason=decision.reason,
                dangerous_command=decision.dangerous,
                duration_ms=_elapsed_ms(started),
            )

        docker_command = [
            "docker",
            "run",
            "--rm",
            "-i",
            "--network",
            "none",
            "--cpus",
            "2",
            "--memory",
            "1g",
            "--pids-limit",
            "256",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "-v",
            f"{self.workspace}:/workspace",
            "-w",
            "/workspace",
            self.image,
            "sh",
            "-lc",
            command,
        ]
        env = os.environ.copy()
        self.docker_config.mkdir(parents=True, exist_ok=True)
        env["DOCKER_CONFIG"] = str(self.docker_config)
        try:
            completed = subprocess.run(
                docker_command,
                capture_output=True,
                text=True,
                timeout=self.timeout,
                check=False,
                env=env,
            )
        except FileNotFoundError as exc:
            raise RuntimeError("Docker CLI was not found. Install Docker first.") from exc
        except subprocess.TimeoutExpired as exc:
            return SandboxResult(
                command=command,
                exit_code=124,
                stdout=exc.stdout or "",
                stderr=(exc.stderr or "") + "\nCommand timed out.",
                permission_decision=decision.decision.value,
                permission_reason=decision.reason,
                dangerous_command=decision.dangerous,
                duration_ms=_elapsed_ms(started),
            )

        return SandboxResult(
            command=command,
            exit_code=completed.returncode,
            stdout=completed.stdout,
            stderr=completed.stderr,
            permission_decision=decision.decision.value,
            permission_reason=decision.reason,
            dangerous_command=decision.dangerous,
            duration_ms=_elapsed_ms(started),
        )


def _elapsed_ms(started: float) -> int:
    return int((time.perf_counter() - started) * 1000)
