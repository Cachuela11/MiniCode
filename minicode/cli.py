from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from .agent import AgentConfig, CodingAgent
from .eval import run_eval
from .llm import DeepSeekClient
from .permissions import AlwaysApprove, ConsoleApproval, NeverApprove
from .sandbox import DockerSandbox


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the MiniCode coding agent.")
    parser.add_argument("task", nargs="*", help="Coding task for the agent.")
    parser.add_argument("--check", action="store_true", help="Run environment checks and exit.")
    parser.add_argument("--model", default=os.getenv("MINICODE_MODEL", "deepseek-v4-flash"))
    parser.add_argument(
        "--deepseek-url",
        default=os.getenv("MINICODE_DEEPSEEK_URL", "https://api.deepseek.com"),
        help="DeepSeek OpenAI-compatible API base URL.",
    )
    parser.add_argument(
        "--deepseek-api-key",
        default=os.getenv("DEEPSEEK_API_KEY"),
        help="DeepSeek API key. Defaults to DEEPSEEK_API_KEY.",
    )
    parser.add_argument(
        "--llm-timeout",
        type=int,
        default=int(os.getenv("MINICODE_LLM_TIMEOUT", "120")),
        help="Seconds to wait for one LLM response.",
    )
    parser.add_argument(
        "--max-tokens",
        type=int,
        default=int(os.getenv("MINICODE_MAX_TOKENS", "4096")),
        help="Maximum completion tokens for API providers.",
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
        "--run-log",
        default=os.getenv("MINICODE_RUN_LOG"),
        help="Optional path to write the structured runtime log JSON.",
    )
    parser.add_argument(
        "--final-test-command",
        default=os.getenv("MINICODE_FINAL_TEST_COMMAND"),
        help="Optional command to run after the agent finishes.",
    )
    parser.add_argument(
        "--eval",
        action="store_true",
        help="Run the built-in eval task suite instead of a single task.",
    )
    parser.add_argument(
        "--eval-output",
        default=os.getenv("MINICODE_EVAL_OUTPUT", ".minicode/eval-report.json"),
        help="Path to write eval results JSON.",
    )
    parser.add_argument(
        "--approval",
        choices=["never", "ask", "always"],
        default=os.getenv("MINICODE_APPROVAL", "never"),
        help="How to handle commands that need approval.",
    )
    args = parser.parse_args(argv)

    workspace = Path(args.workspace).resolve()
    llm = _build_llm(args)
    approvals = _build_approval_provider(args.approval)
    sandbox = DockerSandbox(workspace=workspace, image=args.docker_image, approvals=approvals)

    if args.check:
        return _check_environment(llm=llm, sandbox=sandbox, model=args.model)

    if args.eval:
        report = run_eval(
            model=args.model,
            deepseek_url=args.deepseek_url,
            deepseek_api_key=args.deepseek_api_key,
            llm_timeout=args.llm_timeout,
            max_tokens=args.max_tokens,
            docker_image=args.docker_image,
            approvals=approvals,
            output_path=Path(args.eval_output),
            max_steps=args.max_steps,
        )
        print(json.dumps(report.summary, indent=2, ensure_ascii=False))
        print(f"Eval report written to {args.eval_output}")
        return 0

    if not args.task:
        parser.error("task is required unless --check or --eval is used")

    agent = CodingAgent(
        llm=llm,
        sandbox=sandbox,
        config=AgentConfig(
            model=args.model,
            max_steps=args.max_steps,
            final_test_command=args.final_test_command,
        ),
    )

    try:
        result = agent.run(" ".join(args.task))
    except KeyboardInterrupt:
        print("Interrupted while waiting for the agent. No run log was written for this run.")
        return 130
    except RuntimeError as exc:
        print(f"ERROR: {exc}")
        return 1
    print(result.answer)

    if args.transcript:
        transcript_path = Path(args.transcript)
        transcript_path.write_text(
            json.dumps(result.transcript, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
    if args.run_log and result.run_log:
        run_log_path = Path(args.run_log)
        run_log_path.parent.mkdir(parents=True, exist_ok=True)
        run_log_path.write_text(
            json.dumps(result.run_log.to_dict(), indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

    return 0


def _build_approval_provider(mode: str):
    if mode == "ask":
        return ConsoleApproval()
    if mode == "always":
        return AlwaysApprove()
    return NeverApprove()


def _build_llm(args):
    return DeepSeekClient(
        api_key=args.deepseek_api_key,
        base_url=args.deepseek_url,
        timeout=args.llm_timeout,
        max_tokens=args.max_tokens,
    )


def _check_environment(llm, sandbox: DockerSandbox, model: str) -> int:
    ok = True

    try:
        llm.chat(
            model=model,
            messages=[
                {"role": "system", "content": "Return JSON only."},
                {"role": "user", "content": '{"action":"finish","args":{"answer":"ok"}}'},
            ],
        )
        print(f"[ok] deepseek model: {model}")
    except Exception as exc:
        ok = False
        print(f"[fail] deepseek model: {exc}")

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
