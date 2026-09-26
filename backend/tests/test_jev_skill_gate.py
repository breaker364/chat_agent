"""TDD tests for the tiered Jev skill preselection gate (group 5, revised)."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import patch

from backend.config import load_jev_config
from backend.jev_client import (
    JevClient,
    JevSkillGate,
    SkillSuggestion,
    build_skill_suggestion,
    skill_gate_from_config,
)


def _mock_skill_client(choice="feishu-personal", confidence=0.9):
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


CATALOG = [
    ("feishu-personal", "Read and operate Feishu resources"),
    ("frontend-design", "Visual design guidance"),
]


class TestJevSkillGateTiers:
    def test_high_confidence_returns_directive(self):
        gate = JevSkillGate(_mock_skill_client(confidence=0.9))
        suggestion = gate.select("read my feishu docs", CATALOG)
        assert suggestion.name == "feishu-personal"
        assert suggestion.tier == "directive"
        assert suggestion.confidence == 0.9

    def test_mid_confidence_returns_advisory(self):
        gate = JevSkillGate(_mock_skill_client(confidence=0.7))
        suggestion = gate.select("read my feishu docs", CATALOG)
        assert suggestion.name == "feishu-personal"
        assert suggestion.tier == "advisory"

    def test_boundary_080_is_directive(self):
        gate = JevSkillGate(_mock_skill_client(confidence=0.80))
        assert gate.select("x", CATALOG).tier == "directive"

    def test_low_confidence_returns_none(self):
        gate = JevSkillGate(_mock_skill_client(confidence=0.59))
        suggestion = gate.select("x", CATALOG)
        assert suggestion.name == ""
        assert suggestion.tier == "none"

    def test_no_skill_answer_never_injects_even_at_high_confidence(self):
        gate = JevSkillGate(_mock_skill_client(choice="no_skill", confidence=0.95))
        suggestion = gate.select("what is rabbitmq", CATALOG)
        assert suggestion.name == ""
        assert suggestion.tier == "none"

    def test_unknown_answer_returns_none(self):
        gate = JevSkillGate(_mock_skill_client(choice="not-in-catalog", confidence=0.95))
        suggestion = gate.select("x", CATALOG)
        assert suggestion.tier == "none"

    def test_no_skill_option_present_in_criteria(self):
        captured = {}

        class Capturing:
            def ask(self, state, questions):
                captured["criteria"] = questions[0].criteria
                raise RuntimeError("stop after capture")

        JevSkillGate(Capturing()).select("x", CATALOG)
        assert "no_skill" in captured["criteria"]

    def test_empty_catalog_skips_request(self):
        class MustNotCall:
            def ask(self, state, questions):
                raise AssertionError("must not be called")

        suggestion = JevSkillGate(MustNotCall()).select("anything", [])
        assert suggestion.tier == "none"

    def test_failure_returns_none(self):
        class Broken:
            def ask(self, state, questions):
                raise RuntimeError("down")

        assert JevSkillGate(Broken()).select("x", CATALOG).tier == "none"

    def test_decision_log_carries_tier(self):
        import json
        import logging

        records = []

        class Collector(logging.Handler):
            def emit(self, record):
                records.append(record.getMessage())

        logger = logging.getLogger("chat_agent.jev")
        handler = Collector()
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
        try:
            JevSkillGate(_mock_skill_client(confidence=0.9)).select("x", CATALOG)
        finally:
            logger.removeHandler(handler)
        payload = json.loads(records[0].split(" ", 1)[1])
        assert payload["extra"]["tier"] == "directive"
        assert payload["extra"]["answer"] == "feishu-personal"


class TestSkillGateConfig:
    def test_gate_thresholds_defaults(self):
        config = load_jev_config(raw={})
        assert config["gates"]["skill"]["advisory_min_confidence"] == 0.6
        assert config["gates"]["skill"]["directive_min_confidence"] == 0.8

    def test_gate_thresholds_clamped(self):
        config = load_jev_config(
            raw={"jev": {"gates": {"skill": {"advisory_min_confidence": 9, "directive_min_confidence": -1}}}}
        )
        assert config["gates"]["skill"]["advisory_min_confidence"] == 1.0
        assert config["gates"]["skill"]["directive_min_confidence"] == 0.0

    def test_factory_disabled(self):
        assert skill_gate_from_config(raw={}) is None


class TestBuildSkillSuggestion:
    def _raw(self, choice="feishu-personal", confidence=0.9):
        return {
            "jev": {
                "enabled": True,
                "mode": "mock",
                "gates": {"skill": {"enabled": True}},
                "mock_answers": {"skill": {"choice": choice, "confidence": confidence}},
            }
        }

    def test_disabled_returns_empty(self):
        assert build_skill_suggestion(root=None, message="x", raw={}) == ""

    def test_directive_text_mentions_skip_detail(self):
        with patch("backend.skills.list_skill_catalog", return_value=[
            SimpleNamespace(name="feishu-personal", description="Feishu ops"),
            SimpleNamespace(name="frontend-design", description="Design"),
        ]):
            text = build_skill_suggestion(root="unused", message="read my feishu docs", raw=self._raw(confidence=0.9))
        assert "feishu-personal" in text
        assert "skill detail" in text.lower() or "read_skill_detail" in text
        assert "skip" in text.lower()

    def test_advisory_text_suggests_reading_detail(self):
        with patch("backend.skills.list_skill_catalog", return_value=[
            SimpleNamespace(name="feishu-personal", description="Feishu ops"),
        ]):
            text = build_skill_suggestion(root="unused", message="x", raw=self._raw(confidence=0.7))
        assert "feishu-personal" in text
        assert "read" in text.lower()

    def test_low_confidence_empty(self):
        with patch("backend.skills.list_skill_catalog", return_value=[
            SimpleNamespace(name="feishu-personal", description="Feishu ops"),
        ]):
            assert build_skill_suggestion(root="unused", message="x", raw=self._raw(confidence=0.4)) == ""

    def test_no_skill_answer_empty(self):
        with patch("backend.skills.list_skill_catalog", return_value=[
            SimpleNamespace(name="feishu-personal", description="Feishu ops"),
        ]):
            text = build_skill_suggestion(
                root="unused", message="x", raw=self._raw(choice="no_skill", confidence=0.95)
            )
        assert text == ""

    def test_failure_returns_empty(self):
        with patch("backend.skills.list_skill_catalog", side_effect=RuntimeError("boom")):
            assert build_skill_suggestion(root="unused", message="x", raw=self._raw()) == ""


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
        "backend.jev_client.build_skill_suggestion", return_value="Skill directive: use feishu-personal"
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
    assert "Skill directive: use feishu-personal" in prompt_text
