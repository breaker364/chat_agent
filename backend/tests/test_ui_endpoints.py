import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from fastapi.testclient import TestClient

from backend import main


class FeedbackEndpointTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(main.app)

    def test_feedback_rejects_invalid_rating(self):
        resp = self.client.post(
            "/feedback",
            json={"rating": "meh", "session_id": "s1", "message_index": 0},
        )
        self.assertEqual(resp.status_code, 400)

    def test_feedback_appends_entry_to_jsonl(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "feedback.jsonl"
            with mock.patch.object(main, "_feedback_store_path", return_value=target):
                resp = self.client.post(
                    "/feedback",
                    json={
                        "rating": "up",
                        "session_id": "s1",
                        "message_index": 2,
                        "content_snippet": "回答" * 300,
                    },
                )
            self.assertEqual(resp.status_code, 200)
            self.assertTrue(resp.json().get("ok"))
            lines = target.read_text(encoding="utf-8").strip().splitlines()
            self.assertEqual(len(lines), 1)
            entry = json.loads(lines[-1])
            self.assertEqual(entry["rating"], "up")
            self.assertEqual(entry["session_id"], "s1")
            self.assertEqual(entry["message_index"], 2)
            self.assertLessEqual(len(entry["content_snippet"]), 200)
            self.assertIn("created_at", entry)


class UiConfigEndpointTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(main.app)

    def test_ui_config_returns_welcome_suggestions_from_runtime(self):
        with mock.patch.object(main, "get_runtime_value", return_value=["建议一", "建议二"]):
            resp = self.client.get("/ui-config")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json(), {"welcome_suggestions": ["建议一", "建议二"]})

    def test_ui_config_empty_without_configuration(self):
        with mock.patch.object(main, "get_runtime_value", return_value=None):
            resp = self.client.get("/ui-config")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json(), {})

    def test_ui_config_ignores_non_string_entries(self):
        with mock.patch.object(main, "get_runtime_value", return_value=["有效", 123, "   ", "可用"]):
            resp = self.client.get("/ui-config")
        self.assertEqual(resp.json(), {"welcome_suggestions": ["有效", "123", "可用"]})


if __name__ == "__main__":
    unittest.main()
