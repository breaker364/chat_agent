"""Fixed-topology parent graph for constrained task orchestration."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from typing import Any

from .workflow_compat import END, START, MemorySaver, Send, StateGraph, interrupt
from .workflow_policy import (
    PlanPolicyError,
    RoutePolicyError,
    RouteProposal,
    enforce_route,
    parse_route_proposal,
    validate_plan,
)
from .workflow_state import WorkflowState, merge_task_results


def _event(node: str, state: Mapping[str, Any], *, status: str, **extra: Any) -> dict[str, Any]:
    request = state.get("request") if isinstance(state.get("request"), Mapping) else {}
    return {
        "graph_path": "workflow",
        "node": node,
        "session_id": request.get("session_id", ""),
        "run_id": request.get("run_id", ""),
        "status": status,
        **extra,
    }


def _as_mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    if hasattr(value, "model_dump"):
        return dict(value.model_dump())
    return {"content": str(getattr(value, "content", value) or "")}


def _task_attempt(task: Mapping[str, Any], results: Mapping[str, Any]) -> tuple[int, str]:
    task_id = str(task.get("task_id") or "")
    current = results.get(task_id) if isinstance(results, Mapping) else None
    raw = str((current or {}).get("attempt_id") or "").rsplit(":", 1)[-1]
    try:
        next_attempt = int(raw) + 1
    except ValueError:
        next_attempt = 1
    return next_attempt, f"{task_id}:{next_attempt}"


def _ready_tasks(state: Mapping[str, Any]) -> list[dict[str, Any]]:
    plan = state.get("plan") if isinstance(state.get("plan"), Mapping) else {}
    tasks = plan.get("tasks") if isinstance(plan.get("tasks"), list) else []
    results = state.get("task_results") if isinstance(state.get("task_results"), Mapping) else {}
    ready: list[dict[str, Any]] = []
    for raw_task in tasks:
        if not isinstance(raw_task, Mapping):
            continue
        task = dict(raw_task)
        task_id = str(task.get("task_id") or "")
        existing = results.get(task_id) if isinstance(results, Mapping) else None
        if isinstance(existing, Mapping) and existing.get("status") == "succeeded":
            continue
        dependencies = task.get("depends_on") if isinstance(task.get("depends_on"), list) else []
        if not all(isinstance(results.get(str(dependency)), Mapping) and results[str(dependency)].get("status") == "succeeded" for dependency in dependencies):
            continue
        max_attempts = int(task.get("max_attempts") or state.get("control", {}).get("max_task_attempts", 2))
        attempt, attempt_id = _task_attempt(task, results)
        if attempt > max_attempts:
            continue
        task["attempt"] = attempt
        task["attempt_id"] = attempt_id
        ready.append(task)
    limit = max(1, int((state.get("control") or {}).get("max_parallel_tasks", 4)))
    return ready[:limit]


def build_workflow_graph(
    *,
    route_model: Any,
    planner: Callable[[str, Mapping[str, Any]], Sequence[Mapping[str, Any]]],
    executor: Any,
    research: Any | None = None,
    capabilities: set[str] | frozenset[str],
    checkpointer: Any | None = None,
):
    """Compile the only parent topology permitted for a workflow invocation."""
    available_capabilities = {str(item or "").strip().lower() for item in capabilities if str(item or "").strip()}

    def intake(state: WorkflowState) -> dict[str, Any]:
        return {"events": [_event("intake", state, status="completed")]}

    def prepare_context(state: WorkflowState) -> dict[str, Any]:
        request = state.get("request") or {}
        return {
            "events": [_event("prepare_context", state, status="completed", history_count=len(request.get("history") or []))]
        }

    def route(state: WorkflowState) -> dict[str, Any]:
        request = state.get("request") or {}
        try:
            response = route_model.invoke([{"role": "user", "content": request.get("message", "")}])
            raw = getattr(response, "content", response)
            proposal = parse_route_proposal(_as_mapping(raw))
            decision = enforce_route(proposal, request=str(request.get("message") or ""), capabilities=available_capabilities)
            return {
                "route": decision.model_dump(),
                "events": [_event("route", state, status="completed", mode=decision.mode)],
            }
        except RoutePolicyError as exc:
            return {
                "route": {"mode": "clarify", "reason": "route_policy_error", "confidence": 0.0},
                "errors": [{"category": "route_policy_error", "message": str(exc)}],
                "events": [_event("route", state, status="failed")],
            }
        except Exception as exc:
            return {
                "route": {"mode": "clarify", "reason": "route_model_error", "confidence": 0.0},
                "errors": [{"category": "route_model_error", "message": type(exc).__name__}],
                "events": [_event("route", state, status="failed")],
            }

    def decide_route(state: WorkflowState) -> str:
        mode = str((state.get("route") or {}).get("mode") or "clarify")
        return {
            "direct": "direct_executor",
            "planned": "planner",
            "research": "planner",
            "clarify": "clarification_gate",
        }.get(mode, "clarification_gate")

    def planner_node(state: WorkflowState) -> dict[str, Any]:
        request = state.get("request") or {}
        route_decision = state.get("route") or {}
        try:
            tasks = [dict(item) for item in planner(str(request.get("message") or ""), route_decision)]
        except Exception as exc:
            return {
                "status": "blocked",
                "errors": [{"category": "planner_error", "message": type(exc).__name__}],
                "events": [_event("planner", state, status="failed")],
            }
        current_plan = state.get("plan") or {}
        return {
            "plan": {
                "version": int(current_plan.get("version") or 0) + 1,
                "tasks": tasks,
                "validated": False,
                "batch_id": int(current_plan.get("batch_id") or 0),
            },
            "events": [_event("planner", state, status="completed", task_count=len(tasks))],
        }

    def plan_guard(state: WorkflowState) -> dict[str, Any]:
        plan = state.get("plan") or {}
        try:
            validated = validate_plan(
                plan.get("tasks") or [],
                capabilities=available_capabilities,
                max_tasks=max(1, int((state.get("control") or {}).get("max_tasks", 12))),
            )
        except PlanPolicyError as exc:
            return {
                "status": "blocked",
                "errors": [{"category": "invalid_plan", "message": str(exc)}],
                "events": [_event("plan_guard", state, status="failed")],
            }
        return {
            "plan": {**plan, "tasks": [task.model_dump() for task in validated], "validated": True},
            "events": [_event("plan_guard", state, status="completed")],
        }

    def decide_after_plan_guard(state: WorkflowState) -> str:
        if state.get("status") == "blocked":
            return "finalize"
        if bool((state.get("route") or {}).get("approval_required")):
            return "approval_gate"
        return "scheduler"

    def approval_gate(state: WorkflowState) -> dict[str, Any]:
        plan = state.get("plan") or {}
        decision = interrupt({
            "kind": "workflow_approval",
            "reason": "route_requires_approval",
            "plan_version": plan.get("version"),
        })
        if isinstance(decision, Mapping) and bool(decision.get("approved")):
            return {"approval": {"status": "approved"}, "events": [_event("approval_gate", state, status="approved")]}
        return {
            "approval": {"status": "rejected"},
            "status": "blocked",
            "response": "Workflow execution was not approved.",
            "events": [_event("approval_gate", state, status="rejected")],
        }

    def decide_after_approval(state: WorkflowState) -> str:
        return "scheduler" if (state.get("approval") or {}).get("status") == "approved" else "finalize"

    def scheduler(state: WorkflowState) -> dict[str, Any]:
        plan = dict(state.get("plan") or {})
        ready = _ready_tasks(state)
        plan["batch_id"] = int(plan.get("batch_id") or 0) + 1
        control = dict(state.get("control") or {})
        control["batch_task_ids"] = [str(task.get("task_id") or "") for task in ready]
        return {
            "plan": plan,
            "control": control,
            "events": [_event("scheduler", state, status="completed", ready_count=len(ready), batch_id=plan["batch_id"])],
        }

    def schedule_ready_tasks(state: WorkflowState):
        ready = _ready_tasks(state)
        if not ready:
            return "assess"
        request = state.get("request") or {}
        return [
            Send(
                "dispatch_task",
                {
                    "task": task,
                    "task_context": str(task.get("context") or request.get("message") or ""),
                    "allowed_capabilities": list(task.get("capabilities") or []),
                    "attempt": task["attempt"],
                    "attempt_id": task["attempt_id"],
                    "request": request,
                },
            )
            for task in ready
        ]

    def dispatch_task(state: Mapping[str, Any]) -> dict[str, Any]:
        task = dict(state.get("task") or {})
        task_id = str(task.get("task_id") or "")
        attempt_id = str(state.get("attempt_id") or task.get("attempt_id") or f"{task_id}:1")
        try:
            if task.get("kind") == "research" and research is not None:
                raw_result = research.invoke({
                    "request": (state.get("request") or {}).get("message", ""),
                    "task": task,
                    "knowledge_policy": (state.get("request") or {}).get("knowledge_policy", "auto"),
                })
                result = _as_mapping(raw_result)
                status = "succeeded" if result.get("status") in {"succeeded", "completed", "answer_ready"} else "failed"
                output = str(result.get("summary") or result.get("output") or "")
                errors = list(result.get("errors") or [])
            else:
                result = executor.invoke({
                    "task": task,
                    "task_context": state.get("task_context") or "",
                    "allowed_capabilities": state.get("allowed_capabilities") or [],
                    "messages": [],
                    "attempt": state.get("attempt") or 1,
                })
                status = str(result.get("status") or "failed")
                output = str(result.get("output") or "")
                errors = list(result.get("errors") or [])
            task_result = {
                "task_id": task_id,
                "attempt_id": attempt_id,
                "status": status,
                "output": output,
                "errors": errors,
                "artifacts": list(result.get("artifacts") or []),
                "kind": task.get("kind"),
            }
        except Exception as exc:
            task_result = {
                "task_id": task_id,
                "attempt_id": attempt_id,
                "status": "failed",
                "output": "",
                "errors": [{"category": "subgraph_error", "message": type(exc).__name__}],
                "kind": task.get("kind"),
            }
        return {
            "task_results": {task_id: task_result},
            "errors": task_result["errors"],
            "events": [{"graph_path": "workflow.dispatch_task", "node": "dispatch_task", "task_id": task_id, "attempt": attempt_id, "status": task_result["status"]}],
        }

    def join(state: WorkflowState) -> dict[str, Any]:
        return {"events": [_event("join", state, status="completed")]}

    def assess(state: WorkflowState) -> dict[str, Any]:
        plan = state.get("plan") or {}
        tasks = plan.get("tasks") or []
        results = state.get("task_results") or {}
        if tasks and all(isinstance(results.get(str(task.get("task_id"))), Mapping) and results[str(task.get("task_id"))].get("status") == "succeeded" for task in tasks if isinstance(task, Mapping)):
            control = {**(state.get("control") or {}), "assessment": "complete"}
            return {"control": control, "events": [_event("assess", state, status="complete")]}
        ready = _ready_tasks(state)
        if ready:
            control = {**(state.get("control") or {}), "assessment": "retry"}
            return {"control": control, "events": [_event("assess", state, status="retry")]}
        failed = [
            result
            for task_id, result in results.items()
            if any(isinstance(task, Mapping) and str(task.get("task_id") or "") == str(task_id) for task in tasks)
            and isinstance(result, Mapping)
            and result.get("status") == "failed"
        ]
        control = dict(state.get("control") or {})
        replans = max(0, int(control.get("replans") or 0))
        max_replans = max(0, int(control.get("max_replans") or 0))
        if failed and replans < max_replans:
            control["replans"] = replans + 1
            control["assessment"] = "replan"
            return {"control": control, "events": [_event("assess", state, status="replan", replan=control["replans"])]}
        control = {**(state.get("control") or {}), "assessment": "blocked"}
        return {"control": control, "status": "blocked", "events": [_event("assess", state, status="blocked")]}

    def decide_assessment(state: WorkflowState) -> str:
        assessment = str((state.get("control") or {}).get("assessment") or "blocked")
        return {"complete": "validate", "retry": "scheduler", "replan": "planner", "blocked": "finalize"}.get(assessment, "finalize")

    def validate(state: WorkflowState) -> dict[str, Any]:
        results = state.get("task_results") or {}
        outputs: list[str] = []
        for _, result in sorted(results.items()):
            if not isinstance(result, Mapping) or result.get("status") != "succeeded":
                continue
            output = str(result.get("output") or "").strip()
            if output and output not in outputs:
                outputs.append(output)
        if not outputs:
            return {"validation": {"passed": False, "reason": "missing_output"}, "status": "blocked"}
        return {
            "validation": {"passed": True},
            "response": "\n\n".join(outputs),
            "events": [_event("validate", state, status="completed")],
        }

    def finalize(state: WorkflowState) -> dict[str, Any]:
        validated = bool((state.get("validation") or {}).get("passed"))
        status = "completed" if validated else str(state.get("status") or "blocked")
        return {"status": status, "events": [_event("finalize", state, status=status)]}

    def direct_executor(state: WorkflowState) -> dict[str, Any]:
        request = state.get("request") or {}
        route_decision = state.get("route") or {}
        task = {
            "task_id": "direct",
            "kind": "execute",
            "depends_on": [],
            "capabilities": list(route_decision.get("required_capabilities") or []),
            "max_attempts": 1,
            "context": str(request.get("message") or ""),
        }
        partial = dispatch_task({
            "task": task,
            "task_context": task["context"],
            "allowed_capabilities": task["capabilities"],
            "attempt": 1,
            "attempt_id": "direct:1",
            "request": request,
        })
        return {**partial, "plan": {"version": 1, "tasks": [task], "validated": True, "batch_id": 1}}

    def clarification_gate(state: WorkflowState) -> dict[str, Any]:
        return {
            "status": "blocked",
            "response": "More information is required before the workflow can execute.",
            "events": [_event("clarification_gate", state, status="blocked")],
        }

    graph = StateGraph(WorkflowState)
    graph.add_node("intake", intake)
    graph.add_node("prepare_context", prepare_context)
    graph.add_node("route", route)
    graph.add_node("planner", planner_node)
    graph.add_node("plan_guard", plan_guard)
    graph.add_node("approval_gate", approval_gate)
    graph.add_node("scheduler", scheduler)
    graph.add_node("dispatch_task", dispatch_task)
    graph.add_node("join", join)
    graph.add_node("assess", assess)
    graph.add_node("validate", validate)
    graph.add_node("finalize", finalize)
    graph.add_node("direct_executor", direct_executor)
    graph.add_node("clarification_gate", clarification_gate)
    graph.add_edge(START, "intake")
    graph.add_edge("intake", "prepare_context")
    graph.add_edge("prepare_context", "route")
    graph.add_conditional_edges("route", decide_route)
    graph.add_edge("planner", "plan_guard")
    graph.add_conditional_edges("plan_guard", decide_after_plan_guard)
    graph.add_conditional_edges("approval_gate", decide_after_approval)
    graph.add_conditional_edges("scheduler", schedule_ready_tasks)
    graph.add_edge("dispatch_task", "join")
    graph.add_edge("direct_executor", "join")
    graph.add_edge("join", "assess")
    graph.add_conditional_edges("assess", decide_assessment)
    graph.add_edge("validate", "finalize")
    graph.add_edge("clarification_gate", "finalize")
    graph.add_edge("finalize", END)
    return graph.compile(checkpointer=checkpointer or MemorySaver(), name="workflow_graph")
