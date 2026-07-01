import unittest

from minicode.prompts import (
    build_task_message,
    build_turn_message,
    required_first_action_prompt,
    requires_workspace_inspection,
)


class PromptBuilderTests(unittest.TestCase):
    def test_plain_task_message_has_no_mandatory_first_action(self):
        message = build_task_message("say hello", "/workspace\nREADME.md")

        self.assertIn("Task:\nsay hello", message)
        self.assertIn("Initial context:\n/workspace\nREADME.md", message)
        self.assertNotIn("Mandatory first action", message)

    def test_workspace_task_injects_mandatory_list_files_action(self):
        message = build_task_message("查看一下当前项目结构", "/workspace")

        self.assertIn("Mandatory first action", message)
        self.assertIn('"action":"list_files"', message)
        self.assertIn("even if Initial context already contains a file index", message)
        self.assertLess(message.index("Mandatory first action"), message.index("Initial context:"))

    def test_turn_message_includes_skill_prompt_after_directive(self):
        message = build_turn_message(
            turn=3,
            user_message="inspect workspace structure",
            skill_prompt="No relevant skills.",
        )

        self.assertIn("User turn 3:\ninspect workspace structure", message)
        self.assertIn("Mandatory first action", message)
        self.assertIn("Relevant skills for this turn:\nNo relevant skills.", message)
        self.assertLess(message.index("Mandatory first action"), message.index("Relevant skills"))


class WorkspaceInspectionIntentTests(unittest.TestCase):
    def test_detects_chinese_workspace_inspection(self):
        self.assertTrue(requires_workspace_inspection("查看一下当前项目结构"))

    def test_detects_english_workspace_inspection(self):
        self.assertTrue(requires_workspace_inspection("inspect workspace structure"))

    def test_ignores_unrelated_request(self):
        self.assertFalse(requires_workspace_inspection("write a fibonacci function"))

    def test_required_prompt_empty_when_not_needed(self):
        self.assertEqual(required_first_action_prompt("write a fibonacci function"), "")


if __name__ == "__main__":
    unittest.main()
