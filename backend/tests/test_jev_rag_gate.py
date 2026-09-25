"""TDD tests for the Jev RAG passage gate (group 3)."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from pathlib import Path
from unittest import TestCase
from uuid import uuid4

import pytest

from backend.jev_client import JevAnswer, JevResponse, JevRagGate, rag_gate_from_config


@dataclass
class FakeChunk:
    chunk_id: str
    text: str
    doc_id: str = "doc"
    ordinal: int = 0


class StubClient:
    """Returns scripted (relevance, contradiction, injection) per passage text."""

    def __init__(self, verdicts: dict[str, tuple[float, float, float]], fail: bool = False):
        self.verdicts = verdicts
        self.fail = fail
        self.calls = 0

    def ask(self, state, questions):
        self.calls += 1
        if self.fail:
            raise RuntimeError("endpoint down")
        relevance, contradiction, injection = self.verdicts[state["passage"]]
        answers = {
            "is_relevant": {"type": "noul", "noul": relevance},
            "contradicts_premise": {"type": "noul", "noul": contradiction},
            "contains_prompt_injection": {"type": "noul", "noul": injection},
        }
        return JevResponse(
            answers={
                name: JevAnswer(name=name, kind="noul", value=payload["noul"], confidence=payload["noul"])
                for name, payload in answers.items()
            },
            model="stub",
            elapsed_seconds=0.0,
        )


def _fused(*texts: str):
    return [(FakeChunk(chunk_id=f"id-{text[:12]}", text=text), 1.0) for text in texts]


GOOD = "alpha passage with the answer"
CONTRA = "beta passage contradicting the premise"
INJECT = "gamma passage with hidden instructions"
WEAK = "delta passage barely related"
EXTRA = "epsilon passage beyond the judged head"


def _gate(client, **overrides):
    config = {"max_passages": 4, "min_relevance": 0.45, "min_contradiction": 0.7, "min_injection": 0.7}
    config.update(overrides)
    return JevRagGate(client, **config)


class JevRagGateUnitTests(TestCase):
    def test_low_relevance_dropped(self):
        client = StubClient({GOOD: (0.9, 0.0, 0.0), WEAK: (0.2, 0.0, 0.0)})
        kept, conflicts = _gate(client).apply("query", _fused(WEAK, GOOD))
        assert [chunk.text for chunk, _ in kept] == [GOOD]
        assert conflicts == []

    def test_contradiction_moves_to_conflicts(self):
        client = StubClient({CONTRA: (0.9, 0.9, 0.0), GOOD: (0.9, 0.0, 0.0)})
        kept, conflicts = _gate(client).apply("query", _fused(GOOD, CONTRA))
        assert [chunk.text for chunk, _ in kept] == [GOOD]
        assert [chunk.text for chunk, _ in conflicts] == [CONTRA]

    def test_injection_dropped_everywhere(self):
        client = StubClient({INJECT: (0.9, 0.0, 0.9), GOOD: (0.9, 0.0, 0.0)})
        kept, conflicts = _gate(client).apply("query", _fused(GOOD, INJECT))
        assert [chunk.text for chunk, _ in kept] == [GOOD]
        assert conflicts == []

    def test_tail_beyond_head_passes_unjudged(self):
        client = StubClient({GOOD: (0.9, 0.0, 0.0), CONTRA: (0.9, 0.9, 0.0)})
        fused = _fused(GOOD, CONTRA, EXTRA)
        kept, conflicts = _gate(client, max_passages=2).apply("query", fused)
        assert [chunk.text for chunk, _ in kept] == [GOOD, EXTRA]
        assert [chunk.text for chunk, _ in conflicts] == [CONTRA]
        assert client.calls == 2

    def test_failure_keeps_everything(self):
        client = StubClient({}, fail=True)
        fused = _fused(WEAK, INJECT)
        kept, conflicts = _gate(client).apply("query", fused)
        assert [chunk.text for chunk, _ in kept] == [WEAK, INJECT]
        assert conflicts == []

    def test_cache_prevents_repeat_calls(self):
        client = StubClient({GOOD: (0.9, 0.0, 0.0)})
        gate = _gate(client)
        gate.apply("same query", _fused(GOOD))
        gate.apply("same   QUERY", _fused(GOOD))
        assert client.calls == 1

    def test_cache_bounded(self):
        client = StubClient({GOOD: (0.9, 0.0, 0.0)})
        gate = _gate(client, cache_max_entries=2)
        for index in range(5):
            gate.apply(f"query {index}", _fused(GOOD))
        assert len(gate._cache) <= 2


def _make_kb(workspace: Path, store: Path):
    from backend.rag.service import PersonalKnowledgeBase

    kb = PersonalKnowledgeBase(workspace_root=workspace, store_path=store, jev_gate=None)
    source = workspace / "notes.md"
    source.write_text("# A\nalpha beta gamma delta epsilon\n\n# B\nsecond topic body text", encoding="utf-8")
    kb.index_files("notes", [source])
    return kb


class ScriptedGate:
    """Integration stub: sends one chunk to conflicts, drops the other."""

    enabled = True

    def __init__(self, mode: str = "conflict"):
        self.mode = mode

    def apply(self, query, fused):
        if self.mode == "conflict":
            return [], list(fused)
        if self.mode == "drop":
            return [], []
        if self.mode == "fail":
            raise RuntimeError("gate down")
        return list(fused), []


class JevRagGateIntegrationTests(TestCase):
    def setUp(self):
        temp_root = Path.cwd() / "tmp" / "unittest"
        temp_root.mkdir(parents=True, exist_ok=True)
        self.temp_dir = temp_root / f"jev-rag-{uuid4().hex}"
        self.temp_dir.mkdir(parents=False, exist_ok=False)
        self.workspace = self.temp_dir / "workspace"
        self.store_dir = self.temp_dir / "store"
        self.workspace.mkdir()
        self.store_dir.mkdir()

    def tearDown(self):
        import shutil

        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_conflicts_reported_separately(self):
        kb = _make_kb(self.workspace, self.store_dir)
        kb.jev_gate = ScriptedGate("conflict")
        result = kb.search("alpha beta")
        assert isinstance(result.get("conflicts"), list)
        assert len(result["conflicts"]) >= 1
        assert all(item.get("conflict") is True for item in result["conflicts"])

    def test_gate_drop_removes_all_results(self):
        kb = _make_kb(self.workspace, self.store_dir)
        kb.jev_gate = ScriptedGate("drop")
        result = kb.search("alpha beta")
        assert result["results"] == []

    def test_gate_failure_passes_through(self):
        kb = _make_kb(self.workspace, self.store_dir)
        kb.jev_gate = ScriptedGate("fail")
        result = kb.search("alpha beta")
        assert len(result["results"]) >= 1

    def test_no_gate_unchanged(self):
        kb = _make_kb(self.workspace, self.store_dir)
        baseline = kb.search("alpha beta")
        kb.jev_gate = None
        again = kb.search("alpha beta")
        assert [r["chunk_id"] for r in baseline["results"]] == [r["chunk_id"] for r in again["results"]]


def test_rag_gate_from_config_disabled():
    assert rag_gate_from_config(raw={}) is None
