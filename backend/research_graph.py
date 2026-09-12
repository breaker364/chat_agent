"""LangGraph boundary around the bounded, read-only research runtime."""

from __future__ import annotations

from typing import Annotated, Any, TypedDict

from .workflow_compat import END, START, StateGraph
from .workflow_state import merge_evidence


class ResearchGraphState(TypedDict, total=False):
    request: str
    task: dict[str, Any]
    knowledge_policy: str
    plan: dict[str, Any]
    evidence: Annotated[list[dict[str, Any]], merge_evidence]
    assessment: dict[str, Any]
    status: str
    summary: str
    gaps: list[str]
    reason: str
    budget: dict[str, Any]
    events: list[dict[str, Any]]
    errors: list[dict[str, Any]]


def _event(node: str, state: ResearchGraphState, status: str) -> dict[str, Any]:
    return {
        "graph_path": "workflow.research",
        "node": node,
        "task_id": (state.get("task") or {}).get("task_id", ""),
        "status": status,
    }


def _evidence_records(result: Any) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for observation in list(getattr(result, "observations", []) or []):
        source_kind = str(getattr(observation, "source_kind", "") or "")
        excerpts = list(getattr(observation, "excerpts", []) or [])
        for index, citation in enumerate(list(getattr(observation, "citations", []) or [])):
            if not isinstance(citation, dict):
                continue
            records.append({
                "id": str(citation.get("id") or citation.get("uri") or "").strip(),
                "title": str(citation.get("title") or "").strip(),
                "uri": str(citation.get("uri") or "").strip(),
                "source_kind": source_kind,
                "excerpt": str(excerpts[index] if index < len(excerpts) else "")[:1200],
            })
    return records


def build_research_graph(runtime: Any):
    """Compile a research subgraph that exposes a small, citeable result contract."""
    def research_prepare(state: ResearchGraphState) -> dict[str, Any]:
        request = str(state.get("request") or "").strip()
        if not request:
            return {
                "status": "partial",
                "gaps": ["request"],
                "reason": "empty_request",
                "events": [_event("research_prepare", state, "failed")],
            }
        return {"events": [_event("research_prepare", state, "completed")]}

    def research_plan(state: ResearchGraphState) -> dict[str, Any]:
        return {
            "plan": {"task_id": (state.get("task") or {}).get("task_id", ""), "policy": state.get("knowledge_policy", "auto")},
            "events": [_event("research_plan", state, "completed")],
        }

    def collect_evidence(state: ResearchGraphState) -> dict[str, Any]:
        if state.get("status") == "partial":
            return {}
        try:
            result = runtime.run(str(state.get("request") or ""), knowledge_policy=str(state.get("knowledge_policy") or "auto"))
            trace = getattr(result, "trace", None)
            assessment = getattr(result, "assessment", None)
            assessment_payload = {
                "outcome": str(getattr(assessment, "outcome", "evidence_gap") or "evidence_gap"),
                "usable": bool(getattr(assessment, "usable", False)),
                "missing_facets": list(getattr(assessment, "missing_facets", ()) or ()),
                "reason_category": str(getattr(assessment, "reason_category", "") or ""),
            }
            return {
                "evidence": _evidence_records(result),
                "assessment": assessment_payload,
                "budget": dict(getattr(trace, "budget", {}) or {}),
                "events": [_event("collect_evidence", state, "completed")],
            }
        except Exception as exc:
            return {
                "status": "partial",
                "gaps": ["research_runtime"],
                "reason": type(exc).__name__,
                "errors": [{"category": "research_runtime_error", "message": type(exc).__name__}],
                "events": [_event("collect_evidence", state, "failed")],
            }

    def assess_evidence(state: ResearchGraphState) -> dict[str, Any]:
        if state.get("status") == "partial":
            return {"events": [_event("assess_evidence", state, "partial")]}
        assessment = state.get("assessment") or {}
        if assessment.get("outcome") == "answer_ready" and assessment.get("usable") and state.get("evidence"):
            return {"status": "completed", "events": [_event("assess_evidence", state, "completed")]}
        return {
            "status": "partial",
            "gaps": list(assessment.get("missing_facets") or ["sufficient_evidence"]),
            "reason": str(assessment.get("reason_category") or "evidence_gap"),
            "events": [_event("assess_evidence", state, "partial")],
        }

    def research_finalize(state: ResearchGraphState) -> dict[str, Any]:
        summary = "\n".join(
            str(item.get("excerpt") or "").strip()
            for item in state.get("evidence", [])
            if isinstance(item, dict) and str(item.get("excerpt") or "").strip()
        )
        return {
            "summary": summary,
            "events": [_event("research_finalize", state, str(state.get("status") or "partial"))],
        }

    graph = StateGraph(ResearchGraphState)
    graph.add_node("research_prepare", research_prepare)
    graph.add_node("research_plan", research_plan)
    graph.add_node("collect_evidence", collect_evidence)
    graph.add_node("assess_evidence", assess_evidence)
    graph.add_node("research_finalize", research_finalize)
    graph.add_edge(START, "research_prepare")
    graph.add_edge("research_prepare", "research_plan")
    graph.add_edge("research_plan", "collect_evidence")
    graph.add_edge("collect_evidence", "assess_evidence")
    graph.add_edge("assess_evidence", "research_finalize")
    graph.add_edge("research_finalize", END)
    return graph.compile(name="research_subgraph")
