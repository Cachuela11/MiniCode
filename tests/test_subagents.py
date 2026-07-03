import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from minicode.llm import LLMResponse
from minicode.observability import TokenUsage
from minicode.permissions import Decision
from minicode.security import ToolSecurityReviewer
from minicode.tools import ToolRegistry


class FakeSandbox:
    def __init__(self, workspace: Path):
        self.workspace = workspace

    def run(self, command: str):
        return SimpleNamespace(
            exit_code=0,
            stdout="ok",
            stderr="",
            permission_decision="allow",
            permission_reason="command allowed",
            dangerous_command=False,
            duration_ms=1,
        )


class SubAgentLlm:
    def chat_response(self, model, messages):
        text = "\n".join(message["content"] for message in messages)
        if "Observation:" in text:
            if "source-value" in text:
                content = json.dumps(
                    {
                        "thought": "source inspected",
                        "action": "finish",
                        "args": {"answer": "source report: found source-value in minicode/source.py"},
                    }
                )
            else:
                content = json.dumps(
                    {
                        "thought": "tests inspected",
                        "action": "finish",
                        "args": {"answer": "test report: found test-value in tests/test_source.py"},
                    }
                )
        elif "inspect source" in text:
            content = json.dumps(
                {
                    "thought": "read source",
                    "action": "read_file",
                    "args": {"path": "minicode/source.py", "start_line": 1, "limit": 20},
                }
            )
        else:
            content = json.dumps(
                {
                    "thought": "read tests",
                    "action": "read_file",
                    "args": {"path": "tests/test_source.py", "start_line": 1, "limit": 20},
                }
            )
        return LLMResponse(
            content=content,
            token_usage=TokenUsage(prompt_tokens=1, completion_tokens=1, total_tokens=2),
            duration_ms=1,
            raw={},
        )


class SubAgentTests(unittest.TestCase):
    def test_plan_subagents_approves_normalized_plan_without_running_llm(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = Path(temp_dir)
            registry = ToolRegistry(
                workspace=workspace,
                sandbox=FakeSandbox(workspace),
                llm=SubAgentLlm(),
                model="fake",
            )

            result = registry.execute(
                "plan_subagents",
                {
                    "goal": "review runtime",
                    "tasks": [
                        {
                            "name": "Inspect Runtime",
                            "task": "Inspect runtime flow.",
                            "context": "Main agent wants evidence before editing.",
                            "allowed_tools": ["read_file", "grep_files"],
                            "path_scope": ["."],
                            "max_steps": 3,
                        }
                    ],
                    "max_parallel": 2,
                },
            )

        payload = json.loads(result.output)
        self.assertTrue(result.ok)
        self.assertEqual(payload["status"], "approved")
        self.assertEqual(payload["approved_tasks"][0]["name"], "inspect_runtime")
        self.assertEqual(payload["next_action"]["action"], "run_subagents")
        self.assertIn("Main agent wants evidence", payload["approved_tasks"][0]["context"])

    def test_run_subagents_executes_parallel_read_only_tasks(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = Path(temp_dir)
            (workspace / "minicode").mkdir()
            (workspace / "tests").mkdir()
            (workspace / "minicode" / "source.py").write_text("source-value = 1\n", encoding="utf-8")
            (workspace / "tests" / "test_source.py").write_text("test-value = 1\n", encoding="utf-8")
            registry = ToolRegistry(
                workspace=workspace,
                sandbox=FakeSandbox(workspace),
                llm=SubAgentLlm(),
                model="fake",
            )

            result = registry.execute(
                "run_subagents",
                {
                    "tasks": [
                        {
                            "name": "inspect_source",
                            "task": "inspect source",
                            "allowed_tools": ["read_file"],
                            "path_scope": ["minicode/"],
                            "max_steps": 3,
                        },
                        {
                            "name": "inspect_tests",
                            "task": "inspect tests",
                            "allowed_tools": ["read_file"],
                            "path_scope": ["tests/"],
                            "max_steps": 3,
                        },
                    ],
                    "max_parallel": 2,
                },
            )

        self.assertTrue(result.ok)
        self.assertIn("source report", result.output)
        self.assertIn("test report", result.output)
        self.assertIsNotNone(result.subagent_trace)
        self.assertEqual(len(result.subagent_trace["results"]), 2)

    def test_security_rejects_forbidden_subagent_tool(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            reviewer = ToolSecurityReviewer(Path(temp_dir))

            result = reviewer.review(
                "plan_subagents",
                {
                    "tasks": [
                        {
                            "name": "writer",
                            "task": "write something",
                            "allowed_tools": ["write_file"],
                            "path_scope": ["."],
                        }
                    ]
                },
            )

        self.assertEqual(result.decision, Decision.DENY)
        self.assertTrue(result.dangerous)

    def test_grep_files_searches_workspace_text(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = Path(temp_dir)
            (workspace / "pkg").mkdir()
            (workspace / "pkg" / "demo.py").write_text("def hello():\n    return 'needle'\n", encoding="utf-8")
            registry = ToolRegistry(workspace=workspace, sandbox=FakeSandbox(workspace))

            result = registry.execute("grep_files", {"pattern": "needle", "path": "pkg", "limit": 10})

        self.assertTrue(result.ok)
        self.assertIn("pkg/demo.py:2", result.output)


if __name__ == "__main__":
    unittest.main()
