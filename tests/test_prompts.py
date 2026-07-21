import unittest

from minicode.prompts import build_task_message, build_turn_message


class PromptBuilderTests(unittest.TestCase):
    def test_plain_task_message_has_no_extra_policy_directives(self):
        message = build_task_message("say hello", "/workspace\nREADME.md")

        self.assertIn("Task:\nsay hello", message)
        self.assertIn("Initial context:\n/workspace\nREADME.md", message)
        self.assertIn("No extra policy directives", message)

    def test_workspace_task_injects_policy_before_initial_context(self):
        message = build_task_message("查看一下当前项目结构", "/workspace")

        self.assertIn("Policy directives for this turn", message)
        self.assertIn('"action":"list_files"', message)
        self.assertIn("Use fresh tool output", message)
        self.assertLess(message.index("Policy directives"), message.index("Initial context:"))

    def test_turn_message_includes_skill_prompt_after_policy(self):
        message = build_turn_message(
            turn=3,
            user_message="inspect workspace structure",
            skill_prompt="No relevant skills.",
        )

        self.assertIn("User turn 3:\ninspect workspace structure", message)
        self.assertIn("Policy directives for this turn", message)
        self.assertIn("Skill route hint for this turn:\nNo relevant skills.", message)
        self.assertLess(message.index("Policy directives"), message.index("Skill route hint"))


if __name__ == "__main__":
    unittest.main()
