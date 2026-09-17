import asyncio
import json
import unittest
from pathlib import Path
from unittest.mock import patch

from backend.mutation_guard import (
    expected_content_from_manifest,
    extract_current_content,
    extract_state_version,
)
from backend.mutation_manifest import normalize_command_manifest
from backend.runtime_context import bind_runtime_context, reset_runtime_context
from backend.skills import SkillDefinition, clear_run_mutation_ledger, execute_skill


REQUEST = "lark document replace doc-token --text revised body"


class _FakeLLMResponse:
    def __init__(self, content: str):
        self.content = content


class _FakeLLM:
    def __init__(self, content: str):
        self._content = content

    async def ainvoke(self, _messages):
        return _FakeLLMResponse(self._content)


class MutationStateHelperTests(unittest.TestCase):
    def test_expected_content_from_content_flags(self):
        manifest = normalize_command_manifest(REQUEST)
        self.assertEqual(expected_content_from_manifest(manifest), "revised body")

    def test_expected_content_empty_without_content_flag(self):
        manifest = normalize_command_manifest("lark document delete doc-token")
        self.assertEqual(expected_content_from_manifest(manifest), "")

    def test_extract_current_content_and_version(self):
        self.assertEqual(extract_current_content({"content": "abc"}), "abc")
        self.assertEqual(extract_current_content({"text": "abc"}), "abc")
        self.assertEqual(extract_current_content({"note": "x"}), "")
        self.assertEqual(extract_state_version({"version": "v2"}), "v2")
        self.assertEqual(extract_state_version({"revision_id": "r9"}), "r9")
        self.assertEqual(extract_state_version({"message": "ok"}), "")


class MutationVerificationTests(unittest.TestCase):
    def setUp(self):
        self.skill = SkillDefinition(
            name="remote-command-runner",
            description="",
            prompt_template="",
            source_path=".",
        )
        self._token = bind_runtime_context("mv-session", "mv-run")

    def tearDown(self):
        clear_run_mutation_ledger("mv-run")
        reset_runtime_context(self._token)

    def _run(self, routes, calls):
        def runner(_skill, params, _root):
            request = str(params.get("request") or "")
            calls.append(request)
            for prefix, handler in routes.items():
                if request.startswith(prefix):
                    if callable(handler):
                        outcome = handler(params)
                    else:
                        outcome = handler
                    if isinstance(outcome, Exception):
                        raise outcome
                    return outcome
            raise AssertionError(f"unexpected request: {request}")

        return patch("backend.skills.get_installed_skill", return_value=self.skill), patch(
            "backend.skills._skill_runner_path", return_value=Path("runner.py")
        ), patch("backend.skills._execute_skill_runner_sync", side_effect=runner)

    def test_post_check_version_match_records_verified_success(self):
        calls = []
        patches = self._run(
            {
                "lark document replace": '{"after_version":"v2","message":"ok"}',
                "lark document read": '{"version":"v2"}',
            },
            calls,
        )
        with patches[0], patches[1], patches[2]:
            result = json.loads(asyncio.run(execute_skill(Path.cwd(), self.skill.name, {"request": REQUEST})))

        self.assertTrue(result["verified"])
        self.assertEqual(result["verification"], "verified")
        self.assertTrue(result["action_id"].startswith("sha256:"))
        self.assertEqual(len(calls), 3)  # pre-check read + dispatch + post-check read

    def test_post_check_version_mismatch_returns_verification_failed_and_skips_ledger(self):
        from backend.skills import _RUN_MUTATION_LEDGERS

        calls = []
        patches = self._run(
            {
                "lark document replace": '{"after_version":"v2","message":"ok"}',
                "lark document read": '{"version":"v1"}',
            },
            calls,
        )
        with patches[0], patches[1], patches[2]:
            result = json.loads(asyncio.run(execute_skill(Path.cwd(), self.skill.name, {"request": REQUEST})))

        self.assertEqual(result["status"], "verification_failed")
        self.assertFalse(result["retryable"])
        ledger = _RUN_MUTATION_LEDGERS.get("mv-run")
        self.assertTrue(ledger is None or ledger.succeeded_result(result["action_id"]) is None)
        self.assertEqual(len(calls), 3)  # pre-check read + dispatch + post-check read

    def test_post_check_content_match_verifies_without_versions(self):
        calls = []
        patches = self._run(
            {
                "lark document replace": '{"message":"ok"}',
                "lark document read": lambda _params: (
                    '{"note":"stale"}'
                    if len([c for c in calls if c.startswith("lark document read")]) == 1
                    else '{"content":"revised body"}'
                ),
            },
            calls,
        )
        with patches[0], patches[1], patches[2]:
            result = json.loads(asyncio.run(execute_skill(Path.cwd(), self.skill.name, {"request": REQUEST})))

        self.assertTrue(result["verified"])
        self.assertEqual(result["verification"], "verified")

    def test_post_check_without_comparable_state_records_unverified(self):
        from backend.skills import _RUN_MUTATION_LEDGERS

        calls = []
        patches = self._run(
            {
                "lark document replace": '{"message":"ok"}',
                "lark document read": '{"note":"no state exposed"}',
            },
            calls,
        )
        with patches[0], patches[1], patches[2]:
            result = json.loads(asyncio.run(execute_skill(Path.cwd(), self.skill.name, {"request": REQUEST})))

        self.assertFalse(result["verified"])
        self.assertEqual(result["verification"], "unverified")
        ledger = _RUN_MUTATION_LEDGERS.get("mv-run")
        self.assertIsNotNone(ledger)
        record = ledger.succeeded_result(result["action_id"])
        self.assertIsNotNone(record)
        self.assertFalse(record.verified)

    def test_read_back_failure_marks_verification_failed(self):
        calls = []
        patches = self._run(
            {
                "lark document replace": '{"after_version":"v2"}',
                "lark document read": RuntimeError("remote read exploded"),
            },
            calls,
        )
        with patches[0], patches[1], patches[2]:
            result = json.loads(asyncio.run(execute_skill(Path.cwd(), self.skill.name, {"request": REQUEST})))

        self.assertEqual(result["status"], "verification_failed")
        self.assertEqual(len(calls), 3)  # pre-check read + dispatch + post-check read

    def test_no_read_back_channel_records_unverified(self):
        from backend.skills import _RUN_MUTATION_LEDGERS

        calls = []
        patches = self._run({"lark document replace": '{"message":"ok"}'}, calls)
        with patches[0], patch("backend.skills._skill_runner_path", return_value=None), patch(
            "backend.config.load_llm_config", return_value={}
        ), patch("backend.config.create_chat_deepseek", return_value=_FakeLLM('{"message":"ok"}')):
            result = json.loads(asyncio.run(execute_skill(Path.cwd(), self.skill.name, {"request": REQUEST})))

        self.assertFalse(result["verified"])
        self.assertEqual(result["verification"], "unverified")
        ledger = _RUN_MUTATION_LEDGERS.get("mv-run")
        self.assertIsNotNone(ledger)
        self.assertIsNotNone(ledger.succeeded_result(result["action_id"]))


class MutationPrecheckTests(unittest.TestCase):
    def setUp(self):
        self.skill = SkillDefinition(
            name="remote-command-runner",
            description="",
            prompt_template="",
            source_path=".",
        )
        self._token = bind_runtime_context("mv-session", "mv-precheck-run")

    def tearDown(self):
        clear_run_mutation_ledger("mv-precheck-run")
        reset_runtime_context(self._token)

    def _run(self, routes, calls):
        def runner(_skill, params, _root):
            request = str(params.get("request") or "")
            calls.append(request)
            for prefix, handler in routes.items():
                if request.startswith(prefix):
                    if callable(handler):
                        outcome = handler(params)
                    else:
                        outcome = handler
                    if isinstance(outcome, Exception):
                        raise outcome
                    return outcome
            raise AssertionError(f"unexpected request: {request}")

        return patch("backend.skills.get_installed_skill", return_value=self.skill), patch(
            "backend.skills._skill_runner_path", return_value=Path("runner.py")
        ), patch("backend.skills._execute_skill_runner_sync", side_effect=runner)

    def test_exact_content_match_skips_dispatch_and_records_already_satisfied(self):
        from backend.skills import _RUN_MUTATION_LEDGERS

        calls = []
        patches = self._run(
            {
                "lark document replace": '{"message":"should not dispatch"}',
                "lark document read": '{"content":"revised body","version":"v5"}',
            },
            calls,
        )
        with patches[0], patches[1], patches[2]:
            result = json.loads(asyncio.run(execute_skill(Path.cwd(), self.skill.name, {"request": REQUEST})))

        self.assertEqual(result["status"], "already_satisfied")
        self.assertTrue(result["action_id"].startswith("sha256:"))
        self.assertEqual(len(calls), 1)
        ledger = _RUN_MUTATION_LEDGERS.get("mv-precheck-run")
        self.assertIsNotNone(ledger)
        record = ledger.succeeded_result(result["action_id"])
        self.assertIsNotNone(record)
        self.assertTrue(record.verified)

    def test_content_mismatch_dispatches_normally(self):
        calls = []
        patches = self._run(
            {
                "lark document replace": '{"after_version":"v2"}',
                "lark document read": None,  # resolved below per call count
            },
            calls,
        )

        def runner_side_effect(_skill, params, _root):
            request = str(params.get("request") or "")
            calls.append(request)
            if request.startswith("lark document replace"):
                return '{"after_version":"v2"}'
            if request.startswith("lark document read"):
                if len([c for c in calls if c.startswith("lark document read")]) == 1:
                    return '{"content":"stale"}'
                return '{"version":"v2"}'
            raise AssertionError(request)

        patches = (
            patch("backend.skills.get_installed_skill", return_value=self.skill),
            patch("backend.skills._skill_runner_path", return_value=Path("runner.py")),
            patch("backend.skills._execute_skill_runner_sync", side_effect=runner_side_effect),
        )
        with patches[0], patches[1], patches[2]:
            result = json.loads(asyncio.run(execute_skill(Path.cwd(), self.skill.name, {"request": REQUEST})))

        self.assertNotEqual(result.get("status"), "already_satisfied")
        self.assertTrue(result["verified"])
        dispatch_calls = [c for c in calls if c.startswith("lark document replace")]
        self.assertEqual(len(dispatch_calls), 1)

    def test_precheck_read_failure_does_not_block_dispatch(self):
        calls = []

        def runner_side_effect(_skill, params, _root):
            request = str(params.get("request") or "")
            calls.append(request)
            if request.startswith("lark document replace"):
                return '{"after_version":"v2"}'
            if request.startswith("lark document read"):
                reads = [c for c in calls if c.startswith("lark document read")]
                if len(reads) == 1:
                    raise RuntimeError("transient read failure")
                return '{"version":"v2"}'
            raise AssertionError(request)

        patches = (
            patch("backend.skills.get_installed_skill", return_value=self.skill),
            patch("backend.skills._skill_runner_path", return_value=Path("runner.py")),
            patch("backend.skills._execute_skill_runner_sync", side_effect=runner_side_effect),
        )
        with patches[0], patches[1], patches[2]:
            result = json.loads(asyncio.run(execute_skill(Path.cwd(), self.skill.name, {"request": REQUEST})))

        dispatch_calls = [c for c in calls if c.startswith("lark document replace")]
        self.assertEqual(len(dispatch_calls), 1)
        self.assertTrue(result["verified"])


if __name__ == "__main__":
    unittest.main()
