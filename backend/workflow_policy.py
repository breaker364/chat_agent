"""Schema-bounded route and task-plan policy for the workflow graph."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator


ROUTE_MODES = frozenset({"direct", "planned", "research", "clarify"})
TASK_KINDS = frozenset({"execute", "research"})
RISK_LEVELS = frozenset({"low", "medium", "high"})


class RoutePolicyError(ValueError):
    """Raised when a route cannot be safely enforced."""


class PlanPolicyError(ValueError):
    """Raised when a task DAG is invalid or outside configured limits."""


class RouteProposal(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    mode: Literal["direct", "planned", "research", "clarify"]
    task_kinds: list[Literal["execute", "research"]] = Field(default_factory=list, max_length=8)
    needs_research: bool = False
    risk_level: Literal["low", "medium", "high"] = "low"
    confidence: float = Field(ge=0.0, le=1.0)
    required_capabilities: list[str] = Field(default_factory=list, max_length=8)

    @field_validator("required_capabilities")
    @classmethod
    def normalize_capabilities(cls, values: list[str]) -> list[str]:
        normalized: list[str] = []
        for value in values:
            capability = str(value or "").strip().lower()
            if not capability or len(capability) > 80:
                raise ValueError("capabilities must be non-empty bounded identifiers")
            if capability not in normalized:
                normalized.append(capability)
        return normalized


class RouteDecision(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    mode: Literal["direct", "planned", "research", "clarify"]
    required_capabilities: list[str] = Field(default_factory=list)
    risk_level: Literal["low", "medium", "high"] = "low"
    confidence: float = Field(ge=0.0, le=1.0)
    approval_required: bool = False
    reason: str = ""


class TaskSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    task_id: str = Field(min_length=1, max_length=120)
    kind: Literal["execute", "research"]
    depends_on: list[str] = Field(default_factory=list, max_length=16)
    capabilities: list[str] = Field(default_factory=list, max_length=8)
    max_attempts: int = Field(default=2, ge=1, le=8)
    context: str = Field(default="", max_length=4000)

    @field_validator("task_id")
    @classmethod
    def normalize_task_id(cls, value: str) -> str:
        normalized = str(value or "").strip()
        if not normalized:
            raise ValueError("task_id is required")
        return normalized

    @field_validator("depends_on", "capabilities")
    @classmethod
    def normalize_identifiers(cls, values: list[str]) -> list[str]:
        normalized: list[str] = []
        for value in values:
            identifier = str(value or "").strip().lower()
            if not identifier or len(identifier) > 120:
                raise ValueError("identifiers must be non-empty and bounded")
            if identifier not in normalized:
                normalized.append(identifier)
        return normalized


def parse_route_proposal(value: Any) -> RouteProposal:
    try:
        return value if isinstance(value, RouteProposal) else RouteProposal.model_validate(value)
    except ValidationError as exc:
        raise RoutePolicyError("route proposal does not match the allowed schema") from exc


def enforce_route(
    proposal: RouteProposal | Mapping[str, Any],
    *,
    request: str,
    capabilities: set[str] | frozenset[str],
    minimum_confidence: float = 0.5,
) -> RouteDecision:
    """Apply deterministic resource and capability policy to a model proposal."""
    parsed = parse_route_proposal(proposal)
    available = {str(item).strip().lower() for item in capabilities if str(item).strip()}
    missing = sorted(set(parsed.required_capabilities) - available)
    if missing:
        raise RoutePolicyError("route requests unregistered capabilities")
    if not str(request or "").strip():
        return RouteDecision(mode="clarify", confidence=parsed.confidence, reason="empty_request")
    if parsed.confidence < minimum_confidence:
        return RouteDecision(
            mode="clarify",
            required_capabilities=parsed.required_capabilities,
            risk_level=parsed.risk_level,
            confidence=parsed.confidence,
            reason="low_confidence",
        )
    mode = "research" if parsed.needs_research and parsed.mode != "clarify" else parsed.mode
    if mode == "direct" and (len(parsed.task_kinds) > 1 or parsed.needs_research):
        mode = "planned"
    return RouteDecision(
        mode=mode,
        required_capabilities=parsed.required_capabilities,
        risk_level=parsed.risk_level,
        confidence=parsed.confidence,
        approval_required=parsed.risk_level == "high",
        reason="policy_accepted",
    )


def _assert_acyclic(tasks: dict[str, TaskSpec]) -> None:
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(task_id: str) -> None:
        if task_id in visited:
            return
        if task_id in visiting:
            raise PlanPolicyError("task plan contains a dependency cycle")
        visiting.add(task_id)
        for dependency in tasks[task_id].depends_on:
            visit(dependency)
        visiting.remove(task_id)
        visited.add(task_id)

    for task_id in tasks:
        visit(task_id)


def assert_route_scoped_capabilities(
    tasks: Sequence[TaskSpec],
    route_required: Sequence[str] | set[str] | frozenset[str],
) -> None:
    """Ensure every task capability stays inside the route-approved set."""
    approved = {str(item or "").strip().lower() for item in (route_required or ()) if str(item or "").strip()}
    for task in tasks:
        outside = sorted(set(task.capabilities) - approved)
        if outside:
            raise PlanPolicyError(
                "capability_outside_route: task `"
                + task.task_id
                + "` requests capabilities outside the route approval: "
                + ", ".join(outside)
            )


def validate_plan(
    tasks: Sequence[TaskSpec | Mapping[str, Any]],
    *,
    capabilities: set[str] | frozenset[str],
    max_tasks: int = 12,
) -> list[TaskSpec]:
    """Normalize and validate a bounded dependency DAG before scheduling it."""
    if not isinstance(tasks, Sequence) or isinstance(tasks, (str, bytes)):
        raise PlanPolicyError("task plan must be a list")
    if not tasks:
        raise PlanPolicyError("task plan cannot be empty")
    if len(tasks) > max_tasks:
        raise PlanPolicyError("task plan exceeds the configured task limit")
    parsed: list[TaskSpec] = []
    try:
        parsed = [task if isinstance(task, TaskSpec) else TaskSpec.model_validate(task) for task in tasks]
    except ValidationError as exc:
        raise PlanPolicyError("task plan contains an invalid task") from exc
    by_id = {task.task_id: task for task in parsed}
    if len(by_id) != len(parsed):
        raise PlanPolicyError("task plan contains duplicate task IDs")
    available = {str(item).strip().lower() for item in capabilities if str(item).strip()}
    for task in parsed:
        unknown_dependencies = set(task.depends_on) - set(by_id)
        if unknown_dependencies:
            raise PlanPolicyError("task plan references a missing dependency")
        if task.task_id.lower() in task.depends_on:
            raise PlanPolicyError("task cannot depend on itself")
        if set(task.capabilities) - available:
            raise PlanPolicyError("task plan requests unregistered capabilities")
    _assert_acyclic(by_id)
    return parsed
