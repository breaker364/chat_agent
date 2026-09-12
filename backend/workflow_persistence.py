"""Projection layer between durable session facts and ephemeral workflow state."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .session_store import SessionStore
from .workflow_state import WorkflowState, initial_workflow_state


def load_workflow_state(
    store: SessionStore,
    session_id: str,
    run_id: str,
    message: str,
    *,
    knowledge_policy: str = "auto",
) -> WorkflowState:
    """Project canonical session facts into a new, run-scoped workflow state."""
    state = initial_workflow_state(
        session_id,
        run_id,
        message,
        history=store.get_history(session_id),
        knowledge_policy=knowledge_policy,
    )
    plan = store.load_task_plan(session_id)
    todos = plan.get("todos") if isinstance(plan.get("todos"), list) else []
    tasks: list[dict[str, Any]] = []
    results: dict[str, dict[str, Any]] = {}
    for todo in todos:
        if not isinstance(todo, Mapping):
            continue
        task_id = str(todo.get("task_id") or "").strip()
        if not task_id:
            continue
        task = {
            "task_id": task_id,
            "kind": "execute",
            "depends_on": [],
            "capabilities": [],
            "max_attempts": 2,
            "context": str(todo.get("content") or ""),
        }
        tasks.append(task)
        if str(todo.get("status") or "").strip().lower() == "completed":
            results[task_id] = {
                "task_id": task_id,
                "attempt_id": f"{task_id}:0",
                "status": "succeeded",
                "output": str(todo.get("details") or todo.get("result_ref") or ""),
            }
    resume = store.get_resume_context(session_id)
    state["plan"] = {
        "version": 1 if tasks else 0,
        "tasks": tasks,
        "validated": bool(tasks),
        "batch_id": 0,
    }
    state["task_results"] = results
    state["artifacts"] = {
        str(item.get("artifact_id") or index): dict(item)
        for index, item in enumerate(resume.get("artifacts") or [])
        if isinstance(item, Mapping)
    }
    return state


def persist_workflow_result(
    store: SessionStore,
    state: Mapping[str, Any],
    result: Mapping[str, Any],
) -> None:
    """Write verified workflow results back through existing session APIs."""
    request = state.get("request") if isinstance(state.get("request"), Mapping) else {}
    session_id = str(request.get("session_id") or "").strip()
    if not session_id:
        raise ValueError("workflow result is missing a trusted session_id")
    task_results = result.get("task_results") if isinstance(result.get("task_results"), Mapping) else {}
    for task_id, task_result in task_results.items():
        if not isinstance(task_result, Mapping):
            continue
        status = str(task_result.get("status") or "").strip()
        if status == "succeeded":
            store.update_task_plan_todo(
                session_id,
                str(task_id),
                status="completed",
                details=str(task_result.get("output") or "")[:1200],
            )
    artifacts = result.get("artifacts") if isinstance(result.get("artifacts"), Mapping) else {}
    for artifact in artifacts.values():
        if isinstance(artifact, Mapping):
            store.register_artifact(session_id, dict(artifact))
    validation = result.get("validation") if isinstance(result.get("validation"), Mapping) else {}
    store.record_stage_result(
        session_id,
        "workflow",
        {
            "status": str(result.get("status") or "blocked"),
            "summary": str(result.get("response") or "")[:1200],
            "item_count": len(task_results),
            "verified": bool(validation.get("passed")),
            "source_tool": "workflow_graph",
        },
    )
