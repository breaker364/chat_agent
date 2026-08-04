from __future__ import annotations

import unittest


class FakeAdapter:
    def __init__(self, source_kind, observations):
        self.source_kind = source_kind
        self.observations = list(observations)
        self.calls = []

    def collect(self, query):
        from backend.agentic_research.models import EvidenceObservation

        self.calls.append(query)
        value = self.observations[min(len(self.calls) - 1, len(self.observations) - 1)]
        return value if isinstance(value, EvidenceObservation) else EvidenceObservation(
            source_kind=self.source_kind,
            status=value,
            citations=[] if value != "ok" else [{"id": "citation", "title": "Source", "uri": "source://1"}],
            excerpts=[] if value != "ok" else ["evidence"],
        )


class ScriptedPlanner:
    def __init__(self, plan):
        self.plan_value = plan
        self.calls = []

    def plan(self, message, policy):
        self.calls.append((message, policy))
        return self.plan_value


class ScriptedAssessor:
    def __init__(self, assessments):
        self.assessments = list(assessments)
        self.calls = []

    def assess(self, message, plan, observations, next_source_available):
        self.calls.append((message, plan, observations, next_source_available))
        return self.assessments.pop(0)


class FakeStructuredModel:
    def __init__(self, response):
        self.response = response
        self.messages = []

    def invoke(self, messages):
        self.messages.append(messages)
        return self.response


class AgenticResearchOrchestratorTests(unittest.TestCase):
    def _plan(self, sources, requires=True):
        from backend.agentic_research.models import ResearchPlan

        return ResearchPlan(
            requires_evidence=requires,
            source_sequence=tuple(sources),
            query_or_scope="question",
            success_criteria=("answer",),
        )

    def _observation(self, source, status="ok"):
        from backend.agentic_research.models import EvidenceObservation

        return EvidenceObservation(
            source_kind=source,
            status=status,
            citations=[{"id": f"{source}-1", "title": "Source", "uri": f"{source}://1"}]
            if status == "ok" else [],
            excerpts=["evidence"] if status == "ok" else [],
        )

    def test_direct_plan_does_not_call_evidence(self):
        from backend.agentic_research.adapters import EvidenceRegistry
        from backend.agentic_research.orchestrator import AgenticResearchOrchestrator
        from backend.agentic_research.models import EvidenceAssessment

        adapter = FakeAdapter("web", ["ok"])
        orchestrator = AgenticResearchOrchestrator(
            planner=ScriptedPlanner(self._plan(["direct"], requires=False)),
            assessor=ScriptedAssessor([EvidenceAssessment("answer_ready", usable=True)]),
            registry=EvidenceRegistry({"web": adapter}),
        )
        result = orchestrator.run("say hello", knowledge_policy="auto")
        self.assertEqual(result.trace.outcome, "answer_ready")
        self.assertEqual(adapter.calls, [])

    def test_auto_replans_from_empty_knowledge_to_web(self):
        from backend.agentic_research.adapters import EvidenceRegistry
        from backend.agentic_research.models import EvidenceAssessment
        from backend.agentic_research.orchestrator import AgenticResearchOrchestrator

        knowledge = FakeAdapter("personal_knowledge", ["no_results"])
        web = FakeAdapter("web", ["ok"])
        assessor = ScriptedAssessor([
            EvidenceAssessment("try_next_source", reason_category="no_results"),
            EvidenceAssessment("answer_ready", usable=True),
        ])
        orchestrator = AgenticResearchOrchestrator(
            planner=ScriptedPlanner(self._plan(["personal_knowledge", "web"])),
            assessor=assessor,
            registry=EvidenceRegistry({"personal_knowledge": knowledge, "web": web}),
        )
        result = orchestrator.run("find the answer", knowledge_policy="auto")
        self.assertEqual(knowledge.calls, ["question"])
        self.assertEqual(web.calls, ["question"])
        self.assertEqual(result.trace.sources_attempted, ["personal_knowledge", "web"])
        self.assertEqual(result.trace.outcome, "answer_ready")

    def test_required_does_not_fall_back_to_web(self):
        from backend.agentic_research.adapters import EvidenceRegistry
        from backend.agentic_research.models import EvidenceAssessment
        from backend.agentic_research.orchestrator import AgenticResearchOrchestrator

        knowledge = FakeAdapter("personal_knowledge", ["no_results"])
        web = FakeAdapter("web", ["ok"])
        orchestrator = AgenticResearchOrchestrator(
            planner=ScriptedPlanner(self._plan(["web", "personal_knowledge"])),
            assessor=ScriptedAssessor([EvidenceAssessment("try_next_source")]),
            registry=EvidenceRegistry({"personal_knowledge": knowledge, "web": web}),
        )
        result = orchestrator.run("private answer", knowledge_policy="required")
        self.assertEqual(knowledge.calls, ["question"])
        self.assertEqual(web.calls, [])
        self.assertEqual(result.trace.outcome, "evidence_gap")

    def test_disabled_never_calls_knowledge(self):
        from backend.agentic_research.adapters import EvidenceRegistry
        from backend.agentic_research.models import EvidenceAssessment
        from backend.agentic_research.orchestrator import AgenticResearchOrchestrator

        knowledge = FakeAdapter("personal_knowledge", ["ok"])
        web = FakeAdapter("web", ["ok"])
        orchestrator = AgenticResearchOrchestrator(
            planner=ScriptedPlanner(self._plan(["personal_knowledge", "web"])),
            assessor=ScriptedAssessor([EvidenceAssessment("answer_ready", usable=True)]),
            registry=EvidenceRegistry({"personal_knowledge": knowledge, "web": web}),
        )
        result = orchestrator.run("public answer", knowledge_policy="disabled")
        self.assertEqual(knowledge.calls, [])
        self.assertEqual(web.calls, ["question"])
        self.assertEqual(result.trace.outcome, "answer_ready")

    def test_budget_and_conflict_are_terminal(self):
        from backend.agentic_research.adapters import EvidenceRegistry
        from backend.agentic_research.models import EvidenceAssessment, ResearchBudget
        from backend.agentic_research.orchestrator import AgenticResearchOrchestrator

        web = FakeAdapter("web", ["ok", "ok"])
        orchestrator = AgenticResearchOrchestrator(
            planner=ScriptedPlanner(self._plan(["web"])),
            assessor=ScriptedAssessor([EvidenceAssessment("report_conflict", usable=False)]),
            registry=EvidenceRegistry({"web": web}),
            budget=ResearchBudget(max_route_transitions=1, max_source_calls={"web": 1}),
        )
        result = orchestrator.run("conflicting answer", knowledge_policy="auto")
        self.assertEqual(result.trace.outcome, "report_conflict")
        self.assertEqual(len(web.calls), 1)
        self.assertFalse(result.trace.can_continue)

    def test_required_policy_forces_personal_knowledge_even_for_direct_plan(self):
        from backend.agentic_research.adapters import EvidenceRegistry
        from backend.agentic_research.models import EvidenceAssessment
        from backend.agentic_research.orchestrator import AgenticResearchOrchestrator

        knowledge = FakeAdapter("personal_knowledge", ["ok"])
        orchestrator = AgenticResearchOrchestrator(
            planner=ScriptedPlanner(self._plan(["direct"], requires=False)),
            assessor=ScriptedAssessor([EvidenceAssessment("answer_ready", usable=True)]),
            registry=EvidenceRegistry({"personal_knowledge": knowledge}),
        )

        result = orchestrator.run("private answer", knowledge_policy="required")

        self.assertEqual(knowledge.calls, ["question"])
        self.assertTrue(result.plan.requires_evidence)
        self.assertEqual(result.plan.source_sequence, ("personal_knowledge",))

    def test_refines_a_query_within_the_source_budget(self):
        from backend.agentic_research.adapters import EvidenceRegistry
        from backend.agentic_research.models import EvidenceAssessment, ResearchBudget
        from backend.agentic_research.orchestrator import AgenticResearchOrchestrator

        web = FakeAdapter("web", ["no_results", "ok"])
        orchestrator = AgenticResearchOrchestrator(
            planner=ScriptedPlanner(self._plan(["web"])),
            assessor=ScriptedAssessor([
                EvidenceAssessment("refine_same_source", next_query="refined question"),
                EvidenceAssessment("answer_ready", usable=True),
            ]),
            registry=EvidenceRegistry({"web": web}),
            budget=ResearchBudget(max_route_transitions=2, max_source_calls={"web": 2}),
        )

        result = orchestrator.run("original question", knowledge_policy="auto")

        self.assertEqual(web.calls, ["question", "refined question"])
        self.assertEqual(result.trace.outcome, "answer_ready")


class PlannerAndAssessorTests(unittest.TestCase):
    def test_model_planner_accepts_each_generic_source_route(self):
        from backend.agentic_research.orchestrator import ModelResearchPlanner

        routes = [
            (False, ["direct"]),
            (True, ["personal_knowledge"]),
            (True, ["workspace"]),
            (True, ["web"]),
            (True, ["personal_knowledge", "web"]),
        ]
        for requires_evidence, source_sequence in routes:
            model = FakeStructuredModel({
                "requires_evidence": requires_evidence,
                "source_sequence": source_sequence,
                "query_or_scope": "generic request",
                "freshness_need": "current" if "web" in source_sequence else "stable",
                "success_criteria": ["answer"],
            })
            plan = ModelResearchPlanner(model).plan("question", "auto")
            self.assertEqual(plan.source_sequence, tuple(source_sequence))
            self.assertTrue(model.messages)

    def test_default_assessor_handles_insufficiency_freshness_and_conflicts(self):
        from backend.agentic_research.models import EvidenceObservation, ResearchPlan
        from backend.agentic_research.orchestrator import DefaultEvidenceAssessor

        assessor = DefaultEvidenceAssessor()
        plan = ResearchPlan(
            requires_evidence=True,
            source_sequence=("web", "workspace"),
            query_or_scope="question",
            freshness_need="current",
        )
        no_results = EvidenceObservation(source_kind="web", status="no_results")
        self.assertEqual(
            assessor.assess("question", plan, [no_results], True).outcome,
            "try_next_source",
        )
        stale = EvidenceObservation(
            source_kind="web",
            status="ok",
            freshness="stale",
            citations=[{"id": "web-1", "title": "Result", "uri": "https://example.invalid"}],
            excerpts=["outdated evidence"],
        )
        self.assertEqual(
            assessor.assess("question", plan, [stale], False).outcome,
            "evidence_gap",
        )
        conflict = EvidenceObservation(
            source_kind="web",
            status="ok",
            metadata={"conflict": True, "conflict_sources": ["web", "workspace"]},
        )
        self.assertEqual(
            assessor.assess("question", plan, [conflict], False).outcome,
            "report_conflict",
        )


if __name__ == "__main__":
    unittest.main()
