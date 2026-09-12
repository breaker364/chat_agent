from __future__ import annotations

import asyncio
import json
import unittest
from unittest.mock import patch


class _Model:
    def __init__(self):
        self.responses = [
            {
                "mode": "direct",
                "task_kinds": ["execute"],
                "needs_research": False,
                "risk_level": "low",
                "confidence": 0.95,
                "required_capabilities": [],
            },
            __import__("langchain_core.messages", fromlist=["AIMessage"]).AIMessage(content="workflow answer"),
        ]

    def invoke(self, _messages):
        return self.responses.pop(0)


class WorkflowRuntimeTests(unittest.TestCase):
    def test_default_workflow_config_keeps_legacy_runtime_disabled(self):
        from backend.config import load_workflow_config
        from backend.workflow_runtime import build_workflow_runtime

        config = load_workflow_config({})

        self.assertFalse(config["enabled"])
        self.assertIsNone(build_workflow_runtime(model=_Model(), tools=[], config=config))

    def test_executor_max_steps_config_defaults_bounded(self):
        from backend.config import WorkflowConfigError, load_workflow_config

        self.assertEqual(load_workflow_config({})["executor_max_steps"], 8)
        self.assertEqual(
            load_workflow_config({"enabled": True, "executor_max_steps": 3})["executor_max_steps"],
            3,
        )
        with self.assertRaises(WorkflowConfigError):
            load_workflow_config({"executor_max_steps": 0})
        with self.assertRaises(WorkflowConfigError):
            load_workflow_config({"executor_max_steps": 65})
        with self.assertRaises(WorkflowConfigError):
            load_workflow_config({"executor_max_steps": "8"})

    def test_enabled_runtime_maps_lifecycle_to_existing_sse_events(self):
        from backend.config import load_workflow_config
        from backend.workflow_runtime import build_workflow_runtime

        runtime = build_workflow_runtime(
            model=_Model(),
            tools=[],
            config=load_workflow_config({"enabled": True}),
        )

        events = asyncio.run(self._collect(runtime.stream_events(
            message="answer directly",
            session_id="workflow-session",
            run_id="workflow-run",
            history=[],
        )))

        self.assertTrue(any(event["event"] == "progress" for event in events))
        done = [event for event in events if event["event"] == "done"]
        self.assertEqual(json.loads(done[-1]["data"]), "workflow answer")
        debug_payloads = [json.loads(event["data"]) for event in events if event["event"] == "debug"]
        self.assertTrue(any(payload.get("workflow", {}).get("node") == "finalize" for payload in debug_payloads))

    def test_build_agent_selects_workflow_runtime_only_when_enabled(self):
        from backend.config import load_workflow_config
        from backend.workflow_runtime import WorkflowRuntime
        from backend.agent import build_agent

        async def empty_tools(**_kwargs):
            return []

        model = _Model()
        with patch("backend.agent.load_llm_config", return_value={}), patch(
            "backend.agent.create_chat_deepseek", return_value=model
        ), patch("backend.agent.wrap_chat_model_with_append_injection", return_value=model), patch(
            "backend.agent.get_all_tools", empty_tools
        ), patch("backend.agent.build_agentic_research_runtime", return_value=None), patch(
            "backend.agent.load_workflow_config", return_value=load_workflow_config({"enabled": True})
        ):
            agent = asyncio.run(build_agent())

        self.assertIsInstance(agent, WorkflowRuntime)

    def test_approval_endpoint_resumes_the_requested_workflow_run(self):
        from backend.main import workflow_approval

        class ApprovalRuntime:
            async def approve(self, *, session_id, run_id, approved):
                return {
                    "status": "completed" if approved else "blocked",
                    "response": f"{session_id}:{run_id}:{approved}",
                    "validation": {"passed": approved},
                }

        async def get_runtime():
            return ApprovalRuntime()

        with patch("backend.main.get_agent", get_runtime):
            response = asyncio.run(workflow_approval(_JsonRequest({"approved": True}), "session-a", "run-a"))

        self.assertEqual(response.status_code, 200)
        payload = json.loads(response.body)
        self.assertEqual(payload["status"], "completed")
        self.assertEqual(payload["response"], "session-a:run-a:True")

    def test_model_task_planner_returns_only_route_granted_capabilities(self):
        from backend.workflow_runtime import ModelTaskPlanner

        class PlannerModel:
            def invoke(self, _messages):
                return {
                    "tasks": [
                        {"task_id": "inspect", "kind": "execute", "depends_on": [], "capabilities": ["read"]},
                        {"task_id": "summarize", "kind": "execute", "depends_on": ["inspect"], "capabilities": []},
                    ]
                }

        tasks = ModelTaskPlanner(PlannerModel())(
            "inspect then summarize",
            {"mode": "planned", "required_capabilities": ["read"]},
        )

        self.assertEqual([task["task_id"] for task in tasks], ["inspect", "summarize"])
        self.assertEqual(tasks[0]["capabilities"], ["read"])

    def test_workflow_config_normalizes_declarative_tool_capabilities(self):
        from backend.config import load_workflow_config

        config = load_workflow_config({
            "enabled": True,
            "tool_capabilities": {"inspect": ["read", "read"]},
        })

        self.assertEqual(config["tool_capabilities"], {"inspect": ["read"]})

    def test_workflow_metrics_counts_nodes_tasks_and_terminal_status_without_payloads(self):
        from backend.workflow_observability import WorkflowMetrics

        metrics = WorkflowMetrics()
        metrics.record_event({"node": "route", "status": "completed"})
        metrics.record_event({"node": "dispatch_task", "task_id": "task-1", "status": "succeeded"})
        metrics.record_terminal("completed")

        snapshot = metrics.snapshot()

        self.assertEqual(snapshot["nodes"]["route:completed"], 1)
        self.assertEqual(snapshot["tasks"]["succeeded"], 1)
        self.assertEqual(snapshot["terminal"]["completed"], 1)

    async def _collect(self, generator):
        return [event async for event in generator]


class _JsonRequest:
    def __init__(self, body):
        self.body = body

    async def json(self):
        return self.body


if __name__ == "__main__":
    unittest.main()
