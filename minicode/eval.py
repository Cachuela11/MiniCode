from __future__ import annotations

import json
import shutil
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .agent import AgentConfig, CodingAgent
from .llm import DeepSeekClient
from .observability import RunLog
from .permissions import ApprovalProvider
from .sandbox import DockerSandbox
from .skills import SkillCatalog


@dataclass(frozen=True)
class EvalCase:
    id: str
    category: str
    task: str
    files: dict[str, str]
    test_command: str


@dataclass
class EvalRun:
    case_id: str
    category: str
    success: bool
    test_passed: bool
    tool_calls: int
    invalid_commands: int
    modified_files: list[str]
    token_usage: dict[str, int]
    duration_ms: int
    dangerous_commands: int
    log_path: str


@dataclass
class EvalReport:
    summary: dict[str, Any]
    runs: list[EvalRun]

    def to_dict(self) -> dict[str, Any]:
        return {
            "summary": self.summary,
            "runs": [asdict(run) for run in self.runs],
        }


def run_eval(
    model: str,
    deepseek_url: str,
    deepseek_api_key: str | None,
    llm_timeout: int,
    max_tokens: int,
    docker_image: str,
    approvals: ApprovalProvider,
    output_path: Path,
    max_steps: int,
    skills_dir: Path,
    skills_enabled: bool,
    max_skills: int,
    skill_recall_k: int,
    context_artifact_dir: str = ".minicode/context-artifacts",
    observation_inline_limit: int = 6000,
    observation_preview_chars: int = 1200,
    context_history_char_limit: int = 24000,
    context_keep_recent_messages: int = 6,
    context_note_char_limit: int = 6000,
    memory_dir: str = ".minicode/memory",
    memory_trigger_mode: str = "on",
    memory_min_confidence: float = 0.7,
    memory_max_candidates: int = 5,
) -> EvalReport:
    output_path = output_path.resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    runs_dir = output_path.parent / "eval-runs"
    logs_dir = output_path.parent / "eval-logs"
    runs_dir.mkdir(parents=True, exist_ok=True)
    logs_dir.mkdir(parents=True, exist_ok=True)
    skill_catalog = SkillCatalog.load(skills_dir) if skills_enabled else SkillCatalog.empty()

    runs: list[EvalRun] = []
    for case in _cases():
        workspace = runs_dir / case.id
        if workspace.exists():
            shutil.rmtree(workspace)
        workspace.mkdir(parents=True)
        _write_case_files(workspace, case.files)

        sandbox = DockerSandbox(workspace=workspace, image=docker_image, approvals=approvals)
        agent = CodingAgent(
            llm=_build_llm(
                deepseek_url=deepseek_url,
                deepseek_api_key=deepseek_api_key,
                llm_timeout=llm_timeout,
                max_tokens=max_tokens,
            ),
            sandbox=sandbox,
            config=AgentConfig(
                model=model,
                max_steps=max_steps,
                final_test_command=case.test_command,
                skills_enabled=skills_enabled,
                max_skills=max_skills,
                skill_recall_k=skill_recall_k,
                context_artifact_dir=context_artifact_dir,
                observation_inline_limit=observation_inline_limit,
                observation_preview_chars=observation_preview_chars,
                context_history_char_limit=context_history_char_limit,
                context_keep_recent_messages=context_keep_recent_messages,
                context_note_char_limit=context_note_char_limit,
                memory_dir=memory_dir,
                memory_trigger_mode=memory_trigger_mode,
                memory_min_confidence=memory_min_confidence,
                memory_max_candidates=memory_max_candidates,
            ),
            skill_catalog=skill_catalog,
        )
        result = agent.run(case.task)
        run_log = result.run_log
        if run_log is None:
            raise RuntimeError("agent did not return a run log")

        log_path = logs_dir / f"{case.id}.json"
        log_path.write_text(
            json.dumps(run_log.to_dict(), indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        runs.append(_summarize_run(case, run_log, log_path))

    report = EvalReport(summary=_summarize_report(runs), runs=runs)
    output_path.write_text(
        json.dumps(report.to_dict(), indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    return report


def _build_llm(
    deepseek_url: str,
    deepseek_api_key: str | None,
    llm_timeout: int,
    max_tokens: int,
):
    return DeepSeekClient(
        api_key=deepseek_api_key,
        base_url=deepseek_url,
        timeout=llm_timeout,
        max_tokens=max_tokens,
    )


def _summarize_run(case: EvalCase, run_log: RunLog, log_path: Path) -> EvalRun:
    final_test = run_log.final_test_result
    test_passed = bool(final_test and final_test.passed)
    tool_steps = [step for step in run_log.steps if step.tool_name != "finish"]
    modified_files = sorted({path for step in run_log.steps for path in step.modified_files})
    invalid_commands = sum(1 for step in run_log.steps if step.invalid_command)
    dangerous_commands = sum(1 for step in run_log.steps if step.dangerous_command)
    return EvalRun(
        case_id=case.id,
        category=case.category,
        success=test_passed,
        test_passed=test_passed,
        tool_calls=len(tool_steps),
        invalid_commands=invalid_commands,
        modified_files=modified_files,
        token_usage=asdict(run_log.token_usage),
        duration_ms=run_log.duration_ms,
        dangerous_commands=dangerous_commands,
        log_path=str(log_path),
    )


def _summarize_report(runs: list[EvalRun]) -> dict[str, Any]:
    total = len(runs)
    successes = sum(1 for run in runs if run.success)
    test_passes = sum(1 for run in runs if run.test_passed)
    tool_calls = sum(run.tool_calls for run in runs)
    modified_file_count = sum(len(run.modified_files) for run in runs)
    prompt_tokens = sum(run.token_usage["prompt_tokens"] for run in runs)
    completion_tokens = sum(run.token_usage["completion_tokens"] for run in runs)
    total_tokens = sum(run.token_usage["total_tokens"] for run in runs)
    dangerous_commands = sum(run.dangerous_commands for run in runs)
    return {
        "task_count": total,
        "task_success_rate": _rate(successes, total),
        "test_pass_rate": _rate(test_passes, total),
        "average_tool_calls": tool_calls / total if total else 0,
        "invalid_command_count": sum(run.invalid_commands for run in runs),
        "modified_file_count": modified_file_count,
        "token_cost": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": total_tokens,
        },
        "total_duration_ms": sum(run.duration_ms for run in runs),
        "dangerous_command_count": dangerous_commands,
        "dangerous_command_seen": dangerous_commands > 0,
    }


def _rate(count: int, total: int) -> float:
    return count / total if total else 0.0


def _write_case_files(workspace: Path, files: dict[str, str]) -> None:
    for relative_path, content in files.items():
        path = workspace / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")


def _cases() -> list[EvalCase]:
    return [
        EvalCase(
            id="fix_failing_unit_test",
            category="fix failing unit test",
            task="Fix the failing unit test. Keep the public function name the same.",
            test_command="python -m unittest discover -s tests",
            files={
                "app/math_utils.py": "def add(left: int, right: int) -> int:\n    return left - right\n",
                "tests/test_math_utils.py": (
                    "import unittest\n"
                    "from app.math_utils import add\n\n"
                    "class MathUtilsTests(unittest.TestCase):\n"
                    "    def test_adds_two_numbers(self):\n"
                    "        self.assertEqual(add(2, 3), 5)\n\n"
                    "if __name__ == '__main__':\n"
                    "    unittest.main()\n"
                ),
            },
        ),
        EvalCase(
            id="add_api",
            category="add API",
            task="Add a public multiply(left, right) API in app/math_utils.py so the tests pass.",
            test_command="python -m unittest discover -s tests",
            files={
                "app/math_utils.py": "def add(left: int, right: int) -> int:\n    return left + right\n",
                "tests/test_math_utils.py": (
                    "import unittest\n"
                    "from app.math_utils import add, multiply\n\n"
                    "class MathUtilsTests(unittest.TestCase):\n"
                    "    def test_add(self):\n"
                    "        self.assertEqual(add(2, 3), 5)\n"
                    "    def test_multiply(self):\n"
                    "        self.assertEqual(multiply(4, 5), 20)\n\n"
                    "if __name__ == '__main__':\n"
                    "    unittest.main()\n"
                ),
            },
        ),
        EvalCase(
            id="fix_type_error",
            category="fix type error",
            task="Fix the type error so normalize_user returns a string for valid users.",
            test_command="python -m unittest discover -s tests",
            files={
                "app/users.py": (
                    "def normalize_user(user: dict) -> str:\n"
                    "    name = user.get('name')\n"
                    "    return name.strip()\n"
                ),
                "tests/test_users.py": (
                    "import unittest\n"
                    "from app.users import normalize_user\n\n"
                    "class UserTests(unittest.TestCase):\n"
                    "    def test_normalizes_name(self):\n"
                    "        self.assertEqual(normalize_user({'name': ' Ada '}), 'Ada')\n\n"
                    "if __name__ == '__main__':\n"
                    "    unittest.main()\n"
                ),
            },
        ),
        EvalCase(
            id="refactor_function",
            category="refactor function",
            task="Refactor summarize_numbers to be clearer while preserving behavior.",
            test_command="python -m unittest discover -s tests",
            files={
                "app/stats.py": (
                    "def summarize_numbers(values):\n"
                    "    s = 0\n"
                    "    c = 0\n"
                    "    for v in values:\n"
                    "        s = s + v\n"
                    "        c = c + 1\n"
                    "    return {'count': c, 'total': s, 'average': s / c if c else 0}\n"
                ),
                "tests/test_stats.py": (
                    "import unittest\n"
                    "from app.stats import summarize_numbers\n\n"
                    "class StatsTests(unittest.TestCase):\n"
                    "    def test_summary(self):\n"
                    "        self.assertEqual(summarize_numbers([2, 4, 6]), {'count': 3, 'total': 12, 'average': 4})\n"
                    "    def test_empty(self):\n"
                    "        self.assertEqual(summarize_numbers([]), {'count': 0, 'total': 0, 'average': 0})\n\n"
                    "if __name__ == '__main__':\n"
                    "    unittest.main()\n"
                ),
            },
        ),
        EvalCase(
            id="add_input_validation",
            category="add input validation",
            task="Add input validation so parse_port accepts only integers from 1 through 65535.",
            test_command="python -m unittest discover -s tests",
            files={
                "app/config.py": (
                    "def parse_port(value):\n"
                    "    return int(value)\n"
                ),
                "tests/test_config.py": (
                    "import unittest\n"
                    "from app.config import parse_port\n\n"
                    "class ConfigTests(unittest.TestCase):\n"
                    "    def test_valid_port(self):\n"
                    "        self.assertEqual(parse_port('8080'), 8080)\n"
                    "    def test_rejects_zero(self):\n"
                    "        with self.assertRaises(ValueError):\n"
                    "            parse_port('0')\n"
                    "    def test_rejects_too_large(self):\n"
                    "        with self.assertRaises(ValueError):\n"
                    "            parse_port('70000')\n\n"
                    "if __name__ == '__main__':\n"
                    "    unittest.main()\n"
                ),
            },
        ),
    ]
