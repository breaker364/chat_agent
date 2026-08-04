from __future__ import annotations

import unittest


class FakeTool:
    def __init__(self, name: str):
        self.name = name

    def invoke(self, _payload):
        return '{"results": []}'


class FakeModel:
    def invoke(self, _messages):
        return {
            "requires_evidence": False,
            "source_sequence": ["direct"],
            "query_or_scope": "",
            "freshness_need": "none",
            "success_criteria": [],
        }


class AgenticResearchRuntimeTests(unittest.TestCase):
    def test_binds_only_read_only_evidence_tools_and_filters_react_tools(self):
        from backend.agent import _react_tools_for_runtime
        from backend.agentic_research.config import AgenticResearchConfig
        from backend.agentic_research.runtime import EVIDENCE_TOOL_NAMES, build_agentic_research_runtime

        tools = [
            FakeTool("knowledge_search"),
            FakeTool("list_directory"),
            FakeTool("get_file_info"),
            FakeTool("read_file"),
            FakeTool("web_search"),
            FakeTool("web_fetch"),
            FakeTool("knowledge_import_files"),
            FakeTool("write_file"),
        ]
        runtime = build_agentic_research_runtime(
            model=FakeModel(),
            tools=tools,
            workspace_root=".",
            config=AgenticResearchConfig(enabled=True),
        )

        self.assertIsNotNone(runtime)
        self.assertEqual(
            set(runtime.orchestrator.registry.source_kinds),
            {"personal_knowledge", "workspace", "web"},
        )
        self.assertNotIn("knowledge_import_files", EVIDENCE_TOOL_NAMES)
        self.assertNotIn("write_file", EVIDENCE_TOOL_NAMES)
        workspace_adapter = runtime.orchestrator.registry.get("workspace")
        self.assertIs(workspace_adapter.list_directory, tools[1])
        self.assertIs(workspace_adapter.get_file_info, tools[2])
        self.assertEqual(
            [tool.name for tool in _react_tools_for_runtime(tools, runtime)],
            ["knowledge_import_files", "write_file"],
        )

    def test_builds_source_labeled_context_only_for_usable_evidence(self):
        from backend.agentic_research.models import EvidenceAssessment, EvidenceObservation, ResearchTrace
        from backend.agentic_research.orchestrator import ResearchResult
        from backend.agentic_research.runtime import build_synthesis_context

        trace = ResearchTrace(policy="auto")
        trace.record(EvidenceObservation(
            source_kind="web",
            citations=[{"id": "result", "title": "Result", "uri": "https://example.invalid"}],
            excerpts=["bounded excerpt"],
        ))
        trace.finish("answer_ready")
        context = build_synthesis_context(ResearchResult(
            trace=trace,
            observations=[EvidenceObservation(
                source_kind="web",
                citations=[{"id": "result", "title": "Result", "uri": "https://example.invalid"}],
                excerpts=["bounded excerpt"],
            )],
            assessment=EvidenceAssessment("answer_ready", usable=True),
        ))

        self.assertIn('"source_kind": "web"', context)
        self.assertIn("bounded excerpt", context)
        self.assertNotIn("raw_payload", context)


if __name__ == "__main__":
    unittest.main()
