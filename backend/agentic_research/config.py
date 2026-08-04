from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from ..config import load_runtime_config


DEFAULT_SOURCE_CALL_LIMITS = {
    "personal_knowledge": 1,
    "workspace": 2,
    "web": 2,
}
_ALLOWED_KEYS = {
    "enabled",
    "max_route_transitions",
    "source_call_limits",
    "deadline_seconds",
    "result_limit",
    "excerpt_char_limit",
}
_SENSITIVE_KEY_MARKERS = ("secret", "token", "authorization", "cookie", "password", "api_key")


class AgenticResearchConfigError(ValueError):
    """Raised for unsafe or unsupported agentic-research configuration."""


@dataclass(frozen=True)
class AgenticResearchConfig:
    enabled: bool = False
    max_route_transitions: int = 3
    source_call_limits: dict[str, int] | None = None
    deadline_seconds: float = 30.0
    result_limit: int = 8
    excerpt_char_limit: int = 1200

    def __post_init__(self) -> None:
        object.__setattr__(self, "source_call_limits", dict(self.source_call_limits or DEFAULT_SOURCE_CALL_LIMITS))


def _positive_int(raw: Mapping[str, Any], key: str, default: int, *, maximum: int) -> int:
    value = raw.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= maximum:
        raise AgenticResearchConfigError(f"{key} must be an integer between 1 and {maximum}")
    return value


def _positive_float(raw: Mapping[str, Any], key: str, default: float, *, maximum: float) -> float:
    value = raw.get(key, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 < float(value) <= maximum:
        raise AgenticResearchConfigError(f"{key} must be a number between 0 and {maximum}")
    return float(value)


def load_agentic_research_config(raw: Mapping[str, Any] | None = None) -> AgenticResearchConfig:
    """Load bounded research settings from a supplied section or runtime config."""
    if raw is None:
        runtime = load_runtime_config()
        rag = runtime.get("rag") if isinstance(runtime, Mapping) else None
        raw = rag.get("agentic_research") if isinstance(rag, Mapping) else {}
    if not isinstance(raw, Mapping):
        raise AgenticResearchConfigError("agentic_research configuration must be an object")
    for key in raw:
        normalized = str(key).strip().lower()
        if any(marker in normalized for marker in _SENSITIVE_KEY_MARKERS):
            raise AgenticResearchConfigError("agentic_research configuration cannot contain credentials")
        if key not in _ALLOWED_KEYS:
            raise AgenticResearchConfigError(f"unsupported agentic_research configuration key: {key}")

    enabled = raw.get("enabled", False)
    if not isinstance(enabled, bool):
        raise AgenticResearchConfigError("enabled must be a boolean")
    limits_raw = raw.get("source_call_limits", DEFAULT_SOURCE_CALL_LIMITS)
    if not isinstance(limits_raw, Mapping):
        raise AgenticResearchConfigError("source_call_limits must be an object")
    unknown_sources = set(limits_raw) - set(DEFAULT_SOURCE_CALL_LIMITS)
    if unknown_sources:
        raise AgenticResearchConfigError("source_call_limits contains an unsupported source kind")
    source_call_limits = dict(DEFAULT_SOURCE_CALL_LIMITS)
    for source_kind, default in DEFAULT_SOURCE_CALL_LIMITS.items():
        value = limits_raw.get(source_kind, default)
        if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 8:
            raise AgenticResearchConfigError(f"source_call_limits.{source_kind} must be an integer between 1 and 8")
        source_call_limits[source_kind] = value

    return AgenticResearchConfig(
        enabled=enabled,
        max_route_transitions=_positive_int(raw, "max_route_transitions", 3, maximum=8),
        source_call_limits=source_call_limits,
        deadline_seconds=_positive_float(raw, "deadline_seconds", 30.0, maximum=180.0),
        result_limit=_positive_int(raw, "result_limit", 8, maximum=20),
        excerpt_char_limit=_positive_int(raw, "excerpt_char_limit", 1200, maximum=4000),
    )
