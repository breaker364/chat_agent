import json
import unittest

from backend.agent import (
    render_tool_error_guidance,
    retryable_from_exception,
    retryable_from_tool_output,
    split_tool_errors_by_retryability,
)


class RetryableExtractionTests(unittest.TestCase):
    def test_payload_retryable_field_wins(self):
        payload = json.dumps({"status": "failed", "retryable": False, "error_category": "invalid_arguments"})
        self.assertIs(retryable_from_tool_output(payload), False)

    def test_payload_status_classification_applies(self):
        self.assertIs(retryable_from_tool_output(json.dumps({"status": "timed_out"})), True)
        self.assertIs(retryable_from_tool_output(json.dumps({"status": "permission_denied"})), False)

    def test_non_failure_payload_returns_none(self):
        self.assertIsNone(retryable_from_tool_output(json.dumps({"status": "success"})))
        self.assertIsNone(retryable_from_tool_output("plain output"))
        self.assertIsNone(retryable_from_tool_output(""))

    def test_exception_attribute_wins(self):
        exc = RuntimeError("boom")
        exc.retryable = False
        self.assertIs(retryable_from_exception(exc), False)

    def test_exception_classification_fallback(self):
        self.assertIs(retryable_from_exception(TimeoutError("late")), True)
        self.assertIs(retryable_from_exception(PermissionError("denied")), False)
        self.assertIs(retryable_from_exception(RuntimeError("mystery")), True)


class ToolErrorPartitionTests(unittest.TestCase):
    def test_errors_split_by_retryability(self):
        errors = [
            {"tool": "a", "message": "timeout", "retryable": True},
            {"tool": "b", "message": "bad args", "retryable": False},
            {"tool": "c", "message": "legacy, no flag"},
        ]
        retryable, fatal = split_tool_errors_by_retryability(errors)
        self.assertEqual([e["tool"] for e in retryable], ["a", "c"])
        self.assertEqual([e["tool"] for e in fatal], ["b"])

    def test_guidance_marks_fatal_calls_as_do_not_reissue(self):
        errors = [
            {"tool": "write_file", "message": "permission denied", "retryable": False},
            {"tool": "web_search", "message": "rate limited", "retryable": True},
        ]
        guidance = render_tool_error_guidance(errors)

        self.assertIn("Do NOT re-issue", guidance)
        self.assertIn("`write_file`", guidance)
        self.assertIn("`web_search`", guidance)

    def test_guidance_without_errors_has_placeholder(self):
        guidance = render_tool_error_guidance([])
        self.assertIn("No structured tool error", guidance)

    def test_guidance_keeps_tool_error_header_compatible_content(self):
        guidance = render_tool_error_guidance([{"tool": "bash", "message": "boom", "retryable": True}])
        self.assertIn("`bash`", guidance)
        self.assertIn("boom", guidance)


if __name__ == "__main__":
    unittest.main()
