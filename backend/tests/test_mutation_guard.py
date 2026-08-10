import unittest
import asyncio
from pathlib import Path
from unittest.mock import patch

from backend.mutation_guard import MutationLedger, build_idempotency_key, parse_standardized_remote_command
from backend.mutation_manifest import normalize_command_manifest
from backend.runtime_context import bind_runtime_context, reset_runtime_context
from backend.skills import SkillDefinition, execute_skill


class RemoteMutationCommandTests(unittest.TestCase):
    def test_classifies_standardized_document_operations_and_unknown_commands(self):
        cases = {
            "lark document read doc-token": ("read", False),
            "lark document append doc-token --text section": ("append", True),
            "lark document edit doc-token --block block-token --text revised": ("edit", True),
            "lark document replace doc-token --file replacement.md": ("replace", True),
            "lark document create --title draft": ("create", True),
            "lark document delete doc-token": ("delete", True),
            "lark document publish doc-token": ("unknown", False),
        }

        for request, expected in cases.items():
            with self.subTest(request=request):
                envelope = parse_standardized_remote_command(request)
                self.assertEqual((envelope.operation, envelope.mutating), expected)

    def test_canonical_key_uses_semantic_mutation_identity_not_tool_call_id(self):
        envelope = parse_standardized_remote_command("lark document append doc-token --text section")
        first = build_idempotency_key(
            envelope,
            {"text": "section", "metadata": {"position": 2}, "tool_call_id": "call-a"},
        )
        equivalent = build_idempotency_key(
            envelope,
            {"metadata": {"position": 2}, "tool_call_id": "call-b", "text": "section"},
        )
        changed = build_idempotency_key(envelope, {"text": "different", "metadata": {"position": 2}})

        self.assertEqual(first, equivalent)
        self.assertNotEqual(first, changed)

    def test_ledger_reuses_succeeded_mutation_with_audit_fields(self):
        envelope = parse_standardized_remote_command("lark document edit doc-token --text revised")
        key = build_idempotency_key(envelope, {"text": "revised"})
        ledger = MutationLedger()

        recorded = ledger.record_success(
            key,
            envelope,
            {"message": "updated"},
            before_version="version-1",
            after_version="version-2",
            verified=True,
        )
        reused = ledger.succeeded_result(key)

        self.assertEqual(reused, recorded)
        self.assertEqual(reused.before_version, "version-1")
        self.assertEqual(reused.after_version, "version-2")
        self.assertTrue(reused.verified)
        self.assertTrue(reused.result_hash.startswith("sha256:"))

    def test_skill_boundary_reuses_succeeded_remote_mutation_for_new_tool_call_id(self):
        skill = SkillDefinition(
            name="remote-command-runner",
            description="",
            prompt_template="",
            source_path=".",
        )
        calls = []

        def fake_runner(_skill, params, _root):
            calls.append(dict(params))
            return '{"before_version":"version-1","after_version":"version-2","verified":true}'

        async def invoke_twice():
            with patch("backend.skills.get_installed_skill", return_value=skill), patch(
                "backend.skills._skill_runner_path", return_value=Path("runner.py")
            ), patch("backend.skills._execute_skill_runner_sync", side_effect=fake_runner):
                first = await execute_skill(
                    Path.cwd(),
                    skill.name,
                    {"request": "lark document edit doc-token --text revised", "tool_call_id": "call-a"},
                )
                second = await execute_skill(
                    Path.cwd(),
                    skill.name,
                    {"request": "lark document edit doc-token --text revised", "tool_call_id": "call-b"},
                )
                return first, second

        tokens = bind_runtime_context("mutation-session", "mutation-run")
        try:
            first, second = asyncio.run(invoke_twice())
        finally:
            reset_runtime_context(tokens)

        self.assertEqual(first, second)
        self.assertEqual(len(calls), 1)

    def test_skill_boundary_blocks_append_when_workflow_requires_replacement(self):
        skill = SkillDefinition(
            name="remote-command-runner",
            description="",
            prompt_template="",
            source_path=".",
        )
        calls = []

        def fake_runner(_skill, params, _root):
            calls.append(dict(params))
            return "should not run"

        async def invoke():
            with patch("backend.skills.get_installed_skill", return_value=skill), patch(
                "backend.skills._skill_runner_path", return_value=Path("runner.py")
            ), patch("backend.skills._execute_skill_runner_sync", side_effect=fake_runner):
                await execute_skill(
                    Path.cwd(),
                    skill.name,
                    {"request": "lark document append doc-token --workflow replace --text revised"},
                )

        with self.assertRaisesRegex(ValueError, "append.*replacement"):
            asyncio.run(invoke())
        self.assertEqual(calls, [])

    def test_skill_boundary_rejects_unclassified_standardized_command_before_dispatch(self):
        skill = SkillDefinition(
            name="remote-command-runner",
            description="",
            prompt_template="",
            source_path=".",
        )
        calls = []

        def fake_runner(_skill, params, _root):
            calls.append(dict(params))
            return "should not run"

        async def invoke():
            with patch("backend.skills.get_installed_skill", return_value=skill), patch(
                "backend.skills._skill_runner_path", return_value=Path("runner.py")
            ), patch("backend.skills._execute_skill_runner_sync", side_effect=fake_runner):
                await execute_skill(Path.cwd(), skill.name, {"request": "lark document publish doc-token"})

        with self.assertRaisesRegex(ValueError, "Unclassified"):
            asyncio.run(invoke())
        self.assertEqual(calls, [])

    def test_skill_boundary_passes_normalized_manifest_to_runner(self):
        skill = SkillDefinition(
            name="remote-command-runner",
            description="",
            prompt_template="",
            source_path=".",
        )
        calls = []

        def fake_runner(_skill, params, _root):
            calls.append(dict(params))
            return '{"verified":true}'

        async def invoke():
            with patch("backend.skills.get_installed_skill", return_value=skill), patch(
                "backend.skills._skill_runner_path", return_value=Path("runner.py")
            ), patch("backend.skills._execute_skill_runner_sync", side_effect=fake_runner):
                return await execute_skill(
                    Path.cwd(),
                    skill.name,
                    {"request": "lark doc replace doc-token --md-file body.md"},
                )

        asyncio.run(invoke())
        assert calls[0]["manifest"]["operation"] == "replace"
        assert calls[0]["manifest"]["resource"] == "document"

    def test_skill_boundary_rejects_manifest_that_disagrees_with_request(self):
        skill = SkillDefinition(
            name="remote-command-runner",
            description="",
            prompt_template="",
            source_path=".",
        )
        calls = []

        def fake_runner(_skill, params, _root):
            calls.append(dict(params))
            return "should not run"

        async def invoke():
            with patch("backend.skills.get_installed_skill", return_value=skill), patch(
                "backend.skills._skill_runner_path", return_value=Path("runner.py")
            ), patch("backend.skills._execute_skill_runner_sync", side_effect=fake_runner):
                await execute_skill(
                    Path.cwd(),
                    skill.name,
                    {
                        "request": "lark doc replace doc-token --md-file body.md",
                        "manifest": normalize_command_manifest("lark doc append doc-token --text body").to_dict(),
                    },
                )

        with self.assertRaisesRegex(ValueError, "manifest.*disagrees"):
            asyncio.run(invoke())
        self.assertEqual(calls, [])


if __name__ == "__main__":
    unittest.main()
