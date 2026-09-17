import json
import unittest

from backend.error_taxonomy import (
    classify_exception,
    classify_payload,
    failure_payload_category,
    normalize_failure_result,
)


class ExceptionClassificationTests(unittest.TestCase):
    def test_timeout_and_connection_errors_are_retryable(self):
        self.assertTrue(classify_exception(TimeoutError("late")).retryable)
        self.assertEqual(classify_exception(TimeoutError("late")).category, "timeout")
        self.assertTrue(classify_exception(ConnectionError("reset")).retryable)
        self.assertEqual(classify_exception(ConnectionResetError("reset")).category, "network")

    def test_permission_and_missing_target_are_not_retryable(self):
        permission = classify_exception(PermissionError("denied"))
        self.assertEqual(permission.category, "permission_denied")
        self.assertFalse(permission.retryable)

        missing = classify_exception(FileNotFoundError("gone"))
        self.assertEqual(missing.category, "not_found")
        self.assertFalse(missing.retryable)

    def test_unknown_exceptions_default_to_retryable(self):
        classification = classify_exception(RuntimeError("mystery"))
        self.assertEqual(classification.category, "unknown")
        self.assertTrue(classification.retryable)


class PayloadClassificationTests(unittest.TestCase):
    def test_explicit_status_categories(self):
        self.assertEqual(classify_payload({"status": "timed_out"}).category, "timeout")
        self.assertTrue(classify_payload({"status": "timed_out"}).retryable)
        self.assertEqual(classify_payload({"status": "invalid_input"}).category, "invalid_arguments")
        self.assertFalse(classify_payload({"status": "invalid_input"}).retryable)
        self.assertEqual(classify_payload({"status": "budget_denied"}).category, "budget_denied")
        self.assertFalse(classify_payload({"status": "budget_denied"}).retryable)

    def test_duplicate_side_effect_block_is_not_retryable(self):
        classification = classify_payload({"blocked": True, "reason": "duplicate_side_effect_tool_call"})
        self.assertIsNotNone(classification)
        self.assertFalse(classification.retryable)

    def test_non_failure_payload_returns_none(self):
        self.assertIsNone(classify_payload({"status": "success", "result": "ok"}))
        self.assertIsNone(classify_payload({"result": 1}))

    def test_unknown_failure_status_defaults_retryable(self):
        classification = classify_payload({"status": "command_failed", "error": "boom"})
        self.assertIsNotNone(classification)
        self.assertEqual(classification.category, "unknown")
        self.assertTrue(classification.retryable)

    def test_text_failure_classification(self):
        classification = failure_payload_category(json.dumps({"status": "permission_denied"}))
        self.assertIsNotNone(classification)
        self.assertFalse(classification.retryable)
        self.assertIsNone(failure_payload_category(json.dumps({"status": "success"})))
        self.assertIsNone(failure_payload_category("plain text output"))


class NormalizeFailureResultTests(unittest.TestCase):
    def test_failure_result_gains_category_fields(self):
        enriched = json.loads(normalize_failure_result(json.dumps({"status": "policy_denied"})))
        self.assertEqual(enriched["error_category"], "policy_denied")
        self.assertFalse(enriched["retryable"])

    def test_existing_fields_are_preserved(self):
        original = json.dumps({"status": "timed_out", "error_category": "timeout", "retryable": True})
        enriched = json.loads(normalize_failure_result(original))
        self.assertEqual(enriched["error_category"], "timeout")
        self.assertTrue(enriched["retryable"])

    def test_success_and_plain_text_unchanged(self):
        success = json.dumps({"status": "success", "result": "fine"}, ensure_ascii=False)
        self.assertEqual(normalize_failure_result(success), success)
        self.assertEqual(normalize_failure_result("plain text"), "plain text")


if __name__ == "__main__":
    unittest.main()
