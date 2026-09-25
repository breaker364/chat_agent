"""TDD tests for Jev compaction gray zone and memory read filter (group 6)."""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from backend.jev_client import (
    GateVerdict,
    JevClient,
    JevCompactionGate,
    JevMemoryReadGate,
    compaction_gate_from_config,
    memory_read_gate_from_config,
)
from backend.config import load_jev_config
from backend.agent import _compact_history_if_needed
from backend.memory import MemoryContextProvider

from backend.tests.test_context_compaction import _FakeLlm, _MemoryCompactionStore, _summary


def _history():
    spelled = [
        "one", "two", "three", "four", "five", "six", "seven", "eight",
    ]
    items = []
    for word in spelled:
        items.append({"role": "user", "content": f"request {word}"})
        items.append({"role": "assistant", "content": f"answer {word}"})
    return items


class StubCompactionGate:
    def __init__(self, *, probability=0.2, adopted=True, fail=False):
        self.probability = probability
        self.adopted = adopted
        self.fail = fail
        self.calls = 0

    async def holds(self, recent_text):
        self.calls += 1
        if self.fail:
            raise RuntimeError("gate down")
        return GateVerdict(adopted=self.adopted, probability=self.probability)


class MustNotCallGate:
    async def holds(self, recent_text):
        raise AssertionError("gate must not be consulted")


def _run_compact(estimate, *, gate=None, gray_zone=25_000):
    llm = _FakeLlm([_summary([])])

    async def run():
        with patch("backend.agent.SessionStore", return_value=_MemoryCompactionStore()), patch(
            "backend.agent.create_chat_deepseek", return_value=llm
        ):
            return await _compact_history_if_needed(
                _history(),
                session_id="jev-gray-session",
                model_config={"model": "test-model"},
                model_context_window=100_000,
                context_token_estimate=estimate,
                settings={
                    "enabled": True,
                    "trigger_remaining_tokens": 20_000,
                    "retain_recent_turns": 3,
                    "summary_max_output_tokens": 500,
                    "chunk_target_tokens": 500,
                    "max_retries": 0,
                },
                jev_compaction_gate=gate,
                jev_gray_zone_tokens=gray_zone,
            )

    return asyncio.run(run())


class CompactionGrayZoneTests(unittest.TestCase):
    def test_gray_zone_gate_clear_compacts_early(self):
        gate = StubCompactionGate(probability=0.2)
        history, details = _run_compact(70_000, gate=gate)
        assert gate.calls == 1
        assert details["triggered"] is True
        assert details["skip_reason"] == "gray_zone_compact"
        assert history[0]["role"] == "context_summary"

    def test_gray_zone_gate_unfinished_holds(self):
        gate = StubCompactionGate(probability=0.8)
        history, details = _run_compact(70_000, gate=gate)
        assert details["triggered"] is False
        assert details["skip_reason"] == "gray_zone_hold"
        assert len(history) == len(_history())

    def test_gray_zone_gate_failure_holds(self):
        gate = StubCompactionGate(fail=True)
        history, details = _run_compact(70_000, gate=gate)
        assert details["triggered"] is False
        assert details["skip_reason"] == "gray_zone_hold"

    def test_outside_gray_zone_skips_without_gate(self):
        gate = StubCompactionGate(fail=True)
        history, details = _run_compact(40_000, gate=gate)
        assert details["skip_reason"] == "above_threshold"
        assert gate.calls == 0

    def test_hard_threshold_compacts_without_consulting_gate(self):
        gate = MustNotCallGate()
        history, details = _run_compact(90_000, gate=gate)
        assert details["triggered"] is True
        assert history[0]["role"] == "context_summary"

    def test_no_gate_keeps_legacy_behavior(self):
        history, details = _run_compact(70_000, gate=None)
        assert details["skip_reason"] == "above_threshold"


class StubReadGate:
    enabled = True
    filter_threshold = 2

    def __init__(self, keep=1, fail=False):
        self.keep = keep
        self.fail = fail
        self.calls = []

    def filter(self, user_message, lines):
        self.calls.append((user_message, list(lines)))
        if self.fail:
            raise RuntimeError("gate down")
        return lines[: self.keep]


class MemoryReadFilterTests(unittest.TestCase):
    def setUp(self):
        from backend.memory import AgentMemoryStore, MemoryCandidate

        self.temp_dir = tempfile.TemporaryDirectory()
        self.settings = {
            "enabled": True,
            "directory": str(Path(self.temp_dir.name) / "agent_memory"),
            "max_index_lines": 20,
            "max_index_bytes": 4096,
        }
        self.store = AgentMemoryStore(self.settings["directory"], self.settings)
        for index in range(1, 4):
            self.store.upsert(
                MemoryCandidate(
                    operation="create",
                    name=f"record {index}",
                    description=f"Summary of durable memory {index}.",
                    memory_type="project",
                    content=f"Full body {index}.",
                )
            )

    def tearDown(self):
        self.temp_dir.cleanup()

    def _provider(self, gate=None):
        from backend.memory import MemoryContextProvider

        return MemoryContextProvider(
            self.store,
            enabled=True,
            max_index_lines=20,
            max_index_bytes=4096,
            jev_read_gate=gate,
        )

    def test_gate_filters_lines_when_over_threshold(self):
        gate = StubReadGate(keep=1)
        context = self._provider(gate).get_context(user_message="current question")
        assert gate.calls, "gate must be consulted"
        assert context.count("](project_") == 1

    def test_gate_not_consulted_under_threshold(self):
        gate = StubReadGate(keep=1)
        provider = MemoryContextProvider(
            self.store,
            enabled=True,
            max_index_lines=1,
            max_index_bytes=4096,
            jev_read_gate=gate,
        )
        provider.get_context(user_message="q")
        assert gate.calls == []

    def test_gate_failure_returns_full_index(self):
        gate = StubReadGate(fail=True)
        context = self._provider(gate).get_context(user_message="q")
        assert context.count("](project_") == 3

    def test_no_user_message_skips_gate(self):
        gate = StubReadGate(keep=1)
        self._provider(gate).get_context()
        assert gate.calls == []


class JevMemoryReadGateUnitTests(unittest.TestCase):
    def _gate(self, probability, fail=False):
        class Client:
            def __init__(self):
                self.fail = fail
                self.probability = probability

            def ask(self, state, questions):
                if self.fail:
                    raise RuntimeError("down")
                from backend.jev_client import JevAnswer, JevResponse, JevQuestion

                return JevResponse(
                    answers={
                        question.name: JevAnswer(
                            name=question.name, kind="noul", value=self.probability, confidence=self.probability
                        )
                        for question in questions
                    },
                    model="stub",
                    elapsed_seconds=0.0,
                )

        return JevMemoryReadGate(Client(), min_probability=0.5, filter_threshold=2)

    def test_relevant_kept_irrelevant_dropped(self):
        gate = JevMemoryReadGate(
            _FixedClient([0.9, 0.1]), min_probability=0.5, filter_threshold=2
        )
        kept = gate.filter("current question", ["line one", "line two"])
        assert kept == ["line one"]

    def test_failure_keeps_line(self):
        class Boom:
            def ask(self, state, questions):
                raise RuntimeError("down")

        gate = JevMemoryReadGate(Boom(), min_probability=0.5, filter_threshold=2)
        assert gate.filter("q", ["line one"]) == ["line one"]

    def test_factory_disabled(self):
        assert memory_read_gate_from_config(raw={}) is None
        assert compaction_gate_from_config(raw={}) is None


class _FixedClient:
    def __init__(self, probabilities):
        self.probabilities = list(probabilities)

    def ask(self, state, questions):
        from backend.jev_client import JevAnswer, JevResponse

        probability = self.probabilities.pop(0) if self.probabilities else 0.0
        return JevResponse(
            answers={
                question.name: JevAnswer(
                    name=question.name, kind="noul", value=probability, confidence=probability
                )
                for question in questions
            },
            model="stub",
            elapsed_seconds=0.0,
        )


class JevCompactionGateUnitTests(unittest.TestCase):
    def test_holds_probability_passthrough(self):
        client = _FixedClient([0.8])
        gate = JevCompactionGate(client, min_probability=0.5)
        verdict = asyncio.run(gate.holds("recent text"))
        assert verdict.adopted is True
        assert verdict.probability == 0.8

    def test_failure_returns_fallback(self):
        class Boom:
            def ask(self, state, questions):
                raise RuntimeError("down")

        gate = JevCompactionGate(Boom(), min_probability=0.5)
        verdict = asyncio.run(gate.holds("recent text"))
        assert verdict.adopted is False
