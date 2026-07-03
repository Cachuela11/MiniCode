from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from minicode.llm import LLMResponse
from minicode.observability import TokenUsage
from minicode.skill_evolution import SkillEvolution, SkillEvolutionConfig
from minicode.skills import SkillCatalog


class FakeSkillEvolutionLLM:
    def __init__(self, content: str):
        self.content = content
        self.calls = 0

    def chat_response(self, model: str, messages: list[dict[str, str]]) -> LLMResponse:
        self.calls += 1
        return LLMResponse(
            content=self.content,
            token_usage=TokenUsage(prompt_tokens=10, completion_tokens=5, total_tokens=15),
            duration_ms=1,
            raw={},
        )


class SkillEvolutionTests(unittest.TestCase):
    def test_generates_draft_skill_from_successful_run_log(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir)
            runs_dir = workspace / ".minicode" / "runs"
            skills_dir = workspace / ".skills"
            runs_dir.mkdir(parents=True)
            skills_dir.mkdir()
            (skills_dir / "existing.md").write_text(
                """---
name: "existing"
description: "Existing active skill."
tags: ["test"]
intents: ["test"]
tools: ["run_tests"]
triggers: ["pytest"]
---
## Workflow
Run tests.
""",
                encoding="utf-8",
            )
            (runs_dir / "run.json").write_text(
                json.dumps(
                    {
                        "run_id": "run_1",
                        "task": "修复失败的单元测试",
                        "answer": "测试已经修复。",
                        "started_at": "2026-07-03T00:00:00+00:00",
                        "steps": [
                            {
                                "step": 1,
                                "tool_name": "read_file",
                                "tool_args": {"path": "tests/test_demo.py"},
                                "stdout": "assert add(1, 1) == 3",
                                "stderr": "",
                                "exit_code": 0,
                                "modified_files": [],
                            },
                            {
                                "step": 2,
                                "tool_name": "write_file",
                                "tool_args": {"path": "demo.py", "content": "secret text"},
                                "stdout": "Wrote demo.py",
                                "stderr": "",
                                "exit_code": 0,
                                "modified_files": ["demo.py"],
                            },
                            {
                                "step": 3,
                                "tool_name": "run_tests",
                                "tool_args": {"command": "python -m unittest"},
                                "stdout": "OK",
                                "stderr": "",
                                "exit_code": 0,
                                "modified_files": [],
                            },
                            {
                                "step": 4,
                                "tool_name": "finish",
                                "tool_args": {"answer": "done"},
                                "stdout": "done",
                                "stderr": "",
                                "exit_code": 0,
                                "modified_files": [],
                            },
                        ],
                        "final_test_result": {"passed": True},
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            llm = FakeSkillEvolutionLLM(
                json.dumps(
                    {
                        "operation": "create",
                        "target_skill": "",
                        "name": "unit_test_repair_flow",
                        "description": "Repair failing Python unit tests.",
                        "tags": ["python", "tests"],
                        "intents": ["test_repair"],
                        "tools": ["read_file", "write_file", "run_tests"],
                        "triggers": ["failing unit test"],
                        "workflow": ["Read the failing test.", "Patch the implementation.", "Run tests."],
                        "boundaries": ["Do not rewrite unrelated code."],
                        "completion_criteria": ["Relevant tests pass."],
                        "reason": "Reusable repair workflow.",
                    }
                )
            )

            result = SkillEvolution(
                llm=llm,
                model="fake-model",
                workspace=workspace,
                config=SkillEvolutionConfig(max_runs=10),
            ).run()

            self.assertEqual(result.status, "completed")
            self.assertEqual(result.inspected_runs, 1)
            self.assertEqual(result.eligible_runs, 1)
            self.assertEqual(len(result.drafts), 1)
            self.assertEqual(llm.calls, 1)
            draft_path = Path(result.drafts[0].path)
            self.assertTrue(draft_path.exists())
            self.assertIn("_drafts", draft_path.parts)
            text = draft_path.read_text(encoding="utf-8")
            self.assertIn('name: "unit_test_repair_flow"', text)
            self.assertIn("Review manually", text)
            self.assertNotIn("secret text", text)
            self.assertEqual(SkillCatalog.load(skills_dir).names(), ["existing"])

    def test_skips_unsafe_run_without_calling_llm(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir)
            runs_dir = workspace / ".minicode" / "runs"
            runs_dir.mkdir(parents=True)
            (runs_dir / "run.json").write_text(
                json.dumps(
                    {
                        "task": "delete files",
                        "answer": "blocked",
                        "steps": [
                            {
                                "step": 1,
                                "tool_name": "run_shell",
                                "tool_args": {"command": "rm -rf ."},
                                "dangerous_command": True,
                                "modified_files": [],
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            llm = FakeSkillEvolutionLLM("{}")

            result = SkillEvolution(llm=llm, model="fake-model", workspace=workspace).run()

            self.assertEqual(result.status, "completed")
            self.assertEqual(result.eligible_runs, 0)
            self.assertEqual(result.drafts, [])
            self.assertEqual(llm.calls, 0)


if __name__ == "__main__":
    unittest.main()
