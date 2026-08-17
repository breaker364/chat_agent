from __future__ import annotations

import json
import unittest
from pathlib import Path
from unittest.mock import patch


class _FakeTool:
    def __init__(self, name: str):
        self.name = name


class _PlannerModel:
    def __init__(self, response=None, error: Exception | None = None):
        self.response = response or {
            "requires_evidence": True,
            "source_sequence": ["personal_knowledge", "web"],
            "query_or_scope": "bounded question",
            "freshness_need": "stable",
            "success_criteria": ["answer with citations"],
        }
        self.error = error
        self.calls = []

    def invoke(self, messages):
        self.calls.append(messages)
        if self.error:
            raise self.error
        return self.response


class AgentOwnedResearchRoutingTests(unittest.TestCase):
    def test_enabled_runtime_keeps_registered_evidence_tools_visible(self):
        from backend.agent import _react_tools_for_runtime
        from backend.agentic_research.config import AgenticResearchConfig
        from backend.agentic_research.runtime import build_agentic_research_runtime

        tools = [
            _FakeTool("knowledge_search"),
            _FakeTool("read_file"),
            _FakeTool("list_directory"),
            _FakeTool("get_file_info"),
            _FakeTool("web_search"),
            _FakeTool("web_fetch"),
            _FakeTool("write_file"),
        ]
        runtime = build_agentic_research_runtime(
            model=_PlannerModel(),
            tools=tools,
            workspace_root=".",
            config=AgenticResearchConfig(enabled=True),
        )

        visible = {tool.name for tool in _react_tools_for_runtime(tools, runtime)}
        self.assertTrue({"knowledge_search", "read_file", "web_search"}.issubset(visible))
        self.assertIn("write_file", visible)

    def test_planner_tool_returns_valid_plan_without_calling_evidence_tools(self):
        from backend.agentic_research.runtime import build_research_planner_tool

        model = _PlannerModel()
        evidence_called = []
        tool = build_research_planner_tool(model)

        result = json.loads(tool.invoke({"request": "find the answer", "knowledge_policy": "auto"}))

        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["source_sequence"], ["personal_knowledge", "web"])
        self.assertEqual(len(model.calls), 1)
        self.assertEqual(evidence_called, [])

    def test_planner_tool_contains_failure_category_on_invalid_plan(self):
        from backend.agentic_research.runtime import build_research_planner_tool

        tool = build_research_planner_tool(
            _PlannerModel(response={"requires_evidence": True, "source_sequence": ["not-a-source"]})
        )

        result = json.loads(tool.invoke({"request": "find the answer", "knowledge_policy": "auto"}))

        self.assertEqual(result["status"], "error")
        self.assertIn(result["error_category"], {"invalid_plan", "planner_error"})

    def test_required_policy_blocks_non_knowledge_evidence_tool(self):
        from backend.runtime_context import bind_runtime_context, reset_runtime_context
        from backend import tools as runtime_tools

        original = _FakeTool("web_search")
        original.invoke = lambda _payload: '{"results":[{"title":"unexpected"}]}'
        wrapped = runtime_tools._wrap_tool_with_run_dedupe(original)
        tokens = bind_runtime_context("routing-test", "run-1", knowledge_policy="required")
        try:
            result = json.loads(wrapped.invoke({"query": "question"}))
        finally:
            reset_runtime_context(tokens)

        self.assertEqual(result["status"], "policy_denied")

    def test_source_budget_blocks_repeated_evidence_calls_before_service(self):
        from backend.runtime_context import bind_runtime_context, reset_runtime_context
        from backend import tools as runtime_tools

        calls = []
        original = _FakeTool("web_search")
        original.invoke = lambda _payload: calls.append(True) or '{"results":[]}'
        wrapped = runtime_tools._wrap_tool_with_run_dedupe(original)
        tokens = bind_runtime_context(
            "routing-test",
            "run-budget",
            knowledge_policy="auto",
            source_call_limits={"web": 1},
        )
        try:
            first = json.loads(wrapped.invoke({"query": "question"}))
            second = json.loads(wrapped.invoke({"query": "different question"}))
        finally:
            reset_runtime_context(tokens)

        self.assertEqual(first["results"], [])
        self.assertEqual(second["status"], "budget_denied")
        self.assertEqual(calls, [True])

    def test_answer_run_rejects_internal_index_reads(self):
        from backend.runtime_context import bind_runtime_context, reset_runtime_context
        from backend import tools as runtime_tools

        with patch.object(runtime_tools, "_ALLOWED_ROOT", str(Path.cwd().resolve())):
            tokens = bind_runtime_context("routing-test", "run-2", knowledge_policy="auto")
            try:
                result = runtime_tools.read_file.invoke({"path": "knowledge_base/index/chunks.json"})
            finally:
                reset_runtime_context(tokens)

        self.assertIn("Access denied", result)


if __name__ == "__main__":
    unittest.main()
