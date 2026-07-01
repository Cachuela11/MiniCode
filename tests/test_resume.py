import json
import tempfile
import unittest
from pathlib import Path

from minicode.memory import FileMemoryStore, MemoryCandidate
from minicode.resume import (
    build_resume_result,
    delete_session_log,
    find_resume_log,
    list_resume_candidates,
    load_resume_log,
    related_memory_ids_for_run,
    resolve_resume_selection,
)


class ResumeTests(unittest.TestCase):
    def test_find_resume_log_uses_newest_json_in_directory(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            older = root / "older.json"
            newer = root / "newer.json"
            older.write_text(json.dumps({"answer": "old"}), encoding="utf-8")
            newer.write_text(json.dumps({"answer": "new"}), encoding="utf-8")

            found = find_resume_log("", workspace=root, default_target=root)

        self.assertEqual(found.name, "newer.json")

    def test_list_resume_candidates_includes_empty_sessions(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            empty = root / "empty.json"
            ready = root / "ready.json"
            empty.write_text(json.dumps({"task": "Interactive session"}), encoding="utf-8")
            ready.write_text(
                json.dumps({"task": "Interactive session", "session_turns": [{"turn": 1}], "steps": []}),
                encoding="utf-8",
            )

            candidates = list_resume_candidates("", workspace=root, default_target=root)
            selected = resolve_resume_selection(candidates, "2")

        self.assertEqual(len(candidates), 2)
        self.assertTrue(any(not candidate.resumable for candidate in candidates))
        self.assertEqual(selected.index, 2)

    def test_build_resume_result_prefers_session_turns(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "run.json"
            payload = {
                "run_id": "run_1",
                "task": "Interactive session",
                "model": "fake",
                "started_at": "2026-01-01T00:00:00Z",
                "session_turns": [
                    {"turn": 1, "user": "hello", "answer": "hi", "steps": 1},
                ],
                "steps": [
                    {"step": 1, "tool_name": "finish", "exit_code": 0, "stdout": "hi"},
                ],
            }
            path.write_text(json.dumps(payload), encoding="utf-8")

            loaded = load_resume_log(path)
            result = build_resume_result(loaded, path)

        self.assertEqual(result.restored_turns, 1)
        self.assertEqual(result.restored_steps, 1)
        self.assertIn("Recovered conversation turns", result.message_content)
        self.assertIn("user: hello", result.message_content)
        self.assertIn("answer: hi", result.message_content)

    def test_delete_session_archives_related_memory_chain(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            log_path = root / "runs" / "run.json"
            log_path.parent.mkdir()
            log_path.write_text(
                json.dumps({"run_id": "run_abc", "answer": "done", "steps": [{"step": 1}]}),
                encoding="utf-8",
            )
            store = FileMemoryStore(workspace=root, memory_dir=".minicode/memory")
            session_write = store.write_candidate(
                MemoryCandidate(
                    memory_type="session_memory",
                    title="Session memory",
                    body="session body",
                    source_run_id="run_abc",
                    source_trace_ids=["run_abc", "run_abc:step:1"],
                )
            )
            long_term_write = store.write_candidate(
                MemoryCandidate(
                    memory_type="project_memory",
                    title="Project memory",
                    body="project body",
                    parent_memory_ids=[session_write.memory_id],
                )
            )

            related = related_memory_ids_for_run(store, "run_abc")
            result = delete_session_log(log_path, store)

            self.assertIn(session_write.memory_id, related)
            self.assertIn(long_term_write.memory_id, related)
            self.assertFalse(log_path.exists())
            self.assertTrue(result.deleted_log_path.exists())
            self.assertEqual(
                sorted(item.memory_id for item in result.archived_memories),
                sorted([session_write.memory_id, long_term_write.memory_id]),
            )
            self.assertEqual(store.all(), [])


if __name__ == "__main__":
    unittest.main()
