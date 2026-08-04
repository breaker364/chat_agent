from __future__ import annotations

import time
import unittest


class ResearchPolicyTests(unittest.TestCase):
    def test_resolves_new_policy_and_legacy_boolean(self):
        from backend.agentic_research.models import resolve_knowledge_policy

        self.assertEqual(resolve_knowledge_policy(), "auto")
        self.assertEqual(resolve_knowledge_policy(knowledge_policy="required"), "required")
        self.assertEqual(resolve_knowledge_policy(knowledge_policy="disabled"), "disabled")
        self.assertEqual(resolve_knowledge_policy(knowledge_mode=True), "required")
        self.assertEqual(resolve_knowledge_policy(knowledge_mode=False), "auto")

    def test_rejects_conflicting_policy_fields(self):
        from backend.agentic_research.models import ResearchPolicyError, resolve_knowledge_policy

        with self.assertRaises(ResearchPolicyError):
            resolve_knowledge_policy(knowledge_policy="disabled", knowledge_mode=True)


class ResearchPlanTests(unittest.TestCase):
    def test_validates_generic_source_plan(self):
        from backend.agentic_research.models import ResearchPlan

        plan = ResearchPlan.from_dict({
            "requires_evidence": True,
            "source_sequence": ["personal_knowledge", "web"],
            "query_or_scope": "project retrieval behavior",
            "freshness_need": "stable",
            "success_criteria": ["retrieval source", "citation"],
        })
        self.assertEqual(plan.source_sequence, ("personal_knowledge", "web"))

    def test_rejects_arbitrary_or_unbounded_plan(self):
        from backend.agentic_research.models import ResearchPlanError, ResearchPlan

        with self.assertRaises(ResearchPlanError):
            ResearchPlan.from_dict({"requires_evidence": True, "source_sequence": ["run_shell"]})
        with self.assertRaises(ResearchPlanError):
            ResearchPlan.from_dict({"requires_evidence": True, "source_sequence": []})
        with self.assertRaises(ResearchPlanError):
            ResearchPlan.from_dict({
                "requires_evidence": True,
                "source_sequence": ["web"],
                "query_or_scope": "x" * 5000,
            })


class ResearchTraceAndBudgetTests(unittest.TestCase):
    def test_trace_excludes_raw_evidence_and_sensitive_values(self):
        from backend.agentic_research.models import EvidenceObservation, ResearchTrace

        trace = ResearchTrace(policy="auto")
        trace.record(EvidenceObservation(
            source_kind="web",
            status="ok",
            citations=[{"url": "https://example.invalid/a", "title": "A"}],
            excerpts=["raw document body must not be persisted"],
            raw_payload={"cookie": "secret-cookie", "embedding": [1.0, 2.0]},
        ))
        payload = trace.to_dict()
        serialized = str(payload)
        self.assertNotIn("raw document body", serialized)
        self.assertNotIn("secret-cookie", serialized)
        self.assertNotIn("embedding", serialized)
        self.assertEqual(payload["citation_counts"], {"web": 1})

    def test_budget_is_atomic_and_terminal(self):
        from backend.agentic_research.models import ResearchBudget, ResearchTrace

        now = [100.0]
        budget = ResearchBudget(
            max_route_transitions=1,
            max_source_calls={"web": 1},
            deadline_seconds=5.0,
            clock=lambda: now[0],
        )
        self.assertTrue(budget.consume("web"))
        self.assertFalse(budget.consume("web"))
        self.assertFalse(budget.consume("workspace"))
        trace = ResearchTrace(policy="auto")
        trace.finish("evidence_gap")
        self.assertFalse(trace.can_continue)
        self.assertFalse(budget.consume("web"))
        now[0] += 6
        self.assertFalse(budget.can_call("web"))


if __name__ == "__main__":
    unittest.main()
