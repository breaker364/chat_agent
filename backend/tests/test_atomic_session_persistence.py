import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from backend.session_store import SessionPersistenceError, SessionStore


class AtomicSessionPersistenceTests(unittest.TestCase):
    def test_failed_replacement_keeps_last_good_session_and_reports_candidate(self):
        with tempfile.TemporaryDirectory() as directory:
            store = SessionStore(Path(directory))
            session = store.create_or_get_session("atomic", first_message="first")
            path = store.session_path("atomic")
            before = json.loads(path.read_text(encoding="utf-8"))
            session["title"] = "updated"

            with patch.object(Path, "replace", side_effect=PermissionError("locked")):
                with self.assertRaises(SessionPersistenceError) as captured:
                    store.save_session(session)

            self.assertEqual(json.loads(path.read_text(encoding="utf-8")), before)
            self.assertTrue(Path(captured.exception.candidate_path).exists())


if __name__ == "__main__":
    unittest.main()
