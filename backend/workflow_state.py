"""Typed state and deterministic reducers for fixed-topology workflows."""

from __future__ import annotations

from copy import deepcopy
from hashlib import sha256
from typing import Annotated, Any, Mapping, TypedDict

from langchain_core.messages import BaseMessage

from .workflow_compat import add_messages


WORKFLOW_STATE_SCHEMA_VERSION = "1"
MAX_AUDIT_EVENTS = 200
MAX_WORKFLOW_ERRORS = 100


class RequestContext(TypedDict):
    session_id: str
    run_id: str
    message: str
    history: list[dict[str, Any]]
    knowledge_policy: str


class RouteDecisionState(TypedDict, total=False):
    mode: str
    required_capabilities: list[str]
    risk_level: str
    confidence: float
    approval_required: bool
    reason: str


class PlanSnapshot(TypedDict, total=False):
    version: int
    tasks: list[dict[str, Any]]
    validated: bool
    batch_id: int


class TaskResult(TypedDict, total=False):
    task_id: str
    attempt_id: str
    status: str
    output: str
    artifacts: list[dict[str, Any]]
    errors: list[dict[str, Any]]
    usage: dict[str, Any]
    kind: str


class TaskRef(TypedDict, total=False):
    task_id: str
    kind: str
    depends_on: list[str]
    capabilities: list[str]
    max_attempts: int
    context: str


class TaskAttempt(TypedDict, total=False):
    task_id: str
    attempt_id: str
    attempt: int
    status: str
    idempotency_key: str


class ResearchResultState(TypedDict, total=False):
    status: str
    evidence: list[dict[str, Any]]
    gaps: list[str]
    budget: dict[str, Any]


class ArtifactRef(TypedDict, total=False):
    artifact_id: str
    status: str
    path: str
    url: str
    verified: bool


class WorkflowError(TypedDict, total=False):
    category: str
    message: str
    task_id: str


class AuditEvent(TypedDict, total=False):
    graph_path: str
    node: str
    task_id: str
    attempt: str
    status: str


class ApprovalState(TypedDict, total=False):
    status: str
    reason: str


class ControlState(TypedDict, total=False):
    max_tasks: int
    max_parallel_tasks: int
    max_task_attempts: int
    max_replans: int
    replans: int


def _attempt_sequence(value: Mapping[str, Any]) -> int:
    raw_attempt = str(value.get("attempt_id") or "").rsplit(":", 1)[-1]
    try:
        return max(0, int(raw_attempt))
    except (TypeError, ValueError):
        return int(value.get("attempt") or 0) if str(value.get("attempt") or "").isdigit() else 0


def merge_task_results(
    current: Mapping[str, Mapping[str, Any]] | None,
    update: Mapping[str, Mapping[str, Any]] | None,
) -> dict[str, TaskResult]:
    """Merge task results without allowing a late attempt to replace a newer one."""
    merged: dict[str, TaskResult] = {
        str(task_id): dict(value)
        for task_id, value in (current or {}).items()
        if isinstance(value, Mapping)
    }
    for task_id, candidate in (update or {}).items():
        if not isinstance(candidate, Mapping):
            continue
        normalized_id = str(task_id or candidate.get("task_id") or "").strip()
        if not normalized_id:
            continue
        existing = merged.get(normalized_id)
        if existing is None or _attempt_sequence(candidate) >= _attempt_sequence(existing):
            next_value = dict(candidate)
            next_value["task_id"] = normalized_id
            merged[normalized_id] = next_value
    return merged


def _stable_item_key(item: Mapping[str, Any], prefix: str) -> str:
    explicit = str(item.get("id") or item.get("artifact_id") or item.get("evidence_id") or "").strip()
    if explicit:
        return explicit
    source = str(item.get("source") or item.get("source_kind") or "").strip()
    uri = str(item.get("uri") or item.get("source_uri") or "").strip()
    excerpt = str(item.get("excerpt") or item.get("content") or item.get("summary") or "").strip()
    digest = sha256(f"{source}\n{uri}\n{excerpt}".encode("utf-8")).hexdigest()[:20]
    return f"{prefix}:{digest}"


def merge_artifacts(
    current: Mapping[str, Mapping[str, Any]] | None,
    update: Mapping[str, Mapping[str, Any]] | None,
) -> dict[str, dict[str, Any]]:
    merged = {str(key): dict(value) for key, value in (current or {}).items() if isinstance(value, Mapping)}
    for key, artifact in (update or {}).items():
        if not isinstance(artifact, Mapping):
            continue
        artifact_key = str(key or _stable_item_key(artifact, "artifact"))
        merged.setdefault(artifact_key, dict(artifact))
    return merged


def merge_evidence(
    current: list[Mapping[str, Any]] | None,
    update: list[Mapping[str, Any]] | None,
) -> list[dict[str, Any]]:
    merged: dict[str, dict[str, Any]] = {}
    for item in [*(current or []), *(update or [])]:
        if not isinstance(item, Mapping):
            continue
        key = _stable_item_key(item, "evidence")
        if key not in merged:
            merged[key] = dict(item)
    return list(merged.values())


def _merge_bounded_events(
    current: list[Mapping[str, Any]] | None,
    update: list[Mapping[str, Any]] | None,
    *,
    limit: int,
) -> list[dict[str, Any]]:
    values = [dict(item) for item in [*(current or []), *(update or [])] if isinstance(item, Mapping)]
    return values[-limit:]


def merge_errors(
    current: list[Mapping[str, Any]] | None,
    update: list[Mapping[str, Any]] | None,
) -> list[dict[str, Any]]:
    return _merge_bounded_events(current, update, limit=MAX_WORKFLOW_ERRORS)


def merge_events(
    current: list[Mapping[str, Any]] | None,
    update: list[Mapping[str, Any]] | None,
) -> list[dict[str, Any]]:
    return _merge_bounded_events(current, update, limit=MAX_AUDIT_EVENTS)


class WorkflowState(TypedDict, total=False):
    schema_version: str
    request: RequestContext
    route: RouteDecisionState
    plan: PlanSnapshot
    messages: Annotated[list[BaseMessage], add_messages]
    task_results: Annotated[dict[str, TaskResult], merge_task_results]
    research: ResearchResultState
    artifacts: Annotated[dict[str, ArtifactRef], merge_artifacts]
    errors: Annotated[list[WorkflowError], merge_errors]
    events: Annotated[list[AuditEvent], merge_events]
    approval: ApprovalState
    control: ControlState
    response: str
    status: str
    validation: dict[str, Any]


def workflow_thread_id(session_id: str, run_id: str) -> str:
    """Return a stable, scoped checkpoint thread identifier."""
    return f"{str(session_id or '').strip()}:{str(run_id or '').strip()}"


def initial_workflow_state(
    session_id: str,
    run_id: str,
    message: str,
    *,
    history: list[dict[str, Any]] | None = None,
    knowledge_policy: str = "auto",
    control: Mapping[str, Any] | None = None,
) -> WorkflowState:
    """Create trusted state; runtime-owned fields never come from a model response."""
    defaults = {
        "max_parallel_tasks": 4,
        "max_task_attempts": 2,
        "max_replans": 1,
        "replans": 0,
        "max_tasks": 12,
    }
    defaults.update(dict(control or {}))
    return {
        "schema_version": WORKFLOW_STATE_SCHEMA_VERSION,
        "request": {
            "session_id": str(session_id or "").strip(),
            "run_id": str(run_id or "").strip(),
            "message": str(message or "").strip(),
            "history": deepcopy(history or []),
            "knowledge_policy": str(knowledge_policy or "auto").strip().lower() or "auto",
        },
        "route": {},
        "plan": {"version": 0, "tasks": [], "validated": False, "batch_id": 0},
        "messages": [],
        "task_results": {},
        "research": {"evidence": []},
        "artifacts": {},
        "errors": [],
        "events": [],
        "approval": {},
        "control": defaults,
        "response": "",
        "status": "running",
        "validation": {},
    }
