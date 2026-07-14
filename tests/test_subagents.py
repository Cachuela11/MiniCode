import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from minicode.llm import LLMResponse
from minicode.observability import TokenUsage
from minicode.permissions import Decision
from minicode.security import ToolSecurityReviewer
from minicode.subagents import STAGE_HANDOFF_LIMIT, SUBAGENT_SUMMARY_LIMIT
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
            if "test-value" in text:
                content = json.dumps(
                    {
                        "thought": "tests inspected",
                        "action": "finish",
                        "args": {"answer": "test report: found test-value in tests/test_source.py"},
                    }
                )
            elif "source-value" in text:
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
                        "thought": "nothing inspected",
                        "action": "finish",
                        "args": {"answer": "no useful report"},
                    }
                )
        elif "list workspace" in text:
            content = json.dumps(
                {
                    "thought": "list files",
                    "action": "list_files",
                    "args": {"path": ".", "max_depth": 1, "limit": 20},
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


class VerboseStructuredSubAgentLlm:
    def chat_response(self, model, messages):
        answer = {
            "summary": "Structured summary " + ("x" * 500),
            "findings": [
                {"file": f"file_{index}.py", "line": index, "fact": "fact " + ("y" * 250)}
                for index in range(1, 8)
            ],
            "handoff": ["handoff " + ("z" * 250) for _ in range(6)],
            "next": ["next " + ("n" * 250) for _ in range(6)],
        }
        content = json.dumps({"thought": "done", "action": "finish", "args": {"answer": answer}})
        return LLMResponse(
            content=content,
            token_usage=TokenUsage(prompt_tokens=1, completion_tokens=1, total_tokens=2),
            duration_ms=1,
            raw={},
        )


class SubAgentTests(unittest.TestCase):
    def test_plan_subagent_workflow_approves_staged_plan(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = Path(temp_dir)
            registry = ToolRegistry(workspace=workspace, sandbox=FakeSandbox(workspace), llm=SubAgentLlm(), model="fake")

            result = registry.execute(
                "plan_subagent_workflow",
                {
                    "goal": "review runtime",
                    "stages": [
                        {
                            "name": "Locate",
                            "nodes": [
                                {
                                    "name": "Inspect Source",
                                    "task": "inspect source",
                                    "context": "Find relevant source.",
                                    "allowed_tools": ["read_file"],
                                    "path_scope": ["."],
                                    "max_steps": 3,
                                }
                            ],
                        }
                    ],
                    "max_parallel_per_stage": 2,
                },
            )

        payload = json.loads(result.output)
        self.assertTrue(result.ok)
        self.assertEqual(payload["status"], "approved")
        self.assertEqual(payload["approved_stages"][0]["name"], "locate")
        self.assertEqual(payload["approved_stages"][0]["nodes"][0]["name"], "inspect_source")
        self.assertEqual(payload["next_action"]["action"], "run_subagent_workflow")

    def test_workflow_rejects_more_than_four_stages(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = Path(temp_dir)
            registry = ToolRegistry(workspace=workspace, sandbox=FakeSandbox(workspace), llm=SubAgentLlm(), model="fake")

            result = registry.execute(
                "plan_subagent_workflow",
                {
                    "stages": [
                        {
                            "name": f"stage_{index}",
                            "nodes": [{"name": "node", "task": "inspect", "path_scope": ["."]}],
                        }
                        for index in range(5)
                    ]
                },
            )

        self.assertFalse(result.ok)
        self.assertIn("at most 4 stages", result.output)

    def test_workflow_rejects_more_than_three_nodes_per_stage(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = Path(temp_dir)
            registry = ToolRegistry(workspace=workspace, sandbox=FakeSandbox(workspace), llm=SubAgentLlm(), model="fake")

            result = registry.execute(
                "plan_subagent_workflow",
                {
                    "stages": [
                        {
                            "name": "too_many",
                            "nodes": [
                                {"name": f"node_{index}", "task": "inspect", "path_scope": ["."]}
                                for index in range(4)
                            ],
                        }
                    ]
                },
            )

        self.assertFalse(result.ok)
        self.assertIn("at most 3", result.output)

    def test_run_subagent_workflow_runs_stages_and_passes_handoff_context(self):
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
                "run_subagent_workflow",
                {
                    "stages": [
                        {
                            "name": "locate",
                            "nodes": [
                                {
                                    "name": "inspect_source",
                                    "task": "inspect source",
                                    "context": "Stage one.",
                                    "allowed_tools": ["read_file"],
                                    "path_scope": ["minicode/"],
                                    "max_steps": 3,
                                }
                            ],
                        },
                        {
                            "name": "verify",
                            "nodes": [
                                {
                                    "name": "inspect_tests",
                                    "task": "inspect tests",
                                    "context": "Use prior handoff.",
                                    "allowed_tools": ["read_file"],
                                    "path_scope": ["tests/"],
                                    "max_steps": 3,
                                }
                            ],
                        },
                    ],
                    "max_parallel_per_stage": 2,
                },
            )

        payload = json.loads(result.output)
        self.assertTrue(result.ok)
        self.assertEqual(payload["status"], "completed")
        self.assertEqual(len(payload["stages"]), 2)
        self.assertIn("source report", payload["stages"][0]["handoff_context"])
        self.assertIn("test report", payload["final_handoff_context"])
        self.assertIsNotNone(result.subagent_trace)
        self.assertEqual(len(result.subagent_trace["stages"]), 2)

    def test_stage_control_edges_run_serial_nodes_inside_one_stage(self):
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
                "run_subagent_workflow",
                {
                    "stages": [
                        {
                            "name": "controlled",
                            "nodes": [
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
                            "entry_nodes": ["inspect_source"],
                            "edges": [
                                {
                                    "from": "inspect_source",
                                    "to": "inspect_tests",
                                    "condition": "on_success",
                                    "max_traversals": 1,
                                }
                            ],
                            "max_iterations": 3,
                        }
                    ],
                    "max_parallel_per_stage": 2,
                },
            )

        payload = json.loads(result.output)
        self.assertTrue(result.ok)
        stage = payload["stages"][0]
        self.assertEqual(stage["control_flow"]["mode"], "controlled_graph")
        self.assertEqual(stage["control_flow"]["iterations"], 2)
        self.assertEqual([item["name"] for item in stage["results"]], ["inspect_source", "inspect_tests"])

    def test_stage_loop_stops_at_max_iterations(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = Path(temp_dir)
            (workspace / "README.md").write_text("public\n", encoding="utf-8")
            registry = ToolRegistry(
                workspace=workspace,
                sandbox=FakeSandbox(workspace),
                llm=VerboseStructuredSubAgentLlm(),
                model="fake",
            )

            result = registry.execute(
                "run_subagent_workflow",
                {
                    "stages": [
                        {
                            "name": "loop_guard",
                            "nodes": [
                                {
                                    "name": "retry_node",
                                    "task": "return structured summary",
                                    "allowed_tools": ["list_files"],
                                    "path_scope": ["."],
                                    "max_steps": 1,
                                }
                            ],
                            "entry_nodes": ["retry_node"],
                            "edges": [
                                {
                                    "from": "retry_node",
                                    "to": "retry_node",
                                    "condition": "always",
                                    "max_traversals": 2,
                                }
                            ],
                            "max_iterations": 1,
                        }
                    ]
                },
            )

        payload = json.loads(result.output)
        stage = payload["stages"][0]
        self.assertFalse(result.ok)
        self.assertEqual(payload["status"], "partial")
        self.assertTrue(stage["control_flow"]["stopped_by_guard"])
        self.assertEqual(stage["control_flow"]["iterations"], 1)

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
        isolation = result.subagent_trace["results"][0]["workspace_isolation"]
        self.assertEqual(isolation["mode"], "snapshot")
        self.assertTrue(isolation["created"])
        self.assertTrue(isolation["destroyed"])
        self.assertFalse(Path(isolation["snapshot_workspace"]).exists())

    def test_subagent_structured_summary_is_compacted(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = Path(temp_dir)
            registry = ToolRegistry(
                workspace=workspace,
                sandbox=FakeSandbox(workspace),
                llm=VerboseStructuredSubAgentLlm(),
                model="fake",
            )

            result = registry.execute(
                "run_subagents",
                {
                    "tasks": [
                        {
                            "name": "verbose",
                            "task": "return structured summary",
                            "allowed_tools": ["read_file"],
                            "path_scope": ["."],
                            "max_steps": 1,
                        }
                    ]
                },
            )

        payload = json.loads(result.output)
        summary = payload["results"][0]["summary"]
        structured = json.loads(summary)
        self.assertLessEqual(len(summary), SUBAGENT_SUMMARY_LIMIT)
        self.assertEqual(set(structured), {"summary", "findings", "handoff", "next"})
        self.assertLessEqual(len(structured["findings"]), 3)
        self.assertLessEqual(len(structured["handoff"]), 3)
        self.assertLessEqual(len(structured["next"]), 3)

    def test_workflow_handoff_context_is_limited(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = Path(temp_dir)
            registry = ToolRegistry(
                workspace=workspace,
                sandbox=FakeSandbox(workspace),
                llm=VerboseStructuredSubAgentLlm(),
                model="fake",
            )

            result = registry.execute(
                "run_subagent_workflow",
                {
                    "stages": [
                        {
                            "name": "first",
                            "nodes": [
                                {"name": "node_a", "task": "summarize", "path_scope": ["."]},
                                {"name": "node_b", "task": "summarize", "path_scope": ["."]},
                                {"name": "node_c", "task": "summarize", "path_scope": ["."]},
                            ],
                        },
                        {
                            "name": "second",
                            "nodes": [{"name": "node_d", "task": "summarize", "path_scope": ["."]}],
                        },
                    ]
                },
            )

        payload = json.loads(result.output)
        self.assertTrue(result.ok)
        self.assertLessEqual(len(payload["stages"][0]["handoff_context"]), STAGE_HANDOFF_LIMIT)
        self.assertLessEqual(len(payload["final_handoff_context"]), STAGE_HANDOFF_LIMIT)

    def test_subagent_snapshot_skips_secret_files(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = Path(temp_dir)
            (workspace / ".env").write_text("SECRET=1\n", encoding="utf-8")
            (workspace / "README.md").write_text("public\n", encoding="utf-8")
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
                            "name": "inspect_readme",
                            "task": "list workspace",
                            "allowed_tools": ["list_files"],
                            "path_scope": ["."],
                            "max_steps": 1,
                        }
                    ]
                },
            )

        trace = result.subagent_trace["results"][0]["trace"][0]
        self.assertNotIn(".env", trace["output_preview"])

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

    def test_security_rejects_forbidden_workflow_node_tool(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            reviewer = ToolSecurityReviewer(Path(temp_dir))

            result = reviewer.review(
                "plan_subagent_workflow",
                {
                    "stages": [
                        {
                            "name": "bad",
                            "nodes": [
                                {
                                    "name": "writer",
                                    "task": "write something",
                                    "allowed_tools": ["write_file"],
                                    "path_scope": ["."],
                                }
                            ],
                        }
                    ]
                },
            )

        self.assertEqual(result.decision, Decision.DENY)
        self.assertTrue(result.dangerous)

    def test_security_rejects_unknown_workflow_edge_node(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            reviewer = ToolSecurityReviewer(Path(temp_dir))

            result = reviewer.review(
                "plan_subagent_workflow",
                {
                    "stages": [
                        {
                            "name": "bad_edge",
                            "nodes": [{"name": "known", "task": "inspect", "path_scope": ["."]}],
                            "edges": [{"from": "known", "to": "missing", "condition": "always"}],
                        }
                    ]
                },
            )

        self.assertEqual(result.decision, Decision.DENY)
        self.assertTrue(result.invalid)

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
