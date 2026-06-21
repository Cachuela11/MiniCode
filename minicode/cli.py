from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from .agent import AgentConfig, CodingAgent
from .llm import OllamaClient
from .permissions import AlwaysApprove, ConsoleApproval, NeverApprove
from .sandbox import DockerSandbox


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the MiniCode coding agent.")
    parser.add_argument("task", nargs="*", help="Coding task for the agent.")
    parser.add_argument("--check", action="store_true", help="Run environment checks and exit.")
    parser.add_argument("--model", default=os.getenv("MINICODE_MODEL", "qwen2.5:7b"))
    parser.add_argument(
        "--ollama-url",
        default=os.getenv("MINICODE_OLLAMA_URL", "http://127.0.0.1:11434"),
    )
    parser.add_argument(
        "--workspace",
        default=os.getenv("MINICODE_WORKSPACE", "."),
        help="Host workspace path mounted into Docker.",
    )
    parser.add_argument(
        "--docker-image",
        default=os.getenv("MINICODE_DOCKER_IMAGE", "python:3.12-slim"),
    )
    parser.add_argument(
        "--max-steps",
        type=int,
        default=int(os.getenv("MINICODE_MAX_STEPS", "8")),
    )
    parser.add_argument(
        "--transcript",
        help="Optional path to write the agent transcript JSON.",
    )
    parser.add_argument(
        "--approval",
        choices=["never", "ask", "always"],
        default=os.getenv("MINICODE_APPROVAL", "never"),
        help="How to handle commands that need approval.",
    )
    args = parser.parse_args(argv)

    workspace = Path(args.workspace).resolve()
    llm = OllamaClient(base_url=args.ollama_url)
    approvals = _build_approval_provider(args.approval)
    sandbox = DockerSandbox(workspace=workspace, image=args.docker_image, approvals=approvals)

    if args.check:
        return _check_environment(llm=llm, sandbox=sandbox, model=args.model)

    if not args.task:
        parser.error("task is required unless --check is used")

    agent = CodingAgent(
        llm=llm,
        sandbox=sandbox,
        config=AgentConfig(model=args.model, max_steps=args.max_steps),
    )

    result = agent.run(" ".join(args.task))
    print(result.answer)

    if args.transcript:
        transcript_path = Path(args.transcript)
        transcript_path.write_text(
            json.dumps(result.transcript, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

    return 0


def _build_approval_provider(mode: str):
    if mode == "ask":
        return ConsoleApproval()
    if mode == "always":
        return AlwaysApprove()
    return NeverApprove()


def _check_environment(llm: OllamaClient, sandbox: DockerSandbox, model: str) -> int:
    ok = True

    try:
        llm.chat(
            model=model,
            messages=[
                {"role": "system", "content": "Return JSON only."},
                {"role": "user", "content": '{"action":"finish","args":{"answer":"ok"}}'},
            ],
        )
        print(f"[ok] Ollama model: {model}")
    except Exception as exc:
        ok = False
        print(f"[fail] Ollama model: {exc}")

    try:
        result = sandbox.run("printf sandbox-ok")
        if result.exit_code == 0 and "sandbox-ok" in result.stdout:
            print(f"[ok] Docker sandbox image: {sandbox.image}")
        else:
            ok = False
            print(f"[fail] Docker sandbox: {result.stderr or result.stdout}")
    except Exception as exc:
        ok = False
        print(f"[fail] Docker sandbox: {exc}")

    return 0 if ok else 1
