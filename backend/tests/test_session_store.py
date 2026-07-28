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
