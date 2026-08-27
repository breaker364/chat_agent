from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from pydantic import ValidationError


class ShellToolContractTests(unittest.TestCase):
    def test_shell_config_has_conservative_defaults_and_rejects_invalid_values(self):
        from backend.config import ShellToolsConfigError, load_shell_tools_config

        defaults = load_shell_tools_config(raw={})

        self.assertTrue(defaults["enabled"])
        self.assertFalse(defaults["bash_enabled"])
        self.assertEqual(defaults["default_timeout_seconds"], 30)
        self.assertLessEqual(defaults["max_output_chars"], 12000)
        self.assertLessEqual(defaults["max_glob_matches"], 200)
        self.assertLessEqual(defaults["max_grep_matches"], 200)
        self.assertEqual(defaults["environment_allowlist"], [])

        with self.assertRaises(ShellToolsConfigError):
            load_shell_tools_config(raw={"max_output_chars": 0})
        with self.assertRaises(ShellToolsConfigError):
            load_shell_tools_config(raw={"unsupported_setting": True})

    def test_input_models_reject_empty_or_oversized_values(self):
        from backend.workspace_shell_tools import BashInput, GlobInput, GrepInput

        with self.assertRaises(ValidationError):
            GlobInput(pattern="")
        with self.assertRaises(ValidationError):
            GlobInput(pattern="x" * 4097)
        with self.assertRaises(ValidationError):
            GrepInput(pattern="x" * 4097)
        with self.assertRaises(ValidationError):
            BashInput(command="x" * 8193)
        with self.assertRaises(ValidationError):
            BashInput(command="echo ok", timeout_seconds=0)

        self.assertEqual(GlobInput(pattern="*.txt", max_output_chars=256).max_output_chars, 256)
        self.assertEqual(GrepInput(pattern="needle", max_output_chars=256).max_output_chars, 256)

    def test_shell_tools_have_stable_names_and_typed_schemas(self):
        from backend.workspace_shell_tools import BashInput, GlobInput, GrepInput, bash, glob, grep

        self.assertEqual((bash.name, glob.name, grep.name), ("bash", "glob", "grep"))
        self.assertIs(bash.args_schema, BashInput)
        self.assertIs(glob.args_schema, GlobInput)
        self.assertIs(grep.args_schema, GrepInput)


if __name__ == "__main__":
    unittest.main()


class GlobToolTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.workspace = Path(self.temp_dir.name)
        from backend import tools as runtime_tools

        runtime_tools.set_allowed_root(self.workspace, external_read_roots=[])

    def tearDown(self):
        from backend import tools as runtime_tools

        runtime_tools.set_allowed_root(Path.cwd(), external_read_roots=[])
        self.temp_dir.cleanup()

    def _invoke(self, **payload):
        from backend.workspace_shell_tools import glob as glob_tool

        return json.loads(glob_tool.invoke(payload))

    def test_glob_returns_sorted_workspace_relative_files_and_directories(self):
        (self.workspace / "b.txt").write_text("b", encoding="utf-8")
        (self.workspace / "a.txt").write_text("a", encoding="utf-8")
        (self.workspace / "nested").mkdir()
        (self.workspace / "nested" / "c.txt").write_text("c", encoding="utf-8")

        result = self._invoke(pattern="**/*")

        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["matches"], ["a.txt", "b.txt", "nested", "nested/c.txt"])
        self.assertEqual(result["returned_count"], 4)
        self.assertFalse(result["truncated"])
        self.assertTrue(all(not Path(item).is_absolute() for item in result["matches"]))

    def test_glob_handles_empty_file_and_directory_patterns(self):
        (self.workspace / "note.md").write_text("note", encoding="utf-8")
        (self.workspace / "folder").mkdir()

        files = self._invoke(pattern="*.md")
        directory = self._invoke(pattern="folder")
        empty = self._invoke(pattern="*.missing")

        self.assertEqual(files["matches"], ["note.md"])
        self.assertEqual(directory["matches"], ["folder"])
        self.assertEqual(empty["matches"], [])
        self.assertFalse(empty["truncated"])

    def test_glob_normalizes_separators_and_reports_match_limit(self):
        (self.workspace / "nested").mkdir()
        (self.workspace / "nested" / "note.md").write_text("note", encoding="utf-8")
        (self.workspace / "nested" / "other.md").write_text("other", encoding="utf-8")

        normalized = self._invoke(pattern=r"nested\*.md")
        limited = self._invoke(pattern="**/*", max_matches=1)

        self.assertEqual(normalized["matches"], ["nested/note.md", "nested/other.md"])
        self.assertEqual(limited["limit"], 1)
        self.assertEqual(limited["returned_count"], 1)
        self.assertTrue(limited["truncated"])

    def test_glob_rejects_absolute_traversal_and_protected_scopes(self):
        outside = Path(self.temp_dir.name).parent / "outside.txt"
        outside.write_text("outside", encoding="utf-8")
        try:
            absolute = self._invoke(pattern="*.txt", scope=str(self.workspace))
            traversal = self._invoke(pattern="*.txt", scope="../")

            from backend import tools as runtime_tools
            from backend.runtime_context import bind_runtime_context, reset_runtime_context

            with patch.object(runtime_tools, "_knowledge_index_root", return_value=self.workspace / "knowledge_base" / "index"):
                tokens = bind_runtime_context("glob-test", "run-glob")
                try:
                    protected = self._invoke(pattern="**/*", scope="knowledge_base/index")
                finally:
                    reset_runtime_context(tokens)
        finally:
            outside.unlink(missing_ok=True)

        self.assertEqual(absolute["status"], "permission_denied")
        self.assertEqual(traversal["status"], "permission_denied")
        self.assertEqual(protected["status"], "permission_denied")

    def test_glob_does_not_return_symlink_targets_outside_workspace(self):
        external = Path(self.temp_dir.name).parent / "shell-tools-external"
        external.mkdir(exist_ok=False)
        (external / "secret.txt").write_text("secret", encoding="utf-8")
        link = self.workspace / "external-link"
        try:
            try:
                os.symlink(external, link, target_is_directory=True)
            except (OSError, NotImplementedError) as exc:
                self.skipTest(f"symlink creation unavailable: {exc}")

            result = self._invoke(pattern="**/*")

            self.assertNotIn("external-link/secret.txt", result["matches"])
        finally:
            if link.exists() or link.is_symlink():
                link.unlink()
            if external.exists():
                (external / "secret.txt").unlink(missing_ok=True)
                external.rmdir()


class GrepToolTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.workspace = Path(self.temp_dir.name)
        from backend import tools as runtime_tools

        runtime_tools.set_allowed_root(self.workspace, external_read_roots=[])

    def tearDown(self):
        from backend import tools as runtime_tools

        runtime_tools.set_allowed_root(Path.cwd(), external_read_roots=[])
        self.temp_dir.cleanup()

    def _invoke(self, **payload):
        from backend.workspace_shell_tools import grep as grep_tool

        return json.loads(grep_tool.invoke(payload))

    def test_grep_returns_deterministic_line_numbered_matches(self):
        (self.workspace / "b.txt").write_text("other\nneedle in b\n", encoding="utf-8")
        (self.workspace / "a.txt").write_text("needle one\nnot this\nneedle two\n", encoding="utf-8")

        result = self._invoke(pattern="needle")

        self.assertEqual(result["status"], "ok")
        self.assertEqual(
            result["matches"],
            [
                {"path": "a.txt", "line_number": 1, "line": "needle one"},
                {"path": "a.txt", "line_number": 3, "line": "needle two"},
                {"path": "b.txt", "line_number": 2, "line": "needle in b"},
            ],
        )
        self.assertEqual(result["files_scanned"], 2)

    def test_grep_handles_invalid_regex_no_matches_and_file_scope(self):
        (self.workspace / "note.txt").write_text("alpha\nbeta\n", encoding="utf-8")

        invalid = self._invoke(pattern="[")
        empty = self._invoke(pattern="needle")
        file_scope = self._invoke(pattern="beta", scope="note.txt")

        self.assertEqual(invalid["status"], "invalid_input")
        self.assertEqual(invalid["error_category"], "invalid_regex")
        self.assertEqual(invalid["files_scanned"], 0)
        self.assertEqual(empty["status"], "ok")
        self.assertEqual(empty["matches"], [])
        self.assertEqual(file_scope["matches"][0]["path"], "note.txt")
        self.assertEqual(file_scope["matches"][0]["line_number"], 2)

    def test_grep_skips_binary_and_oversized_files_with_bounded_diagnostics(self):
        (self.workspace / "binary.dat").write_bytes(b"needle\x00binary")
        (self.workspace / "large.txt").write_text("needle" * 20, encoding="utf-8")
        (self.workspace / "ok.txt").write_text("needle\n", encoding="utf-8")

        result = self._invoke(pattern="needle", max_file_bytes=16)

        self.assertEqual(result["status"], "ok")
        self.assertEqual([item["path"] for item in result["matches"]], ["ok.txt"])
        skipped = {item["path"]: item["reason"] for item in result["skipped"]}
        self.assertEqual(skipped["binary.dat"], "binary")
        self.assertEqual(skipped["large.txt"], "file_size_limit")

    def test_grep_reports_match_limit_and_protected_scope(self):
        (self.workspace / "a.txt").write_text("needle\nneedle\n", encoding="utf-8")
        (self.workspace / "b.txt").write_text("needle\n", encoding="utf-8")

        limited = self._invoke(pattern="needle", max_matches=2)

        from backend import tools as runtime_tools
        from backend.runtime_context import bind_runtime_context, reset_runtime_context

        with patch.object(runtime_tools, "_knowledge_index_root", return_value=self.workspace / "knowledge_base" / "index"):
            tokens = bind_runtime_context("grep-test", "run-grep")
            try:
                protected = self._invoke(pattern="needle", scope="knowledge_base/index")
            finally:
                reset_runtime_context(tokens)

        self.assertEqual(len(limited["matches"]), 2)
        self.assertTrue(limited["truncated"])
        self.assertIn("match_limit", limited["truncation_reasons"])
        self.assertEqual(protected["status"], "permission_denied")

    def test_grep_rejects_absolute_and_traversal_scopes(self):
        outside = Path(self.temp_dir.name).parent / "grep-outside.txt"
        outside.write_text("needle", encoding="utf-8")
        try:
            absolute = self._invoke(pattern="needle", scope=str(self.workspace))
            traversal = self._invoke(pattern="needle", scope="../")
        finally:
            outside.unlink(missing_ok=True)

        self.assertEqual(absolute["status"], "permission_denied")
        self.assertEqual(traversal["status"], "permission_denied")

    def test_grep_skips_symlink_targets_outside_workspace(self):
        external = Path(self.temp_dir.name).parent / "grep-symlink-external"
        external.mkdir(exist_ok=False)
        target = external / "secret.txt"
        target.write_text("needle", encoding="utf-8")
        link = self.workspace / "external-link.txt"
        try:
            try:
                os.symlink(target, link)
            except (OSError, NotImplementedError) as exc:
                self.skipTest(f"symlink creation unavailable: {exc}")

            result = self._invoke(pattern="needle")

            self.assertNotIn("external-link.txt", [item["path"] for item in result["matches"]])
            skipped = {item["path"]: item["reason"] for item in result["skipped"]}
            self.assertEqual(skipped["external-link.txt"], "symlink_outside_workspace")
        finally:
            if link.exists() or link.is_symlink():
                link.unlink()
            if target.exists():
                target.unlink()
            if external.exists():
                external.rmdir()


class BashToolTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.workspace = Path(self.temp_dir.name)
        from backend import tools as runtime_tools

        runtime_tools.set_allowed_root(self.workspace, external_read_roots=[])

    def tearDown(self):
        from backend import tools as runtime_tools

        runtime_tools.set_allowed_root(Path.cwd(), external_read_roots=[])
        self.temp_dir.cleanup()

    def _invoke(self, **payload):
        from backend.workspace_shell_tools import bash as bash_tool

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
        with patch("backend.workspace_shell_tools._config", return_value=settings):
            return json.loads(bash_tool.invoke(payload))

    def test_bash_returns_bounded_success_metadata_and_closed_stdin(self):
        result = self._invoke(command="import sys; print(sys.stdin.read() or 'ok')")

        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["exit_code"], 0)
        self.assertEqual(result["stdout"].strip(), "ok")
        self.assertEqual(result["stderr"], "")
        self.assertTrue(result["command_hash"].startswith("sha256:"))
        self.assertEqual(result["cwd"], str(self.workspace))
        self.assertGreaterEqual(result["stdout_chars"], 3)
        self.assertIn("duration_seconds", result)

    def test_bash_reports_nonzero_exit_and_preserves_bounded_streams(self):
        result = self._invoke(command="import sys; print('failure'); print('details', file=sys.stderr); sys.exit(3)")

        self.assertEqual(result["status"], "command_failed")
        self.assertEqual(result["exit_code"], 3)
        self.assertEqual(result["stdout"].strip(), "failure")
        self.assertEqual(result["stderr"].strip(), "details")
        self.assertEqual(result["error_category"], "nonzero_exit")

    def test_bash_reports_unavailable_without_shell_fallback(self):
        settings = {
            "enabled": True,
            "bash_enabled": False,
            "bash_executable": "",
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
        from backend.workspace_shell_tools import bash as bash_tool

        with patch("backend.workspace_shell_tools._config", return_value=settings):
            result = json.loads(bash_tool.invoke({"command": "print('must not run')"}))

        self.assertEqual(result["status"], "runtime_unavailable")
        self.assertEqual(result["error_category"], "bash_unavailable")
        self.assertNotIn("must not run", result.get("stdout", ""))

    def test_bash_rejects_cwd_outside_workspace_before_starting_process(self):
        outside = self._invoke(command="print('must not run')", working_directory=str(self.workspace.parent))
        traversal = self._invoke(command="print('must not run')", working_directory="../")

        self.assertEqual(outside["status"], "permission_denied")
        self.assertEqual(traversal["status"], "permission_denied")

    def test_bash_terminates_timed_out_process_and_preserves_partial_output(self):
        result = self._invoke(
            command="import time; print('before-timeout', flush=True); time.sleep(3)",
            timeout_seconds=1,
        )

        self.assertEqual(result["status"], "timed_out")
        self.assertTrue(result["timed_out"])
        self.assertIn("before-timeout", result["stdout"])
        self.assertLessEqual(result["stdout_chars"], 12000)

    def test_bash_caps_output_and_does_not_inherit_unlisted_environment_values(self):
        output = self._invoke(command="print('x' * 2000)", max_output_chars=256)
        with patch.dict(os.environ, {"SHELL_TOOLS_TEST_SECRET": "hidden"}, clear=False):
            environment = self._invoke(command="import os; print(os.environ.get('SHELL_TOOLS_TEST_SECRET'))")

        self.assertEqual(output["status"], "output_truncated")
        self.assertTrue(output["truncated"])
        self.assertLessEqual(output["stdout_chars"], 256)
        self.assertEqual(environment["stdout"].strip(), "None")


class ShellToolIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.workspace = Path(self.temp_dir.name)
        from backend import tools as runtime_tools

        runtime_tools.set_allowed_root(self.workspace, external_read_roots=[])

    def tearDown(self):
        from backend import tools as runtime_tools

        runtime_tools.clear_tool_dedupe_cache("shell-integration-run")
        runtime_tools.set_allowed_root(Path.cwd(), external_read_roots=[])
        self.temp_dir.cleanup()

    def test_get_all_tools_exposes_typed_shell_tools(self):
        from backend import tools as runtime_tools

        async def no_external_tools(*_args, **_kwargs):
            return []

        with patch.object(runtime_tools, "load_12306_tools", side_effect=no_external_tools), patch.object(
            runtime_tools, "load_mcd_tools", side_effect=no_external_tools
        ), patch.object(runtime_tools, "build_skill_tools", return_value=[]):
            tools = runtime_tools._run_coro_in_thread(
                runtime_tools.get_all_tools(
                    workspace_dir=self.workspace,
                    rag_config_overrides={"enabled": False},
                )
            )

        tool_map = {item.name: item for item in tools}
        self.assertTrue({"bash", "glob", "grep"}.issubset(tool_map))
        self.assertEqual(tool_map["bash"].args_schema.__name__, "BashInput")
        self.assertEqual(tool_map["glob"].args_schema.__name__, "GlobInput")
        self.assertEqual(tool_map["grep"].args_schema.__name__, "GrepInput")

    def test_search_tools_are_deduplicated_and_bash_is_not_cacheable(self):
        from backend import tools as runtime_tools
        from backend.runtime_context import bind_runtime_context, reset_runtime_context

        (self.workspace / "note.txt").write_text("needle", encoding="utf-8")
        wrapped_glob = runtime_tools._wrap_tool_with_run_dedupe(runtime_tools.glob)
        tokens = bind_runtime_context("shell-integration", "shell-integration-run")
        try:
            first = wrapped_glob.invoke({"pattern": "*.txt"})
            second = wrapped_glob.invoke({"pattern": "*.txt"})
        finally:
            reset_runtime_context(tokens)

        self.assertEqual(first, second)
        self.assertTrue(runtime_tools._tool_policy("glob")["cacheable"])
        self.assertFalse(runtime_tools._tool_policy("bash")["cacheable"])
        self.assertFalse(runtime_tools._tool_policy("bash")["side_effect"])

    def test_required_evidence_policy_denies_search_before_execution(self):
        from backend import tools as runtime_tools
        from backend.runtime_context import bind_runtime_context, reset_runtime_context

        wrapped = runtime_tools._wrap_tool_with_run_dedupe(runtime_tools.grep)
        tokens = bind_runtime_context("shell-integration", "shell-policy", knowledge_policy="required")
        try:
            result = json.loads(wrapped.invoke({"pattern": "needle"}))
        finally:
            reset_runtime_context(tokens)

        self.assertEqual(result["status"], "policy_denied")

    def test_bash_audit_contains_hash_cwd_status_and_no_environment_secret(self):
        from backend import tools as runtime_tools
        from backend.runtime_context import bind_runtime_context, reset_runtime_context
        from backend.session_store import SessionStore

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
        wrapped = runtime_tools._wrap_tool_with_run_dedupe(runtime_tools.bash)
        tokens = bind_runtime_context("shell-audit", "shell-audit-run")
        try:
            with patch("backend.workspace_shell_tools._config", return_value=settings):
                payload = json.loads(wrapped.invoke({"command": "print('audit-ok')"}))
        finally:
            reset_runtime_context(tokens)

        events = SessionStore(self.workspace).read_tool_events_tail("shell-audit", limit=10)
        audit = next(event for event in events if event.get("event") == "tool_runtime_executed")
        content = audit["content"]
        self.assertEqual(payload["status"], "ok")
        self.assertTrue(content["command_hash"].startswith("sha256:"))
        self.assertEqual(content["status"], "ok")
        self.assertEqual(content["cwd"], str(self.workspace))
        self.assertNotIn("SHELL_TOOLS_TEST_SECRET", json.dumps(audit, ensure_ascii=False))


class ShellToolResearchRoutingTests(unittest.TestCase):
    def test_read_only_search_tools_are_evidence_but_bash_is_not(self):
        from backend.agentic_research.runtime import EVIDENCE_TOOL_NAMES
        from backend.tools import _EVIDENCE_TOOL_NAMES, _source_kind_for_tool

        self.assertIn("glob", EVIDENCE_TOOL_NAMES)
        self.assertIn("grep", EVIDENCE_TOOL_NAMES)
        self.assertIn("glob", _EVIDENCE_TOOL_NAMES)
        self.assertIn("grep", _EVIDENCE_TOOL_NAMES)
        self.assertEqual(_source_kind_for_tool("glob"), "workspace")
        self.assertEqual(_source_kind_for_tool("grep"), "workspace")
        self.assertNotIn("bash", EVIDENCE_TOOL_NAMES)
        self.assertNotIn("bash", _EVIDENCE_TOOL_NAMES)
        self.assertIsNone(_source_kind_for_tool("bash"))

    def test_workspace_adapter_uses_structured_glob_and_grep_routes(self):
        from backend.agentic_research.adapters import WorkspaceEvidenceAdapter

        calls = []

        class StructuredCallback:
            def __init__(self, name, response):
                self.name = name
                self.response = response

            def invoke(self, payload):
                calls.append((self.name, payload))
                return json.dumps(self.response)

        run_glob = StructuredCallback(
            "glob",
            {"status": "ok", "matches": ["docs/a.md", "docs/b.md"], "returned_count": 2},
        )
        run_grep = StructuredCallback(
            "grep",
            {
                "status": "ok",
                "matches": [{"path": "docs/a.md", "line_number": 4, "line": "needle"}],
                "files_scanned": 1,
            },
        )

        adapter = WorkspaceEvidenceAdapter(
            workspace_root=".",
            read_file=lambda _payload: "unused",
            glob=run_glob,
            grep=run_grep,
        )

        glob_observation = adapter.collect("glob:docs/*.md")
        grep_observation = adapter.collect("grep:needle")

        self.assertEqual(calls, [("glob", {"pattern": "docs/*.md"}), ("grep", {"pattern": "needle"})])
        self.assertEqual(glob_observation.status, "ok")
        self.assertEqual(glob_observation.citations[0]["id"], "docs/a.md")
        self.assertEqual(grep_observation.status, "ok")
        self.assertIn("needle", grep_observation.excerpts[0])


class ShellToolOperationsTests(unittest.TestCase):
    def test_runtime_configuration_and_example_are_explicit_and_secret_free(self):
        runtime = json.loads(Path("runtime_config.json").read_text(encoding="utf-8"))
        example = json.loads(Path("config.example.json").read_text(encoding="utf-8"))

        for config in (runtime, example):
            shell_tools = config["shell_tools"]
            self.assertIn("enabled", shell_tools)
            self.assertIn("bash_enabled", shell_tools)
            self.assertIn("max_output_chars", shell_tools)
            self.assertFalse(any("secret" in str(value).lower() for value in shell_tools.values()))

    def test_prompts_describe_generic_search_and_bash_boundaries(self):
        combined = (
            Path("backend/prompts/system_prompt.md").read_text(encoding="utf-8")
            + Path("backend/prompts/agent_policy.md").read_text(encoding="utf-8")
        ).lower()

        for phrase in ("glob", "grep", "bash", "non-interactive", "workspace"):
            self.assertIn(phrase, combined)

    def test_diagnostics_report_policy_without_environment_values(self):
        from backend.workspace_shell_tools import get_shell_tools_diagnostics

        diagnostics = get_shell_tools_diagnostics()

        self.assertIn(diagnostics["bash_status"], {"disabled", "unavailable", "available"})
        self.assertIn(diagnostics["executable_policy"], {"configured", "path_discovery", "disabled"})
        self.assertNotIn("environment", diagnostics)
        self.assertNotIn("api_key", json.dumps(diagnostics).lower())
