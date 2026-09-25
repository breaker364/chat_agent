"""TDD tests for the Jev memory-write prefilter (group 2) and read filter (group 6)."""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

import pytest

from backend.config import load_jev_config
from backend.jev_client import JevClient, JevMemoryWriteGate, JevQuestion
from backend.memory import (
    AgentMemoryStore,
    MemoryExtractionResult,
    MemoryExtractionScheduler,
    MemoryPrefilterDecision,
)


class RecordingExtractor:
    def __init__(self):
        self.calls = []

    async def extract(self, *, messages, manifest):
        self.calls.append(list(messages))
        return MemoryExtractionResult(candidates=[])


def _make_store(tmp_path):
    settings = {
        "enabled": True,
        "directory": str(tmp_path / "agent_memory"),
        "max_index_lines": 10,
        "max_index_bytes": 4096,
    }
    return AgentMemoryStore(settings["directory"], settings)


def _history():
    return [
        {"role": "user", "content": "remember that I prefer concise answers"},
        {"role": "assistant", "content": "Noted."},
    ]


def _run_scheduler(store, extractor, *, prefilter=None, history=None):
    activities: list[tuple[str, dict]] = []

    def on_activity(session_id, payload):
        activities.append((session_id, payload))

    scheduler = MemoryExtractionScheduler(
        store,
        extractor,
        recent_message_limit=4,
        prefilter=prefilter,
        on_activity=on_activity,
    )

    async def run():
        scheduler.schedule("session-1", history or _history())
        await scheduler.wait_until_idle()

    asyncio.run(run())
    return activities


@pytest.mark.parametrize(
    "decision,expect_extract",
    [
        (MemoryPrefilterDecision(skip=True, probability=0.2, source="jev"), False),
        (MemoryPrefilterDecision(skip=False, probability=0.9, source="jev"), True),
        (MemoryPrefilterDecision(skip=False, probability=0.0, source="fallback"), True),
    ],
)
def test_prefilter_decision_controls_extraction(tmp_path, decision, expect_extract):
    store = _make_store(tmp_path)
    extractor = RecordingExtractor()

    async def prefilter(messages):
        return decision

    activities = _run_scheduler(store, extractor, prefilter=prefilter)

    assert bool(extractor.calls) is expect_extract
    if decision.skip and not expect_extract:
        stages = [payload.get("stage") for _, payload in activities]
        assert "memory_extraction_prefiltered" in stages
        payload = next(p for _, p in activities if p.get("stage") == "memory_extraction_prefiltered")
        assert payload["probability"] == pytest.approx(0.2)
        assert payload["source"] == "jev"


def test_prefilter_failure_still_extracts(tmp_path):
    store = _make_store(tmp_path)
    extractor = RecordingExtractor()

    async def prefilter(messages):
        raise RuntimeError("gate exploded")

    _run_scheduler(store, extractor, prefilter=prefilter)
    assert len(extractor.calls) == 1


def test_no_prefilter_extracts(tmp_path):
    store = _make_store(tmp_path)
    extractor = RecordingExtractor()
    _run_scheduler(store, extractor, prefilter=None)
    assert len(extractor.calls) == 1


def _mock_gate_client(probability):
    config = load_jev_config(
        raw={
            "jev": {
                "enabled": True,
                "mode": "mock",
                "mock_answers": {"worth_long_term": {"noul": probability}},
            }
        }
    )
    return JevClient(config)


def test_memory_write_gate_skips_low_probability():
    gate = JevMemoryWriteGate(_mock_gate_client(0.2), min_probability=0.5)
    decision = asyncio.run(gate([{"role": "user", "content": "hello"}]))
    assert decision.skip is True
    assert decision.source == "jev"


def test_memory_write_gate_passes_high_probability():
    gate = JevMemoryWriteGate(_mock_gate_client(0.9), min_probability=0.5)
    decision = asyncio.run(gate([{"role": "user", "content": "hello"}]))
    assert decision.skip is False


def test_memory_write_gate_failure_falls_back_to_extract():
    class BrokenClient:
        def ask(self, state, questions):
            raise RuntimeError("down")

    gate = JevMemoryWriteGate(BrokenClient(), min_probability=0.5)
    decision = asyncio.run(gate([{"role": "user", "content": "hello"}]))
    assert decision.skip is False
    assert decision.source == "fallback"


def test_memory_write_gate_from_config_disabled():
    from backend.jev_client import memory_write_gate_from_config

    assert memory_write_gate_from_config(raw={}) is None
