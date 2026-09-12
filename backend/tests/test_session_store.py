import json
import shutil
import unittest
from pathlib import Path
from uuid import uuid4

from backend.session_store import SessionStore


class SessionStoreListTests(unittest.TestCase):
    def setUp(self):
        temp_root = Path.cwd() / "tmp" / "unittest"
        temp_root.mkdir(parents=True, exist_ok=True)
        self.temp_path = temp_root / f"{self._testMethodName}-{uuid4().hex}"
        self.temp_path.mkdir(parents=True, exist_ok=False)
        self.store = SessionStore(self.temp_path)

    def tearDown(self):
        shutil.rmtree(self.temp_path, ignore_errors=True)

    def test_list_sessions_hides_empty_default_placeholder_sessions(self):
        self.store.create_or_get_session("empty-placeholder")
        self.store.create_or_get_session("named-empty")
        self.store.rename_session("named-empty", "Named Draft")
        self.store.append_message("real-session", "user", "真实任务")

        listed_ids = {session["session_id"] for session in self.store.list_sessions()}

        self.assertNotIn("empty-placeholder", listed_ids)
        self.assertIn("named-empty", listed_ids)
        self.assertIn("real-session", listed_ids)


class TaskPlanStatusProtectionTests(unittest.TestCase):
    def setUp(self):
        temp_root = Path.cwd() / "tmp" / "unittest"
        temp_root.mkdir(parents=True, exist_ok=True)
        self.temp_path = temp_root / f"{self._testMethodName}-{uuid4().hex}"
        self.temp_path.mkdir(parents=True, exist_ok=False)
        self.store = SessionStore(self.temp_path)

    def tearDown(self):
        shutil.rmtree(self.temp_path, ignore_errors=True)

    def _seed_plan(self):
        self.store.set_task_plan("session-1", [
            {"task_id": "done", "content": "finished work", "status": "completed", "details": "saved"},
            {"task_id": "next", "content": "pending work", "status": "pending"},
        ])

    def test_snapshot_cannot_downgrade_completed_task(self):
        self._seed_plan()

        self.store.set_task_plan("session-1", [
            {"task_id": "done", "content": "finished work", "status": "pending"},
            {"task_id": "next", "content": "pending work", "status": "pending"},
        ])

        plan = self.store.load_task_plan("session-1")
        todos = {item["task_id"]: item for item in plan["todos"]}
        self.assertEqual(todos["done"]["status"], "completed")
        self.assertTrue(any("downgrade" in warning.lower() for warning in plan["warnings"]))

    def test_snapshot_updates_details_and_result_ref_of_completed_task(self):
        self._seed_plan()

        self.store.set_task_plan("session-1", [
            {"task_id": "done", "content": "finished work", "status": "completed", "details": "extended", "result_ref": "out/report.md"},
            {"task_id": "next", "content": "pending work", "status": "pending"},
        ])

        plan = self.store.load_task_plan("session-1")
        todos = {item["task_id"]: item for item in plan["todos"]}
        self.assertEqual(todos["done"]["status"], "completed")
        self.assertEqual(todos["done"]["details"], "extended")
        self.assertEqual(todos["done"]["result_ref"], "out/report.md")
        self.assertEqual(
            [warning for warning in plan["warnings"] if "downgrade" in warning.lower()],
            [],
        )

    def test_single_item_update_cannot_downgrade_completed_task(self):
        self._seed_plan()

        self.store.update_task_plan_todo("session-1", "done", status="pending", details="trying again")

        todos = {item["task_id"]: item for item in self.store.load_task_plan("session-1")["todos"]}
        self.assertEqual(todos["done"]["status"], "completed")
        self.assertEqual(todos["done"]["details"], "saved")

        journal_path = self.store.task_journal_path("session-1")
        self.assertTrue(journal_path.exists())
        events = [json.loads(line) for line in journal_path.read_text(encoding="utf-8").splitlines() if line.strip()]
        self.assertTrue(any(event.get("event") == "task_plan_todo_update_refused" for event in events))
