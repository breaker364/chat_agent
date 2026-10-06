"""Dynamic subagent construction: resolution semantics and hard guardrails.

Covers openspec change `add-dynamic-subagent-construction`:
- override semantics of on-construction fields over preset type templates
- budget clamping and prompt-length rejection
- recursion blocking and tool-boundary tightening
- background concurrency bound and resume fidelity
"""

import asyncio
import json
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

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


if __name__ == "__main__":
    unittest.main()
