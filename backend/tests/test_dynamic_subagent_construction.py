"""Dynamic subagent construction: resolution semantics and hard guardrails.

Covers openspec change `add-dynamic-subagent-construction`:
- override semantics of on-construction fields over preset type templates
- budget clamping and prompt-length rejection
- recursion blocking and tool-boundary tightening
- background concurrency bound and resume fidelity
"""

import asyncio
import json
import shutil
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from langchain_core.messages import AIMessage

from backend.subagents import (
    SUBAGENT_MAX_TURNS_HARD_CAP,
    SUBAGENT_SYSTEM_PROMPT_MAX_CHARS,
    AgentDefinition,
    AsyncSubagentManager,
    built_in_subagents,
    definition_from_payload,
    definition_to_payload,
    filter_tools_for_subagent,
    resolve_agent_definition,
    run_subagent,
)


def make_tools(*names: str) -> list:
    return [SimpleNamespace(name=name) for name in names]


def tool_names(tools: list) -> set[str]:
    return {getattr(tool, "name", "") for tool in tools}


def wait_until(predicate, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


class TestResolveAgentDefinition(unittest.TestCase):
    def test_explicit_fields_override_preset(self):
        definition, meta = resolve_agent_definition(
            subagent_type="Explore",
            system_prompt="CUSTOM ROLE",
            max_turns=7,
        )
        self.assertEqual(definition.system_prompt, "CUSTOM ROLE")
        self.assertEqual(definition.max_turns, 7)
        self.assertTrue(meta["custom_system_prompt"])
        self.assertFalse(meta["base_type_fallback"])

    def test_omitted_fields_use_preset(self):
        preset = built_in_subagents()["Explore"]
        definition, meta = resolve_agent_definition(subagent_type="Explore")
        self.assertEqual(definition.system_prompt, preset.system_prompt)
        self.assertEqual(definition.max_turns, preset.max_turns)
        self.assertEqual(definition.disallowed_tool_names, preset.disallowed_tool_names)
        self.assertIsNone(definition.allowed_tool_names)
        self.assertFalse(meta["custom_system_prompt"])

    def test_disallowed_union_only_tightens(self):
        preset = built_in_subagents()["Explore"]
        definition, _ = resolve_agent_definition(
            subagent_type="Explore",
            disallowed_tools=["web_search"],
        )
        self.assertTrue(preset.disallowed_tool_names.issubset(definition.disallowed_tool_names))
        self.assertIn("web_search", definition.disallowed_tool_names)

    def test_allowed_tools_become_whitelist(self):
        definition, _ = resolve_agent_definition(
            subagent_type="general-purpose",
            allowed_tools=["read_file", "glob"],
        )
        self.assertEqual(definition.allowed_tool_names, {"read_file", "glob"})

    def test_unknown_type_falls_back_and_is_flagged(self):
        definition, meta = resolve_agent_definition(
            subagent_type="no-such-type",
            system_prompt="CUSTOM ROLE",
        )
        self.assertTrue(meta["base_type_fallback"])
        self.assertEqual(meta["base_type"], "general-purpose")
        self.assertEqual(definition.system_prompt, "CUSTOM ROLE")

    def test_known_type_not_flagged_as_fallback(self):
        _, meta = resolve_agent_definition(subagent_type="verification")
        self.assertFalse(meta["base_type_fallback"])
        self.assertEqual(meta["base_type"], "verification")


class TestBudgetGuardrails(unittest.TestCase):
    def test_max_turns_clamped_to_hard_cap(self):
        definition, meta = resolve_agent_definition(
            subagent_type="general-purpose",
            max_turns=SUBAGENT_MAX_TURNS_HARD_CAP + 500,
        )
        self.assertEqual(definition.max_turns, SUBAGENT_MAX_TURNS_HARD_CAP)
        self.assertEqual(meta["effective_max_turns"], SUBAGENT_MAX_TURNS_HARD_CAP)

    def test_max_turns_zero_uses_preset_default(self):
        preset = built_in_subagents()["Plan"]
        definition, _ = resolve_agent_definition(subagent_type="Plan", max_turns=0)
        self.assertEqual(definition.max_turns, preset.max_turns)

    def test_oversized_system_prompt_rejected(self):
        with self.assertRaises(ValueError) as ctx:
            resolve_agent_definition(
                subagent_type="general-purpose",
                system_prompt="x" * (SUBAGENT_SYSTEM_PROMPT_MAX_CHARS + 1),
            )
        self.assertIn("system_prompt", str(ctx.exception))


class TestDefinitionPayloadRoundTrip(unittest.TestCase):
    def test_round_trip_preserves_fields(self):
        definition, _ = resolve_agent_definition(
            subagent_type="Explore",
            system_prompt="CUSTOM ROLE",
            allowed_tools=["read_file", "glob"],
            disallowed_tools=["web_search"],
            max_turns=9,
        )
        payload = definition_to_payload(definition)
        json.dumps(payload)  # must be JSON serializable for task state persistence
        restored = definition_from_payload(payload)
        self.assertEqual(restored.system_prompt, definition.system_prompt)
        self.assertEqual(restored.allowed_tool_names, definition.allowed_tool_names)
        self.assertEqual(restored.disallowed_tool_names, definition.disallowed_tool_names)
        self.assertEqual(restored.max_turns, definition.max_turns)

    def test_round_trip_without_whitelist(self):
        definition, _ = resolve_agent_definition(subagent_type="general-purpose")
        restored = definition_from_payload(definition_to_payload(definition))
        self.assertIsNone(restored.allowed_tool_names)


class TestRecursionBlockingAndToolBoundary(unittest.TestCase):
    def setUp(self):
        self.tools = make_tools(
            "read_file", "glob", "write_file", "web_search", "Agent", "SendMessage"
        )

    def test_recursion_tools_blocked_for_every_preset(self):
        for agent_type, definition in built_in_subagents().items():
            filtered, _ = filter_tools_for_subagent(self.tools, definition)
            names = tool_names(filtered)
            self.assertNotIn("Agent", names, agent_type)
            self.assertNotIn("SendMessage", names, agent_type)

    def test_recursion_blocked_even_when_whitelisted(self):
        definition, _ = resolve_agent_definition(
            subagent_type="general-purpose",
            allowed_tools=["Agent", "read_file"],
        )
        filtered, _ = filter_tools_for_subagent(self.tools, definition)
        self.assertEqual(tool_names(filtered), {"read_file"})

    def test_allowed_whitelist_filters_to_subset(self):
        definition, _ = resolve_agent_definition(
            subagent_type="general-purpose",
            allowed_tools=["read_file"],
        )
        filtered, warnings = filter_tools_for_subagent(self.tools, definition)
        self.assertEqual(tool_names(filtered), {"read_file"})
        self.assertEqual(warnings, [])

    def test_allowed_unknown_names_dropped_with_warning(self):
        definition, _ = resolve_agent_definition(
            subagent_type="general-purpose",
            allowed_tools=["read_file", "ghost_tool"],
        )
        filtered, warnings = filter_tools_for_subagent(self.tools, definition)
        self.assertEqual(tool_names(filtered), {"read_file"})
        self.assertTrue(any("ghost_tool" in warning for warning in warnings))

    def test_allowed_matching_nothing_raises(self):
        definition, _ = resolve_agent_definition(
            subagent_type="general-purpose",
            allowed_tools=["ghost_tool"],
        )
        with self.assertRaises(ValueError):
            filter_tools_for_subagent(self.tools, definition)

    def test_disallowed_union_applies_to_filtering(self):
        definition, _ = resolve_agent_definition(
            subagent_type="Explore",
            disallowed_tools=["web_search"],
        )
        filtered, _ = filter_tools_for_subagent(self.tools, definition)
        names = tool_names(filtered)
        self.assertNotIn("write_file", names)
        self.assertNotIn("web_search", names)
        self.assertIn("read_file", names)


class TestRunSubagentDefinitionIntegration(unittest.TestCase):
    """run_subagent end-to-end with the LLM and tool registry patched out."""

    def setUp(self):
        self.workspace = Path(__file__).parent.parent / "tmp_test_dynamic_subagents"
        self.workspace.mkdir(parents=True, exist_ok=True)
        self.captured: dict = {}

        class FakeAgent:
            async def ainvoke(self, values, config=None):
                self.captured_tools = None
                return {"messages": [AIMessage(content="done")]}

        self.fake_agent = FakeAgent()

        def fake_create_react_agent(*, model, tools, state_schema=None):
            self.captured["tools"] = list(tools)
            return self.fake_agent

        async def fake_get_all_tools(workspace_dir=None, rag_config_overrides=None):
            return make_tools("read_file", "write_file", "web_search", "Agent", "SendMessage")

        self._patches = [
            patch("backend.subagents.load_llm_config", lambda config_path=None: {}),
            patch("backend.subagents.create_chat_deepseek", lambda config=None: object()),
            patch("backend.tools.get_all_tools", fake_get_all_tools),
            patch("langgraph.prebuilt.create_react_agent", fake_create_react_agent),
        ]
        for item in self._patches:
            item.start()
        self.addCleanup(self._stop_patches)

    def _stop_patches(self):
        for item in self._patches:
            item.stop()
        shutil.rmtree(self.workspace, ignore_errors=True)

    def test_constructed_definition_applied_and_summary_reported(self):
        definition, meta = resolve_agent_definition(
            subagent_type="general-purpose",
            system_prompt="CUSTOM ROLE",
            allowed_tools=["read_file", "Agent"],
            max_turns=5,
        )
        payload = asyncio.run(
            run_subagent(
                prompt="task",
                description="desc",
                workspace_dir=self.workspace,
                definition=definition,
                resolution_meta=meta,
            )
        )
        self.assertEqual(tool_names(self.captured["tools"]), {"read_file"})
        summary = payload["definition_summary"]
        self.assertTrue(summary["custom_system_prompt"])
        self.assertFalse(summary["base_type_fallback"])
        self.assertEqual(summary["effective_tool_count"], 1)
        self.assertEqual(summary["effective_max_turns"], 5)
        self.assertEqual(payload["warnings"], [])

    def test_preset_path_blocks_write_tools(self):
        payload = asyncio.run(
            run_subagent(
                prompt="task",
                description="desc",
                subagent_type="Explore",
                workspace_dir=self.workspace,
            )
        )
        self.assertNotIn("write_file", tool_names(self.captured["tools"]))
        summary = payload["definition_summary"]
        self.assertFalse(summary["custom_system_prompt"])
        self.assertEqual(summary["base_type"], "Explore")


class TestAgentToolConstructionFields(unittest.TestCase):
    def setUp(self):
        from backend import tools as runtime_tools
        from backend.subagent_runtime import get_subagent_manager

        self.runtime_tools = runtime_tools
        self.manager = get_subagent_manager()
        self._original_root = runtime_tools._ALLOWED_ROOT
        self.tmp_root = Path(__file__).parent.parent / "tmp_test_dynamic_subagents"
        self.tmp_root.mkdir(parents=True, exist_ok=True)
        runtime_tools.set_allowed_root(self.tmp_root)

        async def neutral_fake_run(**kwargs):
            return {
                "agent_id": kwargs["agent_id"],
                "result": "ok",
                "duration_seconds": 0.01,
                "definition_summary": {},
                "warnings": [],
            }

        # Never touch the real LLM or tool registry from tool-level tests.
        self._patches = [
            patch("backend.tools.run_subagent", neutral_fake_run),
            patch("backend.subagents.run_subagent", neutral_fake_run),
        ]
        for item in self._patches:
            item.start()
        self.addCleanup(self._restore)

    def _restore(self):
        for item in self._patches:
            item.stop()
        self.runtime_tools._ALLOWED_ROOT = self._original_root
        shutil.rmtree(self.tmp_root, ignore_errors=True)

    def _invoke(self, **overrides):
        from backend.tools import Agent

        args = {
            "description": "desc",
            "prompt": "task prompt",
            "subagent_type": "general-purpose",
            "run_in_background": False,
        }
        args.update(overrides)
        return json.loads(Agent.invoke(args))

    def test_input_model_accepts_construction_fields(self):
        from backend.tools import AgentToolInput

        model = AgentToolInput(
            description="desc",
            prompt="task prompt",
            system_prompt="CUSTOM ROLE",
            allowed_tools=["read_file"],
            disallowed_tools=["bash"],
            max_turns=9,
        )
        self.assertEqual(model.max_turns, 9)

    def test_rejects_oversized_system_prompt_without_dispatch(self):
        from backend.subagents import SUBAGENT_SYSTEM_PROMPT_MAX_CHARS

        before = set(self.manager._tasks.keys())
        result = self._invoke(system_prompt="x" * (SUBAGENT_SYSTEM_PROMPT_MAX_CHARS + 1))
        self.assertEqual(result["status"], "rejected")
        self.assertIn("system_prompt", result["error"])
        self.assertEqual(set(self.manager._tasks.keys()), before)

    def test_sync_result_carries_definition_summary_and_warnings(self):
        summary = {
            "base_type": "general-purpose",
            "base_type_fallback": False,
            "custom_system_prompt": True,
            "effective_tool_count": 3,
            "effective_max_turns": 9,
        }

        async def fake_run_subagent(**kwargs):
            return {
                "agent_id": "subagent-fake1",
                "result": "ok",
                "duration_seconds": 0.01,
                "definition_summary": summary,
                "warnings": ["allowed_tools dropped unknown tool name: ghost_tool"],
            }

        with patch("backend.tools.run_subagent", fake_run_subagent):
            result = self._invoke(system_prompt="CUSTOM ROLE", max_turns=9)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["definition_summary"], summary)
        self.assertTrue(any("ghost_tool" in warning for warning in result["warnings"]))

    def test_background_launch_persists_definition_and_reports_summary(self):
        captured = {}

        async def fake_run_subagent(**kwargs):
            captured.update(kwargs)
            return {
                "agent_id": kwargs["agent_id"],
                "result": "ok",
                "duration_seconds": 0.01,
                "definition_summary": {
                    "base_type": "general-purpose",
                    "base_type_fallback": False,
                    "custom_system_prompt": True,
                    "effective_tool_count": 2,
                    "effective_max_turns": kwargs.get("definition").max_turns,
                },
                "warnings": [],
            }

        with patch("backend.subagents.run_subagent", fake_run_subagent):
            result = self._invoke(
                run_in_background=True,
                system_prompt="CUSTOM ROLE",
                max_turns=9,
            )
            self.assertEqual(result["status"], "async_launched")
            self.assertTrue(result["definition_summary"]["custom_system_prompt"])
            self.assertEqual(result["definition_summary"]["effective_max_turns"], 9)
            agent_id = result["agent_id"]
            # Wait while the patched fake is still active so the thread
            # cannot fall through to the class-level neutral fake.
            self.assertTrue(
                wait_until(lambda: (self.manager.get_task(agent_id, self.tmp_root) or {}).get("status") == "idle")
            )
        state = self.manager.get_task(agent_id, self.tmp_root)
        self.assertEqual(state["definition"]["system_prompt"], "CUSTOM ROLE")
        self.assertEqual(state["definition_summary"]["effective_tool_count"], 2)
        self.assertEqual(captured["definition"].system_prompt, "CUSTOM ROLE")


class TestBackgroundConcurrencyAndResume(unittest.TestCase):
    def setUp(self):
        self.manager = AsyncSubagentManager()
        self.workspace = Path(__file__).parent.parent / "tmp_test_dynamic_subagents"
        self.workspace.mkdir(parents=True, exist_ok=True)
        self.captured: list[dict] = []
        self.release = threading.Event()

        async def fake_run(**kwargs):
            self.captured.append(kwargs)
            self.release.wait(timeout=10)
            return {
                "agent_id": kwargs["agent_id"],
                "result": "ok",
                "duration_seconds": 0.01,
                "definition_summary": {"custom_system_prompt": True},
                "warnings": [],
            }

        self._patch = patch("backend.subagents.run_subagent", fake_run)
        self._patch.start()
        self.addCleanup(self._restore)

    def _restore(self):
        self._patch.stop()
        self.release.set()
        shutil.rmtree(self.workspace, ignore_errors=True)

    def _launch(self, **overrides):
        args = {
            "prompt": "task",
            "description": "desc",
            "subagent_type": "general-purpose",
            "workspace_dir": self.workspace,
        }
        args.update(overrides)
        return self.manager.launch(**args)

    def test_launch_queues_when_concurrency_full(self):
        from backend.subagents import SUBAGENT_MAX_CONCURRENCY

        acquired = []
        try:
            for _ in range(SUBAGENT_MAX_CONCURRENCY):
                self.manager._semaphore.acquire()
                acquired.append(True)
            state = self._launch()
            self.assertEqual(state["status"], "queued")
            time.sleep(0.2)
            self.assertEqual(self.manager._tasks[state["agent_id"]]["status"], "queued")
        finally:
            for _ in acquired:
                self.manager._semaphore.release()
        self.release.set()
        self.assertTrue(
            wait_until(lambda: self.manager._tasks[state["agent_id"]]["status"] == "idle")
        )

    def test_failed_run_releases_concurrency_slot(self):
        async def failing_run(**kwargs):
            raise RuntimeError("boom")

        with patch("backend.subagents.run_subagent", failing_run):
            failing = self._launch()
            self.assertTrue(
                wait_until(lambda: self.manager._tasks[failing["agent_id"]]["status"] == "failed")
            )
        # The failed run released its slot: the semaphore is immediately acquirable.
        self.assertTrue(self.manager._semaphore.acquire(blocking=False))
        self.manager._semaphore.release()

    def test_resume_keeps_constructed_definition(self):
        definition, meta = resolve_agent_definition(
            subagent_type="general-purpose",
            system_prompt="CUSTOM ROLE",
            allowed_tools=["read_file"],
            max_turns=6,
        )
        state = self._launch(definition=definition, resolution_meta=meta)
        agent_id = state["agent_id"]
        self.release.set()
        self.assertTrue(
            wait_until(lambda: self.manager._tasks[agent_id]["status"] == "idle")
        )
        self.assertTrue(self.manager.send_message(agent_id, "follow-up", workspace_dir=self.workspace))
        self.assertTrue(
            wait_until(lambda: len(self.captured) >= 2 and self.manager._tasks[agent_id]["status"] == "idle")
        )
        resumed_definition = self.captured[1].get("definition")
        self.assertIsNotNone(resumed_definition)
        self.assertEqual(resumed_definition.system_prompt, "CUSTOM ROLE")
        self.assertEqual(resumed_definition.allowed_tool_names, {"read_file"})
        self.assertEqual(resumed_definition.max_turns, 6)

    def test_task_state_persists_resolved_definition(self):
        definition, meta = resolve_agent_definition(
            subagent_type="Explore",
            system_prompt="CUSTOM ROLE",
        )
        state = self._launch(definition=definition, resolution_meta=meta)
        agent_id = state["agent_id"]
        # launch persists the task state synchronously before the thread starts.
        persisted = _read_state(self.workspace, agent_id)
        self.assertIsNotNone(persisted)
        self.assertEqual(persisted["definition"]["system_prompt"], "CUSTOM ROLE")
        self.assertEqual(persisted["definition"]["agent_type"], "Explore")


def _read_state(workspace: Path, agent_id: str):
    from backend.subagents import read_subagent_task_state

    return read_subagent_task_state(workspace, agent_id)


if __name__ == "__main__":
    unittest.main()
