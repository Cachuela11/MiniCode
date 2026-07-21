import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from minicode.permissions import CommandPolicy, Decision
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


class ToolSecurityReviewerTests(unittest.TestCase):
    def test_blocks_path_escape(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            reviewer = ToolSecurityReviewer(Path(temp_dir))

            result = reviewer.review("read_file", {"path": "../secret.txt"})

        self.assertEqual(result.decision, Decision.DENY)
        self.assertTrue(result.invalid)

    def test_blocks_secret_file_read(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            reviewer = ToolSecurityReviewer(Path(temp_dir))

            result = reviewer.review("read_file", {"path": ".env"})

        self.assertEqual(result.decision, Decision.DENY)
        self.assertTrue(result.dangerous)

    def test_blocks_protected_git_write(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            reviewer = ToolSecurityReviewer(Path(temp_dir))

            result = reviewer.review("write_file", {"path": ".git/config", "content": "x"})

        self.assertEqual(result.decision, Decision.DENY)
        self.assertTrue(result.dangerous)

    def test_rejects_invalid_write_content(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            reviewer = ToolSecurityReviewer(Path(temp_dir))

            result = reviewer.review("write_file", {"path": "hello.txt", "content": 123})

        self.assertEqual(result.decision, Decision.DENY)
        self.assertTrue(result.invalid)

    def test_blocks_secret_file_edit(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            reviewer = ToolSecurityReviewer(Path(temp_dir))

            result = reviewer.review("edit_file", {"path": ".env", "old_text": "A", "new_text": "B"})

        self.assertEqual(result.decision, Decision.DENY)
        self.assertTrue(result.dangerous)

    def test_blocks_local_web_fetch(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            reviewer = ToolSecurityReviewer(Path(temp_dir))

            result = reviewer.review("web_fetch", {"url": "http://127.0.0.1:8000"})

        self.assertEqual(result.decision, Decision.DENY)
        self.assertTrue(result.dangerous)


class CommandPolicyTests(unittest.TestCase):
    def test_checks_each_shell_segment(self):
        result = CommandPolicy().check("printf ok && rm -rf .")

        self.assertEqual(result.decision, Decision.DENY)
        self.assertTrue(result.dangerous)

    def test_permission_changes_need_approval(self):
        result = CommandPolicy().check("chmod -R 777 .")

        self.assertEqual(result.decision, Decision.ASK)


class ToolRegistrySecurityTests(unittest.TestCase):
    def test_execute_blocks_before_handler_runs(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = Path(temp_dir)
            registry = ToolRegistry(workspace=workspace, sandbox=FakeSandbox(workspace))

            result = registry.execute("read_file", {"path": ".env"})

        self.assertFalse(result.ok)
        self.assertEqual(result.permission_decision, "deny")
        self.assertTrue(result.dangerous_command)

    def test_execute_records_security_allow_for_structured_tool(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = Path(temp_dir)
            (workspace / "README.md").write_text("# ok\n", encoding="utf-8")
            registry = ToolRegistry(workspace=workspace, sandbox=FakeSandbox(workspace))

            result = registry.execute("read_file", {"path": "README.md"})

        self.assertTrue(result.ok)
        self.assertEqual(result.permission_decision, "allow")

    def test_edit_file_replaces_exact_text(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = Path(temp_dir)
            path = workspace / "demo.py"
            path.write_text("value = 1\n", encoding="utf-8")
            registry = ToolRegistry(workspace=workspace, sandbox=FakeSandbox(workspace))

            result = registry.execute("edit_file", {"path": "demo.py", "old_text": "1", "new_text": "2"})
            content = path.read_text(encoding="utf-8")

        self.assertTrue(result.ok)
        self.assertEqual(content, "value = 2\n")

    def test_glob_files_matches_pattern(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = Path(temp_dir)
            (workspace / "pkg").mkdir()
            (workspace / "pkg" / "a.py").write_text("", encoding="utf-8")
            (workspace / "pkg" / "b.txt").write_text("", encoding="utf-8")
            registry = ToolRegistry(workspace=workspace, sandbox=FakeSandbox(workspace))

            result = registry.execute("glob_files", {"pattern": "**/*.py", "path": ".", "limit": 10})

        self.assertTrue(result.ok)
        self.assertIn("pkg/a.py", result.output)
        self.assertNotIn("pkg/b.txt", result.output)

    def test_todo_write_validates_status(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = Path(temp_dir)
            registry = ToolRegistry(workspace=workspace, sandbox=FakeSandbox(workspace))

            result = registry.execute(
                "todo_write",
                {"todos": [{"id": "one", "content": "Inspect code", "status": "in_progress"}]},
            )

        self.assertTrue(result.ok)
        self.assertIn("Inspect code", result.output)

    def test_inspect_diagnostics_reports_python_syntax_error(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = Path(temp_dir)
            (workspace / "bad.py").write_text("def broken(:\n", encoding="utf-8")
            registry = ToolRegistry(workspace=workspace, sandbox=FakeSandbox(workspace))

            result = registry.execute("inspect_diagnostics", {"path": ".", "limit": 10})

        self.assertTrue(result.ok)
        self.assertEqual(result.exit_code, 1)
        self.assertIn("bad.py", result.output)


if __name__ == "__main__":
    unittest.main()
