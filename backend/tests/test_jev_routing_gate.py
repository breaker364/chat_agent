"""TDD tests for Jev routing and evidence-assessment gates (group 4)."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from backend.agentic_research.models import EvidenceAssessment, EvidenceObservation, ResearchPlan
from backend.config import load_jev_config
from backend.jev_client import (
    JevAnswer,
    JevClient,
    JevEvidenceGate,
    JevResponse,
    JevRouteGate,
    evidence_gate_from_config,
    route_gate_from_config,
)
from backend.workflow_policy import enforce_route
from backend.workflow_runtime import ModelRouteAdapter


def _mock_route_client(mode="direct", mode_conf=0.9, noul=0.05, risk="low", risk_conf=0.9):
    from backend.config import load_jev_config

    return JevClient(
        load_jev_config(
            raw={
                "jev": {
                    "enabled": True,
                    "mode": "mock",
                    "mock_answers": {
                        "mode": {"choice": mode, "confidence": mode_conf},
                        "needs_research": {"noul": noul},
                        "risk_level": {"choice": risk, "confidence": risk_conf},
                    },
                }
            }
        )
    )


class TestJevRouteGate:
    def test_high_confidence_proposal_is_policy_compatible(self):
        gate = JevRouteGate(_mock_route_client(), min_confidence=0.6)
        proposal = gate.propose("please summarize this file")
        assert proposal is not None
        assert proposal["mode"] == "direct"
        assert proposal["task_kinds"] == ["execute"]
        assert proposal["risk_level"] == "low"
        assert 0.0 <= proposal["confidence"] <= 1.0
        decision = enforce_route(
            proposal,
            request="please summarize this file",
            capabilities=frozenset(),
        )
        assert decision.mode == "direct"

    def test_low_confidence_returns_none(self):
        gate = JevRouteGate(_mock_route_client(mode_conf=0.3), min_confidence=0.6)
        assert gate.propose("ambiguous request") is None

    def test_client_failure_returns_none(self):
        class Broken:
            def ask(self, state, questions):
                raise RuntimeError("down")

        assert JevRouteGate(Broken()).propose("anything") is None

    def test_confident_negative_noul_keeps_confidence(self):
        gate = JevRouteGate(_mock_route_client(noul=0.05), min_confidence=0.6)
        proposal = gate.propose("task")
        assert proposal is not None
        assert proposal["needs_research"] is False
        assert proposal["confidence"] >= 0.6


class _FakeModel:
    def __init__(self, payload=None):
        self.calls = 0
        self.payload = payload or {
            "mode": "planned",
            "task_kinds": ["execute"],
            "needs_research": False,
            "risk_level": "low",
            "confidence": 0.8,
            "required_capabilities": [],
        }

    def invoke(self, messages):
        self.calls += 1
        return SimpleNamespace(content=json.dumps(self.payload))


class TestModelRouteAdapter:
    def test_gate_adoption_skips_model(self):
        model = _FakeModel()
        class Gate:
            def propose(self, request):
                return {
                    "mode": "direct", "task_kinds": ["execute"], "needs_research": False,
                    "risk_level": "low", "confidence": 0.9, "required_capabilities": [],
                }
        adapter = ModelRouteAdapter(model, jev_gate=Gate())
        proposal = adapter.invoke([{"content": "hello"}])
        assert proposal["mode"] == "direct"
        assert model.calls == 0

    def test_gate_none_falls_back_to_model(self):
        model = _FakeModel()
        class Gate:
            def propose(self, request):
                return None
        adapter = ModelRouteAdapter(model, jev_gate=Gate())
        proposal = adapter.invoke([{"content": "hello"}])
        assert proposal["mode"] == "planned"
        assert model.calls == 1

    def test_no_gate_uses_model(self):
        model = _FakeModel()
        adapter = ModelRouteAdapter(model)
        proposal = adapter.invoke([{"content": "hello"}])
        assert proposal["mode"] == "planned"
        assert model.calls == 1


def _plan():
    return ResearchPlan(
        requires_evidence=True,
        source_sequence=["web"],
        query_or_scope="q",
        freshness_need="any",
        success_criteria=["cited"],
        rationale_category="factual",
    )


class TestJevEvidenceGate:
    def test_high_confidence_assessment(self):
        config = {
            "jev": {
                "enabled": True,
                "mode": "mock",
                "mock_answers": {"outcome": {"choice": "answer_ready", "confidence": 0.9}},
            }
        }
        gate = JevEvidenceGate(JevClient(load_jev_config(raw=config)), min_confidence=0.6)
        assessment = gate.assess("question", _plan(), [], next_source_available=False)
        assert isinstance(assessment, EvidenceAssessment)
        assert assessment.outcome == "answer_ready"
        assert assessment.usable is True

    def test_low_confidence_returns_none(self):
        config = {
            "jev": {
                "enabled": True,
                "mode": "mock",
                "mock_answers": {"outcome": {"choice": "evidence_gap", "confidence": 0.2}},
            }
        }
        gate = JevEvidenceGate(JevClient(load_jev_config(raw=config)), min_confidence=0.6)
        assert gate.assess("question", _plan(), [], next_source_available=False) is None

    def test_invalid_outcome_returns_none(self):
        config = {
            "jev": {
                "enabled": True,
                "mode": "mock",
                "mock_answers": {"outcome": {"choice": "made_up", "confidence": 0.99}},
            }
        }
        gate = JevEvidenceGate(JevClient(load_jev_config(raw=config)), min_confidence=0.6)
        assert gate.assess("question", _plan(), [], next_source_available=False) is None


class TestModelEvidenceAssessorGate:
    def _assess(self, assessor):
        return assessor.assess("question", _plan(), [], next_source_available=False)

    def test_gate_consulted_before_model(self):
        from backend.agentic_research.orchestrator import ModelEvidenceAssessor

        model = _FakeModel()
        class Gate:
            def __init__(self):
                self.calls = 0
            def assess(self, message, plan, observations, next_source_available):
                self.calls += 1
                return EvidenceAssessment("try_next_source", reason_category="jev_assessment")

        gate = Gate()
        assessor = ModelEvidenceAssessor(model, jev_gate=gate)
        decision = self._assess(assessor)
        assert decision.outcome == "try_next_source"
        assert gate.calls == 1
        assert model.calls == 0

    def test_gate_none_falls_back_to_model(self):
        from backend.agentic_research.orchestrator import ModelEvidenceAssessor

        model = _FakeModel(payload={
            "outcome": "answer_ready", "usable": True, "missing_facets": [],
            "next_query": "", "conflict_sources": [], "reason_category": "cited",
        })
        assessor = ModelEvidenceAssessor(model, jev_gate=None)
        decision = self._assess(assessor)
        assert decision.outcome == "answer_ready"
        assert model.calls == 1


def test_gate_factories_disabled_by_default():
    assert route_gate_from_config(raw={}) is None
    assert evidence_gate_from_config(raw={}) is None
