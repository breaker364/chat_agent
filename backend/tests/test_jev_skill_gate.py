"""TDD tests for the Jev skill preselection gate (group 5)."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import patch

from backend.config import load_jev_config
from backend.jev_client import JevClient, JevSkillGate, build_skill_suggestion, skill_gate_from_config


def _mock_skill_client(choice="demo-skill", confidence=0.9):
    return JevClient(
        load_jev_config(
            raw={
                "jev": {
                    "enabled": True,
                    "mode": "mock",
                    "mock_answers": {"skill": {"choice": choice, "confidence": confidence}},
                }
            }
        )
    )


CATALOG = [("demo-skill", "Does demo things"), ("other-skill", "Other things")]


class TestJevSkillGate:
    def test_high_confidence_selects_skill(self):
        gate = JevSkillGate(_mock_skill_client(), min_confidence=0.7)
        assert gate.select("do a demo thing", CATALOG) == "demo-skill"

    def test_low_confidence_returns_empty(self):
        gate = JevSkillGate(_mock_skill_client(confidence=0.3), min_confidence=0.7)
        assert gate.select("do a demo thing", CATALOG) == ""

    def test_unknown_answer_returns_empty(self):
        gate = JevSkillGate(_mock_skill_client(choice="not-in-catalog"), min_confidence=0.7)
        assert gate.select("do a demo thing", CATALOG) == ""

    def test_empty_catalog_skips_request(self):
        class MustNotCall:
            def ask(self, state, questions):
                raise AssertionError("must not be called")

        gate = JevSkillGate(MustNotCall(), min_confidence=0.7)
        assert gate.select("anything", []) == ""

    def test_failure_returns_empty(self):
        class Broken:
            def ask(self, state, questions):
                raise RuntimeError("down")

        assert JevSkillGate(Broken(), min_confidence=0.7).select("x", CATALOG) == ""


def test_build_skill_suggestion_disabled_returns_empty():
    assert build_skill_suggestion(root=None, message="x", raw={}) == ""


def test_build_skill_suggestion_high_confidence_mentions_skill():
    def fake_catalog(root):
        return [SimpleNamespace(name="demo-skill", description="Does demo things")]

    with patch("backend.skills.list_skill_catalog", side_effect=fake_catalog):
        suggestion = build_skill_suggestion(
            root="unused",
            message="do a demo thing",
            raw={
                "jev": {
                    "enabled": True,
                    "mode": "mock",
                    "gates": {"skill": {"enabled": True, "min_confidence": 0.7}},
                    "mock_answers": {"skill": {"choice": "demo-skill", "confidence": 0.9}},
                }
            },
        )
    assert "demo-skill" in suggestion


def test_build_skill_suggestion_low_confidence_empty():
    with patch("backend.skills.list_skill_catalog", return_value=[]):
        suggestion = build_skill_suggestion(
            root="unused",
            message="do a demo thing",
            raw={
                "jev": {
                    "enabled": True,
                    "mode": "mock",
                    "gates": {"skill": {"enabled": True, "min_confidence": 0.7}},
                    "mock_answers": {"skill": {"choice": "demo-skill", "confidence": 0.2}},
                }
            },
        )
    assert suggestion == ""


def test_skill_gate_factory_disabled():
    assert skill_gate_from_config(raw={}) is None


def test_agent_wiring_appends_suggestion_to_system_prompt():
    from backend.agent import stream_agent_events

    class CapturingAgent:
        def __init__(self):
            self.calls = []

        async def astream_events(self, values, *, config, version):
            self.calls.append(values["messages"])
            if False:
                yield {}

    async def consume():
        agent = CapturingAgent()
        events = [event async for event in stream_agent_events(agent, "new request", "jev-skill-test")]
        return events, agent

    capacity = {
        "model_context_window": 128000,
        "context_token_estimate": 100,
        "remaining_tokens": 127900,
        "is_exceeded": False,
    }
    with patch(
        "backend.jev_client.build_skill_suggestion", return_value="Skill suggestion: demo-skill"
    ), patch(
        "backend.agent.load_agent_memory_config", return_value={"enabled": False}
    ), patch(
        "backend.agent.load_llm_config", return_value={"model": "test-model", "base_url": "", "api_key": ""}
    ), patch(
        "backend.agent.load_context_compaction_config", return_value={"enabled": False}
    ), patch(
        "backend.agent.check_context_capacity", return_value=capacity
    ), patch(
        "backend.agent.get_skill_catalog_text", return_value=""
    ):
        events, agent = asyncio.run(consume())
    prompt_text = "\n".join(str(message.content) for message in agent.calls[0])
    assert "Skill suggestion: demo-skill" in prompt_text
