import json
import shutil
import unittest
from pathlib import Path
from uuid import uuid4

from backend.context_attachments import (
    build_context_attachments,
    collect_attachment_sources,
)


def _tool_call(name, args, call_id):
    return {"name": name, "args": args, "id": call_id, "type": "tool_call"}


class CollectAttachmentSourcesTests(unittest.TestCase):
    def test_collects_files_skills_and_agent_ids_most_recent_first(self):
        items = [
            {"role": "user", "content": "please check docs/early.md"},
            {
                "role": "assistant_tool_calls",
                "tool_calls": [_tool_call("read_file", {"path": "docs/early.md"}, "t1")],
            },
            {"role": "tool", "name": "read_file", "tool_call_id": "t1", "content": "data"},
            {
                "role": "assistant_tool_calls",
                "tool_calls": [
                    _tool_call("read_skill_detail", {"skill_name": "skill-one"}, "t2")
                ],
            },
            {
                "role": "assistant_tool_calls",
                "tool_calls": [_tool_call("read_file", {"path": "docs/late.md"}, "t3")],
            },
            {
                "role": "tool",
                "name": "Agent",
                "tool_call_id": "t4",
                "content": json.dumps(
                    {"status": "async_launched", "agent_id": "agent_1", "description": "research"}
                ),
            },
            {
                "role": "assistant_tool_calls",
                "tool_calls": [
                    _tool_call("use_skill", {"skill_name": "skill-two"}, "t5"),
                ],
            },
            {
                "role": "tool",
                "name": "Agent",
                "tool_call_id": "t6",
                "content": json.dumps(
                    {"status": "async_launched", "agent_id": "agent_2", "description": "scan"}
                ),
            },
            {
                "role": "assistant_tool_calls",
                "tool_calls": [
                    _tool_call("get_subagent_task", {"agent_id": "agent_2"}, "t7"),
                ],
            },
        ]

        sources = collect_attachment_sources(items)

        self.assertEqual(sources["files"], ["docs/late.md", "docs/early.md"])
        self.assertEqual(sources["skills"], ["skill-two", "skill-one"])
        self.assertEqual(sources["agent_ids"], ["agent_2", "agent_1"])

    def test_deduplicates_sources_keeping_most_recent_order(self):
        items = [
            {
                "role": "assistant_tool_calls",
                "tool_calls": [_tool_call("read_file", {"path": "docs/a.md"}, "t1")],
            },
            {
                "role": "assistant_tool_calls",
                "tool_calls": [_tool_call("read_file", {"path": "docs/a.md"}, "t2")],
            },
            {
                "role": "assistant_tool_calls",
                "tool_calls": [_tool_call("use_skill", {"skill_name": "s"}, "t3")],
            },
            {
                "role": "assistant_tool_calls",
                "tool_calls": [_tool_call("read_skill_detail", {"skill_name": "s"}, "t4")],
            },
        ]

        sources = collect_attachment_sources(items)

        self.assertEqual(sources["files"], ["docs/a.md"])
        self.assertEqual(sources["skills"], ["s"])

    def test_ignores_mentions_in_plain_messages_and_unknown_tools(self):
        items = [
            {"role": "user", "content": 'please read_file("docs/secret.md") and use skill-one'},
            {"role": "assistant", "content": "agent_id=agent_999"},
            {
                "role": "assistant_tool_calls",
                "tool_calls": [_tool_call("web_search", {"query": "read_file example"}, "t1")],
            },
            {
                "role": "tool",
                "name": "web_search",
                "tool_call_id": "t1",
                "content": json.dumps({"agent_id": "agent_888"}),
            },
        ]

        sources = collect_attachment_sources(items)

        self.assertEqual(sources, {"files": [], "skills": [], "agent_ids": []})


class BuildContextAttachmentsTests(unittest.TestCase):
    def setUp(self):
        temp_root = Path.cwd() / "tmp" / "unittest"
        temp_root.mkdir(parents=True, exist_ok=True)
        self.temp_path = temp_root / f"{self._testMethodName}-{uuid4().hex}"
        self.temp_path.mkdir(parents=True, exist_ok=False)

    def tearDown(self):
        shutil.rmtree(self.temp_path, ignore_errors=True)

    def _write_workspace_file(self, relative_path, text):
        target = self.temp_path / relative_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
        # read_file resolves paths against the process workspace root (cwd),
        # so record the cwd-relative path exactly as agent history would.
        return (self.temp_path / relative_path).relative_to(Path.cwd()).as_posix()

    def _settings(self, **overrides):
        settings = {
            "attachments_enabled": True,
            "attachment_total_tokens": 16_000,
            "attachment_file_max_tokens": 4_000,
            "attachment_file_limit": 5,
            "attachment_skill_max_tokens": 2_000,
            "attachment_skill_limit": 3,
        }
        settings.update(overrides)
        return settings

    def test_file_attachment_reads_fresh_workspace_content(self):
        path = self._write_workspace_file("docs/report.md", "alpha line\nbeta line\n")
        sources = {"files": [path], "skills": [], "agent_ids": []}

        items, details = build_context_attachments(
            sources, workspace_root=self.temp_path, settings=self._settings()
        )

        self.assertEqual(len(items), 1)
        item = items[0]
        self.assertEqual(item["role"], "context_attachment")
        self.assertEqual(item["attachment_type"], "file")
        self.assertEqual(item["source_ref"], path)
        self.assertIn("alpha line", item["content"])
        self.assertIn(path, item["content"])
        self.assertGreater(item["token_count"], 0)
        self.assertEqual(details["counts"]["file"], 1)
        self.assertEqual(details["skipped"], [])

    def test_file_attachment_truncates_to_token_budget_with_hint(self):
        body = "\n".join(f"line {index} with some filler text" for index in range(400))
        path = self._write_workspace_file("docs/big.md", body)
        sources = {"files": [path], "skills": [], "agent_ids": []}

        items, _ = build_context_attachments(
            sources,
            workspace_root=self.temp_path,
            settings=self._settings(attachment_file_max_tokens=40),
        )

        self.assertEqual(len(items), 1)
        content = items[0]["content"]
        self.assertLessEqual(items[0]["token_count"], 40)
        self.assertIn("truncated", content.lower())
        self.assertIn('read_file("%s")' % path, content)
        self.assertNotIn("line 399", content)

    def test_missing_file_is_skipped_without_failure(self):
        sources = {"files": ["docs/does-not-exist.md"], "skills": [], "agent_ids": []}

        items, details = build_context_attachments(
            sources, workspace_root=self.temp_path, settings=self._settings()
        )

        self.assertEqual(items, [])
        self.assertEqual(details["counts"]["file"], 0)
        self.assertTrue(any("does-not-exist.md" in reason for reason in details["skipped"]))

    def test_file_limit_keeps_most_recent_and_skips_rest(self):
        first = self._write_workspace_file("docs/first.txt", "first content")
        second = self._write_workspace_file("docs/second.txt", "second content")
        # collect_attachment_sources emits most-recent-first, so the later
        # write must come first in the list.
        sources = {"files": [second, first], "skills": [], "agent_ids": []}

        items, details = build_context_attachments(
            sources,
            workspace_root=self.temp_path,
            settings=self._settings(attachment_file_limit=1),
        )

        self.assertEqual([item["source_ref"] for item in items], [second])
        self.assertTrue(any(first in reason for reason in details["skipped"]))

    def test_skill_attachment_uses_skill_markdown_head(self):
        skill_body = "# demo-skill\n\n" + "\n".join(f"instruction {i}" for i in range(200))
        skill_dir = self.temp_path / "skills" / "demo-skill"
        skill_dir.mkdir(parents=True, exist_ok=True)
        (skill_dir / "SKILL.md").write_text(skill_body, encoding="utf-8")
        sources = {"files": [], "skills": ["demo-skill"], "agent_ids": []}

        items, details = build_context_attachments(
            sources, workspace_root=self.temp_path, settings=self._settings()
        )

        self.assertEqual(len(items), 1)
        item = items[0]
        self.assertEqual(item["attachment_type"], "skill")
        self.assertIn("demo-skill", item["title"])
        self.assertIn("instruction 0", item["content"])
        self.assertIn("demo-skill", item["source_ref"])
        self.assertTrue(item["source_ref"].endswith("SKILL.md"))
        self.assertEqual(details["counts"]["skill"], 1)

    def test_unknown_skill_is_skipped(self):
        sources = {"files": [], "skills": ["ghost-skill"], "agent_ids": []}

        items, details = build_context_attachments(
            sources, workspace_root=self.temp_path, settings=self._settings()
        )

        self.assertEqual(items, [])
        self.assertTrue(any("ghost-skill" in reason for reason in details["skipped"]))

    def test_subagent_attachment_summarizes_task_states(self):
        def reader(agent_id):
            if agent_id == "agent_running":
                return {
                    "agent_id": "agent_running",
                    "status": "running",
                    "description": "long research",
                }
            if agent_id == "agent_done":
                return {
                    "agent_id": "agent_done",
                    "status": "idle",
                    "description": "quick scan",
                    "result": "finished cleanly",
                }
            return None

        sources = {
            "files": [],
            "skills": [],
            "agent_ids": ["agent_done", "agent_running", "agent_lost"],
        }

        items, details = build_context_attachments(
            sources,
            workspace_root=self.temp_path,
            settings=self._settings(),
            task_reader=reader,
        )

        self.assertEqual(len(items), 1)
        item = items[0]
        self.assertEqual(item["attachment_type"], "subagent")
        content = item["content"]
        self.assertIn("agent_running", content)
        self.assertIn("running", content)
        self.assertIn("agent_done", content)
        self.assertIn("finished cleanly", content)
        lowered = content.lower()
        self.assertIn("re-dispatch", lowered)
        self.assertTrue(any("agent_lost" in reason for reason in details["skipped"]))
        self.assertEqual(details["counts"]["subagent"], 1)

    def test_disabled_returns_empty_attachments(self):
        sources = {"files": ["docs/a.md"], "skills": [], "agent_ids": []}

        items, details = build_context_attachments(
            sources,
            workspace_root=self.temp_path,
            settings=self._settings(attachments_enabled=False),
        )

        self.assertEqual(items, [])
        self.assertEqual(details["counts"], {"file": 0, "skill": 0, "subagent": 0})
        self.assertEqual(details["tokens"], 0)

    def test_total_token_budget_bounds_all_attachments(self):
        first = self._write_workspace_file("docs/one.txt", "content one " * 200)
        second = self._write_workspace_file("docs/two.txt", "content two " * 200)
        sources = {"files": [first, second], "skills": [], "agent_ids": []}

        items, details = build_context_attachments(
            sources,
            workspace_root=self.temp_path,
            settings=self._settings(attachment_total_tokens=60),
        )

        self.assertLessEqual(sum(item["token_count"] for item in items), 60)
        self.assertLessEqual(details["tokens"], 60)


if __name__ == "__main__":
    unittest.main()
