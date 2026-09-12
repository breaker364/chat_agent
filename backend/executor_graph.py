"""Bounded single-task ReAct executor with capability-scoped tools."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Annotated, Any, TypedDict

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage

from .workflow_compat import END, START, StateGraph, add_messages


class ExecutorState(TypedDict, total=False):
    task: dict[str, Any]
    task_context: str
    allowed_capabilities: list[str]
    messages: Annotated[list[BaseMessage], add_messages]
    attempt: int
    max_steps: int
    step_count: int
    status: str
    output: str
    errors: list[dict[str, Any]]
    artifacts: list[dict[str, Any]]
    usage: dict[str, Any]
    events: list[dict[str, Any]]


def _append(existing: list[dict[str, Any]] | None, update: dict[str, Any]) -> list[dict[str, Any]]:
    return [*(existing or []), update]


def _tool_capabilities(tool: Any, metadata: Mapping[str, Mapping[str, Any]] | None, name: str) -> set[str]:
    configured = (metadata or {}).get(name, {})
    raw = configured.get("capabilities") if isinstance(configured, Mapping) else None
    if raw is None:
        raw = getattr(tool, "capabilities", ())
    if isinstance(raw, str):
        raw = [raw]
    return {str(item or "").strip().lower() for item in (raw or ()) if str(item or "").strip()}


def _invoke_tool(tool: Any, args: Mapping[str, Any]) -> Any:
    invoke = getattr(tool, "invoke", None)
    if callable(invoke):
        return invoke(dict(args))
    if callable(tool):
        return tool(**dict(args))
    raise TypeError("registered tool is not callable")


def _as_ai_message(response: Any) -> AIMessage:
    if isinstance(response, AIMessage):
        return response
    content = getattr(response, "content", response)
    return AIMessage(content=str(content or ""))


def build_executor_graph(
    *,
    model: Any,
    tools: Mapping[str, Any],
    tool_metadata: Mapping[str, Mapping[str, Any]] | None = None,
):
    """Compile a reusable executor that never exposes tools outside task capabilities."""
    registered_tools = {str(name): tool for name, tool in tools.items() if str(name).strip()}

    def executor_prepare(state: ExecutorState) -> dict[str, Any]:
        task = dict(state.get("task") or {})
        task_id = str(task.get("task_id") or "").strip()
        if not task_id:
            return {
                "status": "failed",
                "errors": _append(state.get("errors"), {"category": "invalid_task", "message": "task_id is required"}),
            }
        messages = state.get("messages") or []
        if messages:
            return {"status": state.get("status") or "running", "step_count": int(state.get("step_count") or 0)}
        context = str(state.get("task_context") or task.get("context") or "").strip()
        return {
            "messages": [HumanMessage(content=context or f"Complete task {task_id}.")],
            "status": "running",
            "step_count": 0,
            "events": _append(state.get("events"), {"node": "executor_prepare", "task_id": task_id, "status": "started"}),
        }

    def executor_agent(state: ExecutorState) -> dict[str, Any]:
        if state.get("status") not in {None, "running"}:
            return {}
        steps = int(state.get("step_count") or 0)
        max_steps = max(1, int(state.get("max_steps") or 8))
        if steps >= max_steps:
            return {
                "status": "failed",
                "errors": _append(state.get("errors"), {"category": "budget_exhausted", "message": "executor model step limit reached"}),
            }
        try:
            response = _as_ai_message(model.invoke(state.get("messages") or []))
        except Exception as exc:
            return {
                "status": "failed",
                "errors": _append(state.get("errors"), {"category": "model_error", "message": type(exc).__name__}),
            }
        task_id = str((state.get("task") or {}).get("task_id") or "")
        return {
            "messages": [response],
            "step_count": steps + 1,
            "events": _append(state.get("events"), {"node": "executor_agent", "task_id": task_id, "status": "completed"}),
        }

    def executor_tools(state: ExecutorState) -> dict[str, Any]:
        messages = state.get("messages") or []
        last = messages[-1] if messages else None
        calls = getattr(last, "tool_calls", None) if isinstance(last, AIMessage) else None
        if not calls:
            return {}
        allowed = {str(item or "").strip().lower() for item in state.get("allowed_capabilities", []) if str(item or "").strip()}
        task_id = str((state.get("task") or {}).get("task_id") or "")
        tool_messages: list[ToolMessage] = []
        events = list(state.get("events") or [])
        errors = list(state.get("errors") or [])
        for call in calls:
            name = str(call.get("name") or "").strip()
            call_id = str(call.get("id") or "").strip() or f"{task_id}:tool"
            args = call.get("args") if isinstance(call.get("args"), Mapping) else {}
            tool = registered_tools.get(name)
            tool_capabilities = _tool_capabilities(tool, tool_metadata, name) if tool is not None else set()
            if tool is None or not tool_capabilities or not tool_capabilities.issubset(allowed):
                errors.append({"category": "tool_not_allowed", "tool": name, "task_id": task_id})
                events.append({"node": "executor_tools", "task_id": task_id, "tool": name, "status": "denied"})
                tool_messages.append(ToolMessage(content=json.dumps({"status": "denied"}), tool_call_id=call_id))
                continue
            try:
                output = _invoke_tool(tool, args)
                content = output if isinstance(output, str) else json.dumps(output, ensure_ascii=False, default=str)
                tool_messages.append(ToolMessage(content=content, tool_call_id=call_id))
                events.append({"node": "executor_tools", "task_id": task_id, "tool": name, "status": "completed"})
            except Exception as exc:
                errors.append({"category": "tool_error", "tool": name, "message": type(exc).__name__, "task_id": task_id})
                tool_messages.append(ToolMessage(content=json.dumps({"status": "error"}), tool_call_id=call_id))
                events.append({"node": "executor_tools", "task_id": task_id, "tool": name, "status": "failed"})
        return {
            "messages": tool_messages,
            "errors": errors,
            "events": events,
            "status": "failed" if errors else "running",
        }

    def executor_validate(state: ExecutorState) -> dict[str, Any]:
        if state.get("status") == "failed":
            return {"output": ""}
        candidate = ""
        for message in reversed(state.get("messages") or []):
            if isinstance(message, AIMessage) and not getattr(message, "tool_calls", None):
                candidate = str(message.content or "").strip()
                break
        if not candidate:
            return {
                "status": "failed",
                "errors": _append(state.get("errors"), {"category": "invalid_output", "message": "executor returned no final answer"}),
                "output": "",
            }
        task_id = str((state.get("task") or {}).get("task_id") or "")
        return {
            "status": "succeeded",
            "output": candidate,
            "events": _append(state.get("events"), {"node": "executor_validate", "task_id": task_id, "status": "completed"}),
        }

    def decide_after_prepare(state: ExecutorState) -> str:
        return "executor_validate" if state.get("status") == "failed" else "executor_agent"

    def decide_after_agent(state: ExecutorState) -> str:
        if state.get("status") == "failed":
            return "executor_validate"
        messages = state.get("messages") or []
        last = messages[-1] if messages else None
        return "executor_tools" if isinstance(last, AIMessage) and getattr(last, "tool_calls", None) else "executor_validate"

    def decide_after_tools(state: ExecutorState) -> str:
        return "executor_validate" if state.get("status") == "failed" else "executor_agent"

    graph = StateGraph(ExecutorState)
    graph.add_node("executor_prepare", executor_prepare)
    graph.add_node("executor_agent", executor_agent)
    graph.add_node("executor_tools", executor_tools)
    graph.add_node("executor_validate", executor_validate)
    graph.add_edge(START, "executor_prepare")
    graph.add_conditional_edges("executor_prepare", decide_after_prepare)
    graph.add_conditional_edges("executor_agent", decide_after_agent)
    graph.add_conditional_edges("executor_tools", decide_after_tools)
    graph.add_edge("executor_validate", END)
    return graph.compile(name="executor_subgraph")
