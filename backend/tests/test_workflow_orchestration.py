from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path


class _FakeTool:
    def __init__(self, name: str, capabilities: tuple[str, ...]):
        self.name = name
        self.capabilities = capabilities
        self.calls: list[dict] = []

    def invoke(self, payload):
        self.calls.append(dict(payload))
        return {"status": "ok", "content": f"tool:{self.name}"}


class _ScriptedModel:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def invoke(self, messages):
        self.calls.append(messages)
        return self.responses.pop(0)


class _RouteModel:
    def invoke(self, _messages):
        return {
            "mode": "planned",
            "task_kinds": ["execute"],
            "needs_research": False,
            "risk_level": "low",
            "confidence": 0.95,
            "required_capabilities": ["read"],
        }


class _HighRiskRouteModel:
    def invoke(self, _messages):
        return {
            "mode": "planned",
            "task_kinds": ["execute"],
            "needs_research": False,
            "risk_level": "high",
            "confidence": 0.95,
            "required_capabilities": ["read"],
        }


class WorkflowStateTests(unittest.TestCase):
    def test_late_task_attempt_cannot_overwrite_newer_result(self):
        from backend.workflow_state import merge_task_results

        current = {
            "task-1": {
                "task_id": "task-1",
                "attempt_id": "task-1:2",
                "status": "succeeded",
                "output": "new",
            }
        }
        late = {
            "task-1": {
                "task_id": "task-1",
                "attempt_id": "task-1:1",
                "status": "failed",
                "output": "old",
            }
        }

        merged = merge_task_results(current, late)

        self.assertEqual(merged["task-1"]["output"], "new")
        self.assertEqual(merged["task-1"]["status"], "succeeded")

    def test_session_mapping_reuses_completed_tasks_and_persists_workflow_result(self):
        from backend.session_store import SessionStore
        from backend.workflow_persistence import load_workflow_state, persist_workflow_result

        with tempfile.TemporaryDirectory() as directory:
            store = SessionStore(Path(directory))
            store.set_task_plan("session-1", [
                {"task_id": "done", "content": "already complete", "status": "completed", "details": "saved answer"},
                {"task_id": "next", "content": "still pending", "status": "pending"},
            ])

            state = load_workflow_state(store, "session-1", "run-2", "continue work")

            self.assertEqual(state["task_results"]["done"]["status"], "succeeded")
            self.assertEqual(state["plan"]["tasks"][1]["task_id"], "next")

            persist_workflow_result(store, state, {
                "status": "completed",
                "response": "workflow answer",
                "validation": {"passed": True},
                "task_results": {
                    "next": {"task_id": "next", "attempt_id": "next:1", "status": "succeeded", "output": "new answer"},
                },
                "artifacts": {},
            })

            self.assertEqual(store.get_resume_context("session-1")["stage_results"]["workflow"]["status"], "completed")
            todos = {item["task_id"]: item for item in store.load_task_plan("session-1")["todos"]}
            self.assertEqual(todos["next"]["status"], "completed")


class RoutingAndPlanningTests(unittest.TestCase):
    def test_route_rejects_unregistered_capability(self):
        from backend.workflow_policy import RouteProposal, RoutePolicyError, enforce_route

        proposal = RouteProposal.model_validate({
            "mode": "planned",
            "task_kinds": ["execute"],
            "needs_research": False,
            "risk_level": "low",
            "confidence": 0.9,
            "required_capabilities": ["not-registered"],
        })

        with self.assertRaises(RoutePolicyError):
            enforce_route(proposal, request="read a file", capabilities={"read"})

    def test_plan_guard_rejects_dependency_cycle(self):
        from backend.workflow_policy import PlanPolicyError, validate_plan

        with self.assertRaises(PlanPolicyError):
            validate_plan([
                {"task_id": "a", "kind": "execute", "depends_on": ["b"], "capabilities": ["read"]},
                {"task_id": "b", "kind": "execute", "depends_on": ["a"], "capabilities": ["read"]},
            ], capabilities={"read"})


class ExecutorSubgraphTests(unittest.TestCase):
    def test_executor_refuses_tool_outside_task_allowlist(self):
        from langchain_core.messages import AIMessage
        from backend.executor_graph import build_executor_graph

        forbidden = _FakeTool("write", ("write",))
        model = _ScriptedModel([
            AIMessage(content="", tool_calls=[{
                "name": "write",
                "args": {"path": "note.txt"},
                "id": "call-1",
                "type": "tool_call",
            }])
        ])
        graph = build_executor_graph(model=model, tools={"write": forbidden})

        result = asyncio.run(graph.ainvoke({
            "task": {"task_id": "task-1", "kind": "execute", "capabilities": ["read"]},
            "task_context": "read only",
            "allowed_capabilities": ["read"],
            "messages": [],
            "attempt": 1,
        }))

        self.assertEqual(result["status"], "failed")
        self.assertEqual(forbidden.calls, [])
        self.assertEqual(result["errors"][0]["category"], "tool_not_allowed")

    def test_executor_runs_authorized_tool_and_validates_answer(self):
        from langchain_core.messages import AIMessage
        from backend.executor_graph import build_executor_graph

        reader = _FakeTool("read", ("read",))
        model = _ScriptedModel([
            AIMessage(content="", tool_calls=[{
                "name": "read",
                "args": {"path": "note.txt"},
                "id": "call-1",
                "type": "tool_call",
            }]),
            AIMessage(content="done"),
        ])
        graph = build_executor_graph(model=model, tools={"read": reader})

        result = asyncio.run(graph.ainvoke({
            "task": {"task_id": "task-1", "kind": "execute", "capabilities": ["read"]},
            "task_context": "read only",
            "allowed_capabilities": ["read"],
            "messages": [],
            "attempt": 1,
        }))

        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(result["output"], "done")
        self.assertEqual(len(reader.calls), 1)


class TopLevelWorkflowTests(unittest.TestCase):
    def test_planned_workflow_dispatches_tasks_and_joins_results(self):
        from langchain_core.messages import AIMessage
        from backend.executor_graph import build_executor_graph
        from backend.workflow_graph import build_workflow_graph
        from backend.workflow_state import initial_workflow_state

        reader = _FakeTool("read", ("read",))
        model = _ScriptedModel([
            AIMessage(content="done"),
            AIMessage(content="done"),
        ])
        executor = build_executor_graph(model=model, tools={"read": reader})
        graph = build_workflow_graph(
            route_model=_RouteModel(),
            planner=lambda _request, _route: [
                {"task_id": "a", "kind": "execute", "depends_on": [], "capabilities": ["read"]},
                {"task_id": "b", "kind": "execute", "depends_on": [], "capabilities": ["read"]},
            ],
            executor=executor,
            capabilities={"read"},
        )

        result = asyncio.run(graph.ainvoke(
            initial_workflow_state("session-1", "run-1", "read two files"),
            config={"configurable": {"thread_id": "session-1:run-1"}},
        ))

        self.assertEqual(result["status"], "completed")
        self.assertEqual(set(result["task_results"]), {"a", "b"})
        self.assertEqual(result["response"], "done")

    def test_high_risk_plan_interrupts_and_only_resumes_after_approval(self):
        from langchain_core.messages import AIMessage
        from langgraph.types import Command
        from backend.executor_graph import build_executor_graph
        from backend.workflow_graph import build_workflow_graph
        from backend.workflow_state import initial_workflow_state

        reader = _FakeTool("read", ("read",))
        executor = build_executor_graph(model=_ScriptedModel([AIMessage(content="approved result")]), tools={"read": reader})
        graph = build_workflow_graph(
            route_model=_HighRiskRouteModel(),
            planner=lambda _request, _route: [
                {"task_id": "a", "kind": "execute", "depends_on": [], "capabilities": ["read"]},
            ],
            executor=executor,
            capabilities={"read"},
        )
        config = {"configurable": {"thread_id": "session-1:approval-run"}}

        interrupted = graph.invoke(initial_workflow_state("session-1", "approval-run", "protected action"), config=config)

        self.assertIn("__interrupt__", interrupted)
        self.assertEqual(reader.calls, [])

        resumed = graph.invoke(Command(resume={"approved": True}), config=config)

        self.assertEqual(resumed["status"], "completed")
        self.assertEqual(resumed["response"], "approved result")

    def test_assessment_uses_limited_replan_after_terminal_task_failure(self):
        from langchain_core.messages import AIMessage
        from backend.executor_graph import build_executor_graph
        from backend.workflow_graph import build_workflow_graph
        from backend.workflow_state import initial_workflow_state

        plans = [
            [{"task_id": "first", "kind": "execute", "depends_on": [], "capabilities": [], "max_attempts": 1}],
            [{"task_id": "replacement", "kind": "execute", "depends_on": [], "capabilities": [], "max_attempts": 1}],
        ]
        executor = build_executor_graph(
            model=_ScriptedModel([AIMessage(content=""), AIMessage(content="replanned answer")]),
            tools={},
        )
        graph = build_workflow_graph(
            route_model=_RouteModel(),
            planner=lambda _request, _route: plans.pop(0),
            executor=executor,
            capabilities={"read"},
        )

        result = asyncio.run(graph.ainvoke(
            initial_workflow_state("session-1", "replan-run", "recover", control={"max_replans": 1}),
            config={"configurable": {"thread_id": "session-1:replan-run"}},
        ))

        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["response"], "replanned answer")
        self.assertEqual(result["control"]["replans"], 1)


if __name__ == "__main__":
    unittest.main()
