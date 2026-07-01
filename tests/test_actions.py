import unittest

from minicode.actions import extract_finish_answer, parse_action


class ParseActionTests(unittest.TestCase):
    def test_parse_normal_tool_action(self):
        action = parse_action(
            '{"thought":"inspect","action":"list_files","args":{"path":".","max_depth":2}}'
        )

        self.assertEqual(action["action"], "list_files")
        self.assertEqual(action["args"]["path"], ".")

    def test_parse_markdown_fenced_json(self):
        action = parse_action(
            '```json\n{"thought":"done","action":"finish","args":{"answer":"ok"}}\n```'
        )

        self.assertEqual(action["action"], "finish")
        self.assertEqual(action["args"]["answer"], "ok")

    def test_parse_json_with_trailing_text(self):
        action = parse_action(
            '{"thought":"done","action":"finish","args":{"answer":"ok"}}\nextra text'
        )

        self.assertEqual(action["action"], "finish")
        self.assertEqual(action["args"]["answer"], "ok")

    def test_repair_missing_closing_brace(self):
        action = parse_action(
            '{"thought":"done","action":"finish","args":{"answer":"ok"}'
        )

        self.assertEqual(action["action"], "finish")
        self.assertEqual(action["args"]["answer"], "ok")

    def test_bare_answer_becomes_finish(self):
        action = parse_action('{"answer":"plain answer"}')

        self.assertEqual(action["action"], "finish")
        self.assertEqual(action["args"]["answer"], "plain answer")

    def test_extract_answer_from_malformed_finish_json(self):
        action = parse_action(
            '{"thought":"done","action":"finish","args":{"answer":"line 1\\nline 2"'
        )

        self.assertEqual(action["action"], "finish")
        self.assertEqual(action["args"]["answer"], "line 1\nline 2")

    def test_malformed_non_json_falls_back_to_finish(self):
        action = parse_action("not json at all")

        self.assertEqual(action["action"], "finish")
        self.assertEqual(action["args"]["answer"], "not json at all")

    def test_non_object_json_is_rejected(self):
        with self.assertRaises(ValueError):
            parse_action('["not", "an", "object"]')


class ExtractFinishAnswerTests(unittest.TestCase):
    def test_extracts_args_answer_first(self):
        answer = extract_finish_answer(
            {"answer": "top level"},
            {"answer": "args level"},
        )

        self.assertEqual(answer, "args level")

    def test_uses_clear_empty_answer_message(self):
        answer = extract_finish_answer({"action": "finish"}, {})

        self.assertEqual(answer, "The model finished without providing an answer.")


if __name__ == "__main__":
    unittest.main()
