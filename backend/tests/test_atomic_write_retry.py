import json
import unittest
from pathlib import Path
from unittest import mock

from backend.session_store import SessionStore, _atomic_write_json


class AtomicWriteRetryTests(unittest.TestCase):
    def setUp(self):
        import tempfile

        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "session.json"
        self.path.write_text("{}", encoding="utf-8")

    def tearDown(self):
        self._tmp.cleanup()

    def test_retries_replace_on_windows_permission_error(self):
        payload = {"session_id": "s1", "messages": []}
        original_replace = Path.replace
        attempts = {"n": 0}

        def flaky_replace(self, target):
            attempts["n"] += 1
            if attempts["n"] < 3:
                raise PermissionError(5, "拒绝访问")
            return original_replace(self, target)

        with mock.patch.object(Path, "replace", flaky_replace):
            _atomic_write_json(self.path, payload)

        self.assertEqual(attempts["n"], 3)
        self.assertEqual(json.loads(self.path.read_text(encoding="utf-8")), payload)

    def test_raises_after_exhausted_retries(self):
        payload = {"session_id": "s2"}

        def always_denied(self, target):
            raise PermissionError(5, "拒绝访问")

        with mock.patch.object(Path, "replace", always_denied):
            with self.assertRaises(Exception):
                _atomic_write_json(self.path, payload)

    def test_success_on_first_attempt_is_unchanged(self):
        payload = {"ok": True}
        _atomic_write_json(self.path, payload)
        self.assertEqual(json.loads(self.path.read_text(encoding="utf-8")), payload)


if __name__ == "__main__":
    unittest.main()
