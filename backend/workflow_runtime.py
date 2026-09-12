"""Opt-in application adapter for the fixed LangGraph workflow."""

from __future__ import annotations

import json
from collections.abc import AsyncGenerator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage

from .config import WorkflowConfig
from .executor_graph import build_executor_graph
from .research_graph import build_research_graph
from .session_store import SessionStore
from .workflow_compat import Command
from .workflow_graph import build_workflow_graph, deterministic_fallback_planner
from .workflow_observability import WORKFLOW_METRICS
from .workflow_persistence import load_workflow_state, persist_workflow_result
from .workflow_state import initial_workflow_state, workflow_thread_id


def _json_object(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    raw = str(getattr(value, "content", value) or "").strip()
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        start, end = raw.find("{"), raw.rfind("}")
        if start < 0 or end <= start:
            raise ValueError("route model did not return a JSON object")
        parsed = json.loads(raw[start : end + 1])
    if not isinstance(parsed, Mapping):
        raise ValueError("route model response must be an object")
    return dict(parsed)


class ModelRouteAdapter:
    """Restrict model responsibility to a bounded route proposal."""

    def __init__(self, model: Any) -> None:
        self.model = model

    def invoke(self, messages: Sequence[Any]) -> dict[str, Any]:
        request = ""
        if messages:
            last = messages[-1]
            request = str(last.get("content") if isinstance(last, Mapping) else getattr(last, "content", "") or "")
        prompt = (
            "Return JSON only. Select workflow mode and generic required capabilities. "
            "Allowed mode values are direct, planned, research, clarify. "
            "Allowed task_kinds are execute and research. Risk level is low, medium, or high. "
            "Required keys: mode, task_kinds, needs_research, risk_level, confidence, required_capabilities. "
            "Do not name graph nodes, edges, or tools.\n\nRequest:\n"
            + request[:4000]
        )
        response = self.model.invoke([
            SystemMessage(content="You are a strict workflow route proposer."),
            HumanMessage(content=prompt),
        ])
        return _json_object(response)


class ModelTaskPlanner:
    """Produce only a bounded task DAG whose capabilities were pre-authorized by route policy."""

    def __init__(self, model: Any) -> None:
        self.model = model

    def __call__(self, request: str, route: Mapping[str, Any]) -> list[dict[str, Any]]:
        allowed = [str(item or "").strip().lower() for item in route.get("required_capabilities", []) if str(item or "").strip()]
        prompt = (
            "Return JSON only with one key, tasks. tasks is a non-empty list of objects with "
            "task_id, kind, depends_on, capabilities, context, and max_attempts. "
            "kind must be execute or research. dependencies must reference task IDs in this list. "
            "Capabilities may only be selected from this approved set: "
            + json.dumps(allowed, ensure_ascii=False)
            + ". Do not return graph nodes, edges, or tool names.\n\nRequest:\n"
            + str(request or "")[:4000]
        )
        response = self.model.invoke([
            SystemMessage(content="You are a strict bounded task planner."),
            HumanMessage(content=prompt),
        ])
        payload = _json_object(response)
        tasks = payload.get("tasks")
        if not isinstance(tasks, list) or not tasks:
            raise ValueError("task planner must return a non-empty tasks list")
        normalized: list[dict[str, Any]] = []
        allowed_set = set(allowed)
        for task in tasks:
            if not isinstance(task, Mapping):
                raise ValueError("task planner tasks must be objects")
            candidate = dict(task)
            raw_capabilities = candidate.get("capabilities", [])
            if not isinstance(raw_capabilities, list):
                raise ValueError("task capabilities must be a list")
            capabilities = [str(item or "").strip().lower() for item in raw_capabilities if str(item or "").strip()]
            if set(capabilities) - allowed_set:
                raise ValueError("task planner requested capabilities outside the route decision")
            candidate["capabilities"] = capabilities
            normalized.append(candidate)
        return normalized


def _tool_metadata(
    tools: Sequence[Any],
    configured_capabilities: Mapping[str, Sequence[str]] | None = None,
) -> tuple[dict[str, Any], dict[str, dict[str, Any]], set[str]]:
    by_name: dict[str, Any] = {}
    metadata: dict[str, dict[str, Any]] = {}
    capabilities: set[str] = set()
    for tool in tools:
        name = str(getattr(tool, "name", "") or "").strip()
        if not name:
            continue
        raw_metadata = getattr(tool, "workflow_metadata", None)
        if not isinstance(raw_metadata, Mapping):
            candidate = getattr(tool, "metadata", None)
            raw_metadata = candidate.get("workflow") if isinstance(candidate, Mapping) and isinstance(candidate.get("workflow"), Mapping) else {}
        raw_capabilities = raw_metadata.get("capabilities") if isinstance(raw_metadata, Mapping) else ()
        if isinstance(configured_capabilities, Mapping) and name in configured_capabilities:
            raw_capabilities = configured_capabilities[name]
        if isinstance(raw_capabilities, str):
            raw_capabilities = [raw_capabilities]
        normalized = [str(item or "").strip().lower() for item in (raw_capabilities or ()) if str(item or "").strip()]
        by_name[name] = tool
        metadata[name] = {"capabilities": normalized}
        capabilities.update(normalized)
    return by_name, metadata, capabilities


def map_workflow_event(event: Mapping[str, Any]) -> dict[str, str]:
    """Map graph lifecycle metadata onto the established SSE event vocabulary."""
    payload = dict(event)
    node = str(payload.get("node") or "workflow")
    status = str(payload.get("status") or "running")
    return {
        "event": "progress",
        "data": json.dumps(
            {
                "message": f"Workflow node `{node}` {status}.",
                "active_tool": None,
                "workflow": payload,
            },
            ensure_ascii=False,
        ),
    }


@dataclass
class WorkflowRuntime:
    graph: Any
    config: WorkflowConfig
    workspace_root: Path | None = None
    agentic_research_runtime: Any | None = None

    @property
    def name(self) -> str:
        return "chat_agent_workflow"

    async def stream_events(
        self,
        *,
        message: str,
        session_id: str,
        run_id: str,
        history: list[dict[str, Any]] | None = None,
        knowledge_policy: str = "auto",
    ) -> AsyncGenerator[dict[str, str], None]:
        store = SessionStore(self.workspace_root) if self.workspace_root is not None else None
        state = (
            load_workflow_state(store, session_id, run_id, message, knowledge_policy=knowledge_policy)
            if store is not None
            else initial_workflow_state(session_id, run_id, message, history=history, knowledge_policy=knowledge_policy)
        )
        state["request"]["history"] = list(history or state["request"].get("history") or [])
        state["control"].update({
            "max_tasks": self.config["max_tasks"],
            "max_parallel_tasks": self.config["max_parallel_tasks"],
            "max_task_attempts": self.config["max_task_attempts"],
            "max_replans": self.config["max_replans"],
        })
        graph_config = {"configurable": {"thread_id": workflow_thread_id(session_id, run_id)}}
        result = await self.graph.ainvoke(state, config=graph_config)
        for event in result.get("events", []):
            if not isinstance(event, Mapping):
                continue
            WORKFLOW_METRICS.record_event(event)
            yield {
                "event": "debug",
                "data": json.dumps({"stage": "workflow_node", "workflow": dict(event)}, ensure_ascii=False),
            }
            yield map_workflow_event(event)
        if result.get("__interrupt__"):
            WORKFLOW_METRICS.record_terminal("blocked")
            payload = {"status": "blocked", "reason": "approval_required", "run_id": run_id}
            yield {"event": "debug", "data": json.dumps({"stage": "approval_needed", "workflow": payload}, ensure_ascii=False)}
            yield {"event": "done", "data": json.dumps("Workflow is waiting for approval.", ensure_ascii=False)}
            return
        response = str(result.get("response") or "")
        WORKFLOW_METRICS.record_terminal(str(result.get("status") or "blocked"))
        if store is not None:
            persist_workflow_result(store, state, result)
        yield {"event": "done", "data": json.dumps(response, ensure_ascii=False)}

    async def approve(self, *, session_id: str, run_id: str, approved: bool) -> dict[str, Any]:
        """Resume the exact checkpoint that owns a pending approval."""
        graph_config = {"configurable": {"thread_id": workflow_thread_id(session_id, run_id)}}
        result = await self.graph.ainvoke(Command(resume={"approved": bool(approved)}), config=graph_config)
        return dict(result)


def build_workflow_runtime(
    *,
    model: Any,
    tools: Sequence[Any],
    config: WorkflowConfig,
    workspace_root: str | Path | None = None,
    research_runtime: Any | None = None,
) -> WorkflowRuntime | None:
    """Build the opt-in runtime; disabled configuration preserves the legacy Agent."""
    if not bool(config.get("enabled")):
        return None
    tool_map, metadata, capabilities = _tool_metadata(tools, config.get("tool_capabilities"))
    executor = build_executor_graph(model=model, tools=tool_map, tool_metadata=metadata)
    research = build_research_graph(research_runtime) if research_runtime is not None else None
    graph = build_workflow_graph(
        route_model=ModelRouteAdapter(model),
        planner=ModelTaskPlanner(model),
        executor=executor,
        research=research,
        capabilities=capabilities,
        executor_max_steps=config["executor_max_steps"],
    )
    return WorkflowRuntime(
        graph=graph,
        config=config,
        workspace_root=Path(workspace_root).resolve() if workspace_root is not None else None,
        agentic_research_runtime=research_runtime,
    )
