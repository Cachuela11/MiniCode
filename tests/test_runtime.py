import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from minicode.agent import AgentConfig, AgentResult, CodingAgent
from minicode.llm import LLMResponse
from minicode.observability import TokenUsage
from minicode.skills import SkillCatalog


class FakeSandbox:
    def __init__(self, workspace: Path):
        self.workspace = workspace.resolve()

    def run(self, command: str):
        return SimpleNamespace(exit_code=0, stdout="/workspace\n./README.md\n", stderr="")


class ToolThenFinishLlm:
    def __init__(self):
        self.calls = 0

    def chat_response(self, model, messages):
        self.calls += 1
        if self.calls == 1:
            content = (
                '{"thought":"inspect","action":"list_files",'
                '"args":{"path":".","max_depth":1,"limit":10}}'
            )
        else:
            self.assert_observation_was_sent(messages)
            content = '{"thought":"done","action":"finish","args":{"answer":"runtime ok"}}'
        return LLMResponse(
            content=content,
            token_usage=TokenUsage(prompt_tokens=2, completion_tokens=3, total_tokens=5),
            duration_ms=1,
            raw={},
        )

    def assert_observation_was_sent(self, messages):
        if not any("Observation:" in message["content"] for message in messages):
            raise AssertionError("expected tool observation in follow-up model input")


class RuntimeTests(unittest.TestCase):
    def test_agent_run_executes_tool_loop_and_finishes(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = Path(temp_dir)
            (workspace / "README.md").write_text("# test\n", encoding="utf-8")
            llm = ToolThenFinishLlm()
            agent = CodingAgent(
                llm=llm,
                sandbox=FakeSandbox(workspace),
                config=AgentConfig(
                    model="fake",
                    skills_enabled=False,
                    memory_trigger_mode="off",
                    dreaming_mode="off",
                ),
                skill_catalog=SkillCatalog.empty(),
            )

            result = agent.run("list files then answer")

        self.assertIsInstance(result, AgentResult)
        self.assertEqual(result.answer, "runtime ok")
        self.assertEqual(result.steps, 2)
        self.assertEqual([step.tool_name for step in result.run_log.steps], ["list_files", "finish"])


if __name__ == "__main__":
    unittest.main()
