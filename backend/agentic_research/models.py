from __future__ import annotations

from dataclasses import dataclass, field
import time
from typing import Any, Callable, Mapping


KNOWLEDGE_POLICIES = frozenset({"auto", "required", "disabled"})
SOURCE_KINDS = frozenset({"direct", "personal_knowledge", "workspace", "web"})
FRESHNESS_NEEDS = frozenset({"none", "stable", "current"})
ASSESSMENT_OUTCOMES = frozenset({
    "answer_ready",
    "refine_same_source",
    "try_next_source",
    "report_conflict",
    "evidence_gap",
})
_SENSITIVE_METADATA_KEYS = {
    "raw",
    "content",
    "body",
    "cookie",
    "authorization",
    "embedding",
    "api_key",
    "access_key",
    "private_key",
}
_SENSITIVE_METADATA_MARKERS = ("secret", "password", "credential", "access_token", "refresh_token")


class ResearchPolicyError(ValueError):
    """Raised when a request contains an invalid or contradictory policy."""


class ResearchPlanError(ValueError):
    """Raised when an LLM or caller returns an unsafe research plan."""


def _sanitize_metadata(value: Any, *, depth: int = 0) -> Any:
    if depth >= 3:
        return None
    if isinstance(value, Mapping):
        cleaned: dict[str, Any] = {}
        for key, item in value.items():
            normalized_key = str(key or "").strip()[:80]
            lowered = normalized_key.lower()
            if not normalized_key or lowered in _SENSITIVE_METADATA_KEYS:
                continue
            if any(marker in lowered for marker in _SENSITIVE_METADATA_MARKERS):
                continue
            sanitized = _sanitize_metadata(item, depth=depth + 1)
            if sanitized is not None:
                cleaned[normalized_key] = sanitized
        return cleaned
    if isinstance(value, (list, tuple)):
        return [
            sanitized
            for item in value[:8]
            if (sanitized := _sanitize_metadata(item, depth=depth + 1)) is not None
        ]
    if isinstance(value, str):
        return value[:240]
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return str(value)[:240]


def resolve_knowledge_policy(
    *,
    knowledge_policy: str | None = None,
    knowledge_mode: bool | None = None,
) -> str:
    explicit = str(knowledge_policy or "").strip().lower()
    if explicit and explicit not in KNOWLEDGE_POLICIES:
        raise ResearchPolicyError(
            "knowledge_policy must be one of: auto, required, disabled"
        )
    if knowledge_mode is not None and not isinstance(knowledge_mode, bool):
        raise ResearchPolicyError("knowledge_mode must be a boolean when provided")
    legacy_policy = "required" if knowledge_mode is True else "auto"
    if explicit and knowledge_mode is not None and explicit != legacy_policy:
        raise ResearchPolicyError(
            "knowledge_policy conflicts with the legacy knowledge_mode value"
        )
    return explicit or legacy_policy


@dataclass(frozen=True)
class ResearchPlan:
    requires_evidence: bool
    source_sequence: tuple[str, ...]
    query_or_scope: str = ""
    freshness_need: str = "none"
    success_criteria: tuple[str, ...] = ()
    rationale_category: str = ""

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "ResearchPlan":
        if not isinstance(payload, Mapping):
            raise ResearchPlanError("research plan must be an object")
        requires_evidence = payload.get("requires_evidence")
        if not isinstance(requires_evidence, bool):
            raise ResearchPlanError("requires_evidence must be a boolean")
        raw_sequence = payload.get("source_sequence", [])
        if not isinstance(raw_sequence, (list, tuple)):
            raise ResearchPlanError("source_sequence must be a list")
        sequence: list[str] = []
        for raw_kind in raw_sequence:
            kind = str(raw_kind or "").strip().lower()
            if kind not in SOURCE_KINDS:
                raise ResearchPlanError(f"unsupported research source kind: {kind or '<empty>'}")
            if kind not in sequence:
                sequence.append(kind)
        if requires_evidence and not sequence:
            raise ResearchPlanError("an evidence plan must select at least one source")
        if "direct" in sequence and len(sequence) != 1:
            raise ResearchPlanError("direct cannot be combined with evidence sources")
        if not requires_evidence and sequence not in ([], ["direct"]):
            raise ResearchPlanError("a no-evidence plan may only select direct")
        query_or_scope = str(payload.get("query_or_scope") or "").strip()
        if len(query_or_scope) > 2048:
            raise ResearchPlanError("query_or_scope exceeds the 2048 character limit")
        freshness_need = str(payload.get("freshness_need") or "none").strip().lower()
        if freshness_need not in FRESHNESS_NEEDS:
            raise ResearchPlanError("freshness_need must be none, stable, or current")
        raw_criteria = payload.get("success_criteria", [])
        if not isinstance(raw_criteria, (list, tuple)):
            raise ResearchPlanError("success_criteria must be a list")
        if len(raw_criteria) > 8:
            raise ResearchPlanError("success_criteria contains too many items")
        criteria = tuple(str(item or "").strip()[:200] for item in raw_criteria)
        if any(not item for item in criteria):
            raise ResearchPlanError("success_criteria items must be non-empty")
        rationale_category = str(payload.get("rationale_category") or "").strip()[:80]
        return cls(
            requires_evidence=requires_evidence,
            source_sequence=tuple(sequence),
            query_or_scope=query_or_scope,
            freshness_need=freshness_need,
            success_criteria=criteria,
            rationale_category=rationale_category,
        )


@dataclass
class EvidenceObservation:
    source_kind: str
    status: str = "ok"
    citations: list[dict[str, Any]] = field(default_factory=list)
    excerpts: list[str] = field(default_factory=list)
    query: str = ""
    coverage: list[str] = field(default_factory=list)
    freshness: str = ""
    error_category: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    citation_limit: int = field(default=32, repr=False)
    excerpt_limit: int = field(default=1200, repr=False)
    excerpt_count_limit: int = field(default=16, repr=False)
    raw_payload: Any = field(default=None, repr=False)

    def __post_init__(self) -> None:
        self.source_kind = str(self.source_kind or "").strip().lower()
        if self.source_kind not in SOURCE_KINDS - {"direct"}:
            raise ValueError(f"invalid evidence source kind: {self.source_kind}")
        self.status = str(self.status or "ok").strip().lower()[:40]
        self.query = str(self.query or "")[:2048]
        self.citation_limit = max(1, min(int(self.citation_limit), 32))
        self.excerpt_limit = max(128, min(int(self.excerpt_limit), 4000))
        self.excerpt_count_limit = max(1, min(int(self.excerpt_count_limit), 16))
        self.citations = [
            {
                "id": str(item.get("id") or item.get("citation_id") or "")[:160],
                "title": str(item.get("title") or "")[:240],
                "uri": str(item.get("uri") or item.get("url") or item.get("source_uri") or "")[:1000],
            }
            for item in self.citations
            if isinstance(item, Mapping)
        ][: self.citation_limit]
        self.excerpts = [
            str(item or "")[: self.excerpt_limit]
            for item in self.excerpts[: self.excerpt_count_limit]
            if str(item or "").strip()
        ]
        self.coverage = [str(item or "")[:160] for item in self.coverage[:16] if str(item or "").strip()]
        self.metadata = _sanitize_metadata(self.metadata) or {}

    def to_context(self) -> dict[str, Any]:
        return {
            "source_kind": self.source_kind,
            "status": self.status,
            "citations": list(self.citations),
            "excerpts": list(self.excerpts),
            "query": self.query,
            "coverage": list(self.coverage),
            "freshness": self.freshness,
            "error_category": self.error_category,
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True)
class EvidenceAssessment:
    outcome: str
    usable: bool = False
    missing_facets: tuple[str, ...] = ()
    next_query: str = ""
    conflict_sources: tuple[str, ...] = ()
    reason_category: str = ""

    def __post_init__(self) -> None:
        outcome = str(self.outcome or "").strip().lower()
        if outcome not in ASSESSMENT_OUTCOMES:
            raise ValueError(f"invalid assessment outcome: {outcome}")
        object.__setattr__(self, "outcome", outcome)
        object.__setattr__(self, "usable", bool(self.usable))
        object.__setattr__(
            self,
            "missing_facets",
            tuple(str(item or "").strip()[:160] for item in self.missing_facets[:8] if str(item or "").strip()),
        )
        next_query = str(self.next_query or "").strip()
        if len(next_query) > 2048:
            raise ValueError("assessment next_query exceeds the 2048 character limit")
        object.__setattr__(self, "next_query", next_query)
        object.__setattr__(
            self,
            "conflict_sources",
            tuple(
                source
                for source in (str(item or "").strip().lower() for item in self.conflict_sources[:4])
                if source in SOURCE_KINDS - {"direct"}
            ),
        )
        object.__setattr__(self, "reason_category", str(self.reason_category or "").strip()[:80])

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "EvidenceAssessment":
        if not isinstance(payload, Mapping):
            raise ValueError("evidence assessment must be an object")
        raw_missing = payload.get("missing_facets", [])
        raw_conflicts = payload.get("conflict_sources", [])
        if not isinstance(raw_missing, (list, tuple)) or not isinstance(raw_conflicts, (list, tuple)):
            raise ValueError("assessment list fields must be lists")
        usable = payload.get("usable", False)
        if not isinstance(usable, bool):
            raise ValueError("assessment usable must be a boolean")
        return cls(
            outcome=str(payload.get("outcome") or ""),
            usable=usable,
            missing_facets=tuple(raw_missing),
            next_query=str(payload.get("next_query") or ""),
            conflict_sources=tuple(raw_conflicts),
            reason_category=str(payload.get("reason_category") or ""),
        )


@dataclass
class ResearchBudget:
    max_route_transitions: int = 3
    max_source_calls: dict[str, int] = field(default_factory=lambda: {
        "personal_knowledge": 1,
        "workspace": 2,
        "web": 2,
    })
    deadline_seconds: float = 30.0
    clock: Callable[[], float] = time.monotonic
    route_transitions_used: int = 0
    calls: dict[str, int] = field(default_factory=dict)
    started_at: float = field(init=False)

    def __post_init__(self) -> None:
        self.max_route_transitions = max(0, int(self.max_route_transitions))
        self.max_source_calls = {
            str(key): max(0, int(value))
            for key, value in self.max_source_calls.items()
        }
        self.deadline_seconds = max(0.0, float(self.deadline_seconds))
        self.started_at = self.clock()

    def can_call(self, source_kind: str) -> bool:
        if self.route_transitions_used >= self.max_route_transitions:
            return False
        if self.clock() - self.started_at >= self.deadline_seconds:
            return False
        limit = self.max_source_calls.get(source_kind, 0)
        return self.calls.get(source_kind, 0) < limit

    def consume(self, source_kind: str) -> bool:
        if not self.can_call(source_kind):
            return False
        self.route_transitions_used += 1
        self.calls[source_kind] = self.calls.get(source_kind, 0) + 1
        return True

    def to_dict(self) -> dict[str, Any]:
        return {
            "route_transitions_used": self.route_transitions_used,
            "route_transitions_limit": self.max_route_transitions,
            "calls": dict(self.calls),
            "deadline_seconds": round(self.deadline_seconds, 3),
        }

    def for_request(self) -> "ResearchBudget":
        """Create a request-local budget with the same immutable limits."""
        return ResearchBudget(
            max_route_transitions=self.max_route_transitions,
            max_source_calls=dict(self.max_source_calls),
            deadline_seconds=self.deadline_seconds,
            clock=self.clock,
        )


@dataclass
class ResearchTrace:
    policy: str
    outcome: str = "running"
    sources_attempted: list[str] = field(default_factory=list)
    attempts: dict[str, int] = field(default_factory=dict)
    citation_counts: dict[str, int] = field(default_factory=dict)
    budget: dict[str, Any] = field(default_factory=dict)
    can_continue: bool = True

    def record(self, observation: EvidenceObservation) -> None:
        if not self.can_continue:
            return
        source = observation.source_kind
        if source not in self.sources_attempted:
            self.sources_attempted.append(source)
        self.attempts[source] = self.attempts.get(source, 0) + 1
        self.citation_counts[source] = self.citation_counts.get(source, 0) + len(observation.citations)

    def finish(self, outcome: str, *, budget: ResearchBudget | None = None) -> None:
        self.outcome = str(outcome or "evidence_gap")[:40]
        self.can_continue = False
        if budget is not None:
            self.budget = budget.to_dict()

    def to_dict(self) -> dict[str, Any]:
        return {
            "policy": self.policy,
            "outcome": self.outcome,
            "sources_attempted": list(self.sources_attempted),
            "attempts": dict(self.attempts),
            "budget": dict(self.budget),
            "citation_counts": dict(self.citation_counts),
        }
