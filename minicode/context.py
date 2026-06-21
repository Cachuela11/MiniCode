from __future__ import annotations

from .sandbox import DockerSandbox


def build_initial_context(sandbox: DockerSandbox) -> str:
    result = sandbox.run("pwd && find . -maxdepth 2 -type f | sort | head -200")
    if result.exit_code != 0:
        return f"Could not inspect workspace:\n{result.stderr}"
    return result.stdout.strip() or "Workspace has no files."
