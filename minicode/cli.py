from __future__ import annotations

import argparse
import json
import os
import re
from datetime import datetime
from pathlib import Path

from .agent import AgentConfig, CodingAgent
from .eval import run_eval
from .llm import DeepSeekClient
from .permissions import AlwaysApprove, ConsoleApproval, NeverApprove
from .sandbox import DockerSandbox
from .skills import SkillCatalog


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
        default=os.getenv("MINICODE_RUN_LOG", ".minicode/runs"),
        help="Path or directory for persistent structured run logs.",
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
    parser.add_argument(
        "--skills-dir",
        default=os.getenv("MINICODE_SKILLS_DIR", ".skills"),
        help="Directory containing skill markdown files.",
    )
    parser.add_argument(
        "--disable-skills",
        action="store_true",
        help="Disable skill routing and prompt injection.",
    )
    parser.add_argument(
        "--max-skills",
        type=int,
        default=int(os.getenv("MINICODE_MAX_SKILLS", "2")),
        help="Maximum selected skills to inject into the prompt.",
    )
    args = parser.parse_args(argv)

    workspace = Path(args.workspace).resolve()
    llm = _build_llm(args)
    approvals = _build_approval_provider(args.approval)
    sandbox = DockerSandbox(workspace=workspace, image=args.docker_image, approvals=approvals)
    skill_catalog = SkillCatalog.load(Path(args.skills_dir)) if not args.disable_skills else SkillCatalog.empty()

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
            skills_dir=Path(args.skills_dir),
            skills_enabled=not args.disable_skills,
            max_skills=args.max_skills,
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
            skills_enabled=not args.disable_skills,
            max_skills=args.max_skills,
        ),
        skill_catalog=skill_catalog,
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
        run_log_path = _write_run_log(Path(args.run_log), result.run_log.to_dict())
        print(f"Run log written to {run_log_path}")

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


def _write_run_log(target: Path, payload: dict) -> Path:
    path = _resolve_run_log_path(target, payload)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    return path


def _resolve_run_log_path(target: Path, payload: dict) -> Path:
    if target.suffix.lower() == ".json":
        return _avoid_overwrite(target)

    task = str(payload.get("task") or "run")
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    filename = f"{timestamp}-{_slugify(task)}.json"
    return _avoid_overwrite(target / filename)


def _avoid_overwrite(path: Path) -> Path:
    if not path.exists():
        return path
    stem = path.stem
    suffix = path.suffix
    for index in range(1, 1000):
        candidate = path.with_name(f"{stem}-{index}{suffix}")
        if not candidate.exists():
            return candidate
    raise RuntimeError(f"could not find available log filename for {path}")


def _slugify(value: str, limit: int = 60) -> str:
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", value).strip("-._")
    return (slug or "run")[:limit]


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
