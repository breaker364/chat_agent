from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


class FeishuSkillRunnerEncodingTests(unittest.TestCase):
    def test_skill_runner_reconfigures_legacy_stdio_to_utf8(self):
        import importlib.util

        runner_path = Path(__file__).resolve().parents[2] / "skills" / "feishu-personal" / "skill_runner.py"
        spec = importlib.util.spec_from_file_location("feishu_skill_runner_for_test", runner_path)
        self.assertIsNotNone(spec)
        self.assertIsNotNone(spec.loader)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        class LegacyStream:
            def __init__(self):
                self.calls = []

            def reconfigure(self, **kwargs):
                self.calls.append(kwargs)

        stdout = LegacyStream()
        stderr = LegacyStream()
        with patch.object(module.sys, "stdout", stdout), patch.object(module.sys, "stderr", stderr):
            module._configure_stdio_utf8()

        self.assertEqual(stdout.calls, [{"encoding": "utf-8", "errors": "replace"}])
        self.assertEqual(stderr.calls, [{"encoding": "utf-8", "errors": "replace"}])

    def test_skill_runner_subprocess_forces_utf8_stdio_and_skill_runtime_paths(self):
        from backend.skills import SkillDefinition, _execute_skill_runner_sync

        with tempfile.TemporaryDirectory() as raw_root:
            root = Path(raw_root)
            runner = root / "skill_runner.py"
            runner.write_text("", encoding="utf-8")
            skill = SkillDefinition(
                name="remote-skill",
                description="",
                prompt_template="",
                source_path=str(root),
                manifest_config={
                    "runtime_environment": {
                        "REMOTE_SESSION_FILE": "{workspace_root}/sessionss/session.json",
                        "REMOTE_TENANT_CONFIG": "{skill_root}/tenant_config.json",
                    }
                },
            )
            completed = SimpleNamespace(
                returncode=0,
                stdout='{"result":"写入成功"}'.encode("utf-8"),
                stderr=b"",
            )
            with patch("backend.skills.subprocess.run", return_value=completed) as run:
                result = _execute_skill_runner_sync(
                    skill,
                    {"request": "remote read"},
                    root,
                )

            env = run.call_args.kwargs["env"]
            self.assertEqual(result, "写入成功")
            self.assertEqual(env["PYTHONIOENCODING"], "utf-8")
            self.assertEqual(env["PYTHONUTF8"], "1")
            self.assertEqual(
                Path(env["REMOTE_SESSION_FILE"]),
                root / "sessionss" / "session.json",
            )
            self.assertEqual(Path(env["REMOTE_TENANT_CONFIG"]), root / "tenant_config.json")

    def test_skill_runner_classifies_read_runner_failure_as_verification_failure(self):
        from backend.skills import SkillDefinition, _execute_skill_runner_sync

        with tempfile.TemporaryDirectory() as raw_root:
            root = Path(raw_root)
            runner = root / "skill_runner.py"
            runner.write_text("", encoding="utf-8")
            skill = SkillDefinition(
                name="remote-skill",
                description="",
                prompt_template="",
                source_path=str(root),
            )
            completed = SimpleNamespace(
                returncode=1,
                stdout=b"",
                stderr="UnicodeEncodeError: codec cannot encode character".encode("utf-8"),
            )
            with patch("backend.skills.subprocess.run", return_value=completed):
                result = _execute_skill_runner_sync(
                    skill,
                    {
                        "request": "lark document read doc-token",
                        "manifest": {"operation": "read"},
                    },
                    root,
                )

        payload = json.loads(result)
        self.assertEqual(payload["status"], "verification_failed")
        self.assertEqual(payload["error_category"], "readback_failed")
        self.assertIn("UnicodeEncodeError", payload["error"])


class ManagedSkillRoutingGuardTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.workspace = Path(self.temp_dir.name)
        from backend import tools as runtime_tools

        runtime_tools.set_allowed_root(self.workspace, external_read_roots=[])
        skill_dir = self.workspace / "skills" / "managed-remote"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(
            "---\nname: managed-remote\n---\nManaged remote operations.\n",
            encoding="utf-8",
        )
        (skill_dir / "command_manifest.json").write_text(
            json.dumps(
                {
                    "execution_policy": {
                        "required_tool": "use_skill",
                        "skill_name": "managed-remote",
                        "script_markers": [
                            "lark_tools",
                            "lark ",
                            "feishu.cn",
                            "feishu_web_session.json",
                        ],
                    }
                }
            ),
            encoding="utf-8",
        )

    def tearDown(self):
        from backend import tools as runtime_tools

        runtime_tools.set_allowed_root(Path.cwd(), external_read_roots=[])
        self.temp_dir.cleanup()

    def test_python_runner_blocks_read_only_lark_sdk_imports_before_execution(self):
        from backend.tools import run_python_file

        script = self.workspace / "remote_wrapper.py"
        script.write_text("from lark_tools import cli\ncli.main()\n", encoding="utf-8")

        result = json.loads(run_python_file.invoke({"path": "remote_wrapper.py"}))

        self.assertTrue(result["blocked"])
        self.assertEqual(result["status"], "policy_denied")
        self.assertEqual(result["error_category"], "managed_skill_required")
        self.assertEqual(result["suggested_tool"], "use_skill")
        self.assertFalse("executed" in result)

    def test_python_runner_blocks_direct_provider_http_even_without_write_intent(self):
        from backend.tools import run_python_file

        script = self.workspace / "remote_http.py"
        script.write_text(
            "import requests\nrequests.get('https://tenant.feishu.cn/')\n",
            encoding="utf-8",
        )

        result = json.loads(run_python_file.invoke({"path": "remote_http.py"}))

        self.assertEqual(result["status"], "policy_denied")
        self.assertEqual(result["error_category"], "managed_skill_required")
        self.assertEqual(result["suggested_tool"], "use_skill")

    def test_file_tools_block_creation_of_managed_remote_wrapper_scripts(self):
        from backend.tools import append_file, write_file

        content = "from lark_tools import cli\n"
        written = json.loads(write_file.invoke({"path": "wrapper.py", "content": content}))
        appended = json.loads(
            append_file.invoke({"path": "append_wrapper.py", "content": content})
        )

        self.assertEqual(written["status"], "policy_denied")
        self.assertEqual(appended["status"], "policy_denied")
        self.assertFalse((self.workspace / "wrapper.py").exists())
        self.assertFalse((self.workspace / "append_wrapper.py").exists())

    def test_bash_blocks_managed_remote_commands_before_process_start(self):
        from backend.workspace_shell_tools import bash

        settings = {
            "enabled": True,
            "bash_enabled": True,
            "bash_executable": os.sys.executable,
            "environment_allowlist": [],
            "default_timeout_seconds": 30,
            "max_timeout_seconds": 30,
            "max_output_chars": 12000,
            "max_glob_matches": 200,
            "max_grep_matches": 200,
            "max_file_bytes": 1024 * 1024,
            "max_pattern_chars": 4096,
            "max_scope_chars": 2048,
            "max_command_chars": 8192,
        }
        with patch("backend.workspace_shell_tools._config", return_value=settings), patch(
            "backend.workspace_shell_tools._run_bounded_process",
            side_effect=AssertionError("managed remote command must not start"),
        ):
            result = json.loads(
                bash.invoke({"command": "python -c 'import lark_tools.cli'"})
            )

        self.assertEqual(result["status"], "policy_denied")
        self.assertEqual(result["error_category"], "managed_skill_required")
        self.assertEqual(result["suggested_tool"], "use_skill")

    def test_bash_blocks_referenced_managed_wrapper_script_before_process_start(self):
        from backend.workspace_shell_tools import bash

        (self.workspace / "remote_wrapper.py").write_text(
            "from lark_tools import cli\ncli.main()\n",
            encoding="utf-8",
        )
        settings = {
            "enabled": True,
            "bash_enabled": True,
            "bash_executable": os.sys.executable,
            "environment_allowlist": [],
            "default_timeout_seconds": 30,
            "max_timeout_seconds": 30,
            "max_output_chars": 12000,
            "max_glob_matches": 200,
            "max_grep_matches": 200,
            "max_file_bytes": 1024 * 1024,
            "max_pattern_chars": 4096,
            "max_scope_chars": 2048,
            "max_command_chars": 8192,
        }
        with patch("backend.workspace_shell_tools._config", return_value=settings), patch(
            "backend.workspace_shell_tools._run_bounded_process",
            side_effect=AssertionError("referenced managed script must not start"),
        ):
            result = json.loads(bash.invoke({"command": "python remote_wrapper.py"}))

        self.assertEqual(result["status"], "policy_denied")
        self.assertEqual(result["error_category"], "managed_skill_required")


class SkillResultProgressTests(unittest.TestCase):
    def test_skill_failure_statuses_are_not_recorded_as_mutation_success(self):
        from backend.skills import _skill_result_indicates_failure

        for status in (
            "policy_denied",
            "budget_denied",
            "runtime_unavailable",
            "timed_out",
            "output_truncated",
            "command_failed",
            "invalid_input",
            "verification_failed",
            "write_failed",
            "skill_execution_failed",
        ):
            with self.subTest(status=status):
                self.assertTrue(
                    _skill_result_indicates_failure(
                        json.dumps({"success": True, "status": status})
                    )
                )

    def test_readback_failure_is_not_promoted_to_primary_result(self):
        from backend.tools import _first_result_object

        self.assertIsNone(
            _first_result_object(
                json.dumps(
                    {
                        "success": True,
                        "status": "verification_failed",
                        "url": "https://tenant.example.invalid/doc/token",
                    }
                )
            )
        )

    def test_successful_mutating_skill_result_registers_primary_and_completes_active_task(self):
        from backend import tools as runtime_tools
        from backend.runtime_context import bind_runtime_context, reset_runtime_context
        from backend.session_store import SessionStore
        from backend.skills import SkillDefinition

        with tempfile.TemporaryDirectory() as raw_root:
            root = Path(raw_root)
            runtime_tools.set_allowed_root(root, external_read_roots=[])
            store = SessionStore(root)
            session_id = "skill-progress"
            store.set_task_plan(
                session_id,
                [
                    {
                        "task_id": "create_document",
                        "content": "Create the requested document",
                        "activeForm": "Creating the requested document",
                        "status": "in_progress",
                    }
                ],
            )
            skill = SkillDefinition(
                name="remote-skill",
                description="",
                prompt_template="",
                source_path=str(root),
                manifest_config={"subcommand_operations": {"lark document": {"create": "create"}}},
            )
            output = json.dumps(
                {
                    "success": True,
                    "token": "doc-token",
                    "url": "https://tenant.feishu.cn/docx/doc-token",
                    "blocks_added": 36,
                },
                ensure_ascii=False,
            )
            tokens = bind_runtime_context(session_id, "skill-progress-run")
            try:
                with patch.object(runtime_tools, "get_installed_skill", return_value=skill):
                    runtime_tools._auto_register_tool_result(
                        "use_skill",
                        {
                            "skill_name": "remote-skill",
                            "request": "lark document create doc-token",
                        },
                        output,
                    )
            finally:
                reset_runtime_context(tokens)

            context = store.get_resume_context(session_id)
            todo = context["completed_todos"][0]

        self.assertEqual(context["primary_result"]["status"], "written")
        self.assertEqual(context["primary_result"]["token"], "doc-token")
        self.assertEqual(todo["task_id"], "create_document")
        self.assertEqual(todo["status"], "completed")
        self.assertIn("doc-token", todo["result_ref"])

    def test_successful_skill_result_does_not_complete_unrelated_active_task(self):
        from backend import tools as runtime_tools
        from backend.runtime_context import bind_runtime_context, reset_runtime_context
        from backend.session_store import SessionStore
        from backend.skills import SkillDefinition

        with tempfile.TemporaryDirectory() as raw_root:
            root = Path(raw_root)
            runtime_tools.set_allowed_root(root, external_read_roots=[])
            store = SessionStore(root)
            session_id = "skill-progress-unrelated"
            store.set_task_plan(
                session_id,
                [
                    {
                        "task_id": "prepare_data",
                        "content": "Prepare source data",
                        "activeForm": "Preparing source data",
                        "status": "in_progress",
                    },
                    {
                        "task_id": "create_document",
                        "content": "Create the requested document",
                        "activeForm": "Creating the requested document",
                        "status": "pending",
                    },
                ],
            )
            skill = SkillDefinition(
                name="remote-skill",
                description="",
                prompt_template="",
                source_path=str(root),
                manifest_config={"subcommand_operations": {"lark document": {"create": "create"}}},
            )
            tokens = bind_runtime_context(session_id, "skill-progress-unrelated-run")
            try:
                with patch.object(runtime_tools, "get_installed_skill", return_value=skill):
                    runtime_tools._auto_register_tool_result(
                        "use_skill",
                        {
                            "skill_name": "remote-skill",
                            "request": "lark document create doc-token",
                        },
                        json.dumps({"success": True, "token": "doc-token"}),
                    )
            finally:
                reset_runtime_context(tokens)

            todos = store.load_task_plan(session_id)["todos"]

        self.assertEqual(todos[0]["status"], "in_progress")
        self.assertEqual(todos[1]["status"], "completed")


if __name__ == "__main__":
    unittest.main()
