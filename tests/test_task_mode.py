import json
import unittest

from minicode.llm import LLMResponse
from minicode.observability import TokenUsage
from minicode.policy import PolicyEngine, render_policy_prompt
from minicode.task_mode import TaskModeRouter


class TaskModeLlm:
    def __init__(self, payload):
        self.payload = payload
        self.calls = 0

    def chat_response(self, model, messages):
        self.calls += 1
        return LLMResponse(
            content=json.dumps(self.payload, ensure_ascii=False),
            token_usage=TokenUsage(prompt_tokens=3, completion_tokens=4, total_tokens=7),
            duration_ms=1,
            raw={},
        )


class TaskModeTests(unittest.TestCase):
    def test_auto_mode_uses_llm_decision_for_subagents(self):
        llm = TaskModeLlm(
            {
                "mode": "subagents",
                "reason": "needs parallel inspection",
                "confidence": 0.9,
                "tasks": [
                    {
                        "name": "inspect_tests",
                        "task": "Inspect tests for failure clues.",
                        "allowed_tools": ["read_file", "grep_files"],
                        "path_scope": ["tests/"],
                        "max_steps": 3,
                    }
                ],
            }
        )

        decision = TaskModeRouter(llm=llm, model="fake", mode="auto").decide("review this failure")
        policy = PolicyEngine().decide("review this failure", task_mode=decision)
        prompt = render_policy_prompt(policy)

        self.assertTrue(decision.use_subagents)
        self.assertEqual(decision.source, "llm")
        self.assertEqual(llm.calls, 1)
        self.assertIsNotNone(policy.required_first_action)
        self.assertEqual(policy.required_first_action.action, "run_subagents")
        self.assertIn('"action":"run_subagents"', prompt)

    def test_on_mode_forces_subagents_without_llm(self):
        llm = TaskModeLlm({"mode": "default", "tasks": []})

        decision = TaskModeRouter(llm=llm, model="fake", mode="on").decide("简单任务也强制")

        self.assertTrue(decision.use_subagents)
        self.assertEqual(decision.source, "manual")
        self.assertEqual(llm.calls, 0)

    def test_off_mode_keeps_default_without_llm(self):
        llm = TaskModeLlm({"mode": "subagents"})

        decision = TaskModeRouter(llm=llm, model="fake", mode="off").decide("复杂任务")

        self.assertFalse(decision.use_subagents)
        self.assertEqual(decision.mode, "default")
        self.assertEqual(llm.calls, 0)


if __name__ == "__main__":
    unittest.main()
