from __future__ import annotations

import unittest


class _ResearchRuntime:
    def __init__(self, result):
        self.result = result
        self.calls = []

    def run(self, request, *, knowledge_policy=None):
        self.calls.append((request, knowledge_policy))
        return self.result


def _result(outcome: str):
    from backend.agentic_research.models import EvidenceAssessment, EvidenceObservation, ResearchTrace
    from backend.agentic_research.orchestrator import ResearchResult

    trace = ResearchTrace(policy="auto")
    first = EvidenceObservation(
        source_kind="web",
        citations=[{"id": "source-1", "title": "Source", "uri": "https://example.invalid/a"}],
        excerpts=["evidence"],
    )
    duplicate = EvidenceObservation(
        source_kind="web",
        citations=[{"id": "source-1", "title": "Source", "uri": "https://example.invalid/a"}],
        excerpts=["evidence"],
    )
    trace.record(first)
    trace.record(duplicate)
    trace.finish(outcome)
    return ResearchResult(
        trace=trace,
        observations=[first, duplicate],
        assessment=EvidenceAssessment(
            "answer_ready" if outcome == "answer_ready" else "evidence_gap",
            usable=outcome == "answer_ready",
            missing_facets=("missing input",) if outcome != "answer_ready" else (),
            reason_category="budget_exhausted" if outcome != "answer_ready" else "",
        ),
    )


class ResearchSubgraphTests(unittest.TestCase):
    def test_research_subgraph_deduplicates_evidence_in_completed_result(self):
        from backend.research_graph import build_research_graph

        runtime = _ResearchRuntime(_result("answer_ready"))
        graph = build_research_graph(runtime)

        result = graph.invoke({"request": "find evidence", "knowledge_policy": "auto", "task": {"task_id": "research-1"}})

        self.assertEqual(result["status"], "completed")
        self.assertEqual(len(result["evidence"]), 1)
        self.assertEqual(result["evidence"][0]["id"], "source-1")
        self.assertEqual(runtime.calls, [("find evidence", "auto")])

    def test_research_subgraph_returns_partial_result_for_evidence_gap(self):
        from backend.research_graph import build_research_graph

        graph = build_research_graph(_ResearchRuntime(_result("evidence_gap")))

        result = graph.invoke({"request": "find evidence", "knowledge_policy": "auto", "task": {"task_id": "research-1"}})

        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["gaps"], ["missing input"])
        self.assertEqual(result["reason"], "budget_exhausted")


if __name__ == "__main__":
    unittest.main()
