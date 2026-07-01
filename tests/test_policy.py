import unittest

from minicode.policy import PolicyEngine, render_policy_prompt, required_first_action_prompt


class PolicyEngineTests(unittest.TestCase):
    def test_workspace_structure_requires_list_files_first(self):
        policy = PolicyEngine().decide("查看一下当前项目结构")

        self.assertEqual(policy.intent, "workspace_structure")
        self.assertIsNotNone(policy.required_first_action)
        self.assertEqual(policy.required_first_action.action, "list_files")
        self.assertTrue(any("Do not invent files" in rule for rule in policy.rules))

    def test_code_change_adds_test_expectation(self):
        policy = PolicyEngine().decide("帮我修复这个类型错误")

        self.assertIn("Keep edits focused on the user request.", policy.rules)
        self.assertTrue(any("run a relevant test" in rule for rule in policy.rules))

    def test_test_request_prefers_run_tests(self):
        policy = PolicyEngine().decide("跑一下单元测试")

        self.assertTrue(any("Prefer run_tests" in rule for rule in policy.rules))

    def test_general_request_has_no_directives(self):
        policy = PolicyEngine().decide("你好")

        self.assertFalse(policy.has_directives)
        self.assertEqual(render_policy_prompt(policy), "No extra policy directives for this turn.")

    def test_compat_required_first_action_prompt(self):
        prompt = required_first_action_prompt("project structure")

        self.assertIn("Required first action", prompt)
        self.assertIn('"action":"list_files"', prompt)


if __name__ == "__main__":
    unittest.main()
