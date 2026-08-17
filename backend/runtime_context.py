from __future__ import annotations

import os
from contextvars import Context, ContextVar, Token, copy_context
from typing import Any, Mapping


_CURRENT_SESSION_ID_ENV = "CHAT_AGENT_SESSION_ID"
_CURRENT_RUN_ID_ENV = "CHAT_AGENT_RUN_ID"
_CURRENT_KNOWLEDGE_POLICY_ENV = "CHAT_AGENT_KNOWLEDGE_POLICY"

_SESSION_ID: ContextVar[str] = ContextVar("chat_agent_session_id", default="")
_RUN_ID: ContextVar[str] = ContextVar("chat_agent_run_id", default="")
_KNOWLEDGE_POLICY: ContextVar[str] = ContextVar("chat_agent_knowledge_policy", default="auto")
_SOURCE_CALL_LIMITS: ContextVar[dict[str, int]] = ContextVar("chat_agent_source_call_limits", default={})
_SOURCE_CALL_COUNTS: ContextVar[dict[str, int]] = ContextVar("chat_agent_source_call_counts", default={})
_SOURCE_CALL_COUNTS_BY_RUN: dict[str, dict[str, int]] = {}


def bind_runtime_context(
    session_id: str,
    run_id: str,
    knowledge_policy: str | None = None,
    source_call_limits: Mapping[str, Any] | None = None,
) -> tuple[Token[Any], ...]:
    limits = {
        str(key).strip().lower(): max(0, int(value))
        for key, value in (source_call_limits or {}).items()
        if str(key).strip()
    }
    return (
        _SESSION_ID.set(str(session_id or "").strip()),
        _RUN_ID.set(str(run_id or "").strip()),
        _KNOWLEDGE_POLICY.set(str(knowledge_policy or "auto").strip().lower() or "auto"),
        _SOURCE_CALL_LIMITS.set(limits),
        _SOURCE_CALL_COUNTS.set({}),
    )


def reset_runtime_context(tokens: tuple[Token[str], ...] | None) -> None:
    if not tokens:
        return
    session_token, run_token = tokens[:2]
    run_id = _RUN_ID.get()
    _SESSION_ID.reset(session_token)
    _RUN_ID.reset(run_token)
    if len(tokens) > 2:
        _KNOWLEDGE_POLICY.reset(tokens[2])
    if len(tokens) > 3:
        _SOURCE_CALL_LIMITS.reset(tokens[3])
    if len(tokens) > 4:
        _SOURCE_CALL_COUNTS.reset(tokens[4])
    if run_id:
        _SOURCE_CALL_COUNTS_BY_RUN.pop(run_id, None)


def current_session_id() -> str:
    return (_SESSION_ID.get() or os.environ.get(_CURRENT_SESSION_ID_ENV) or "").strip()


def current_run_id() -> str:
    return (_RUN_ID.get() or os.environ.get(_CURRENT_RUN_ID_ENV) or "").strip()


def current_knowledge_policy() -> str:
    return (
        _KNOWLEDGE_POLICY.get()
        or os.environ.get(_CURRENT_KNOWLEDGE_POLICY_ENV)
        or "auto"
    ).strip().lower()


def source_call_allowed(source_kind: str, defaults: Mapping[str, int] | None = None) -> bool:
    key = str(source_kind or "").strip().lower()
    limits = _SOURCE_CALL_LIMITS.get() or dict(defaults or {})
    if key not in limits:
        return True
    run_id = current_run_id()
    counts = _SOURCE_CALL_COUNTS_BY_RUN.get(run_id, {}) if run_id else _SOURCE_CALL_COUNTS.get()
    return counts.get(key, 0) < max(0, int(limits[key]))


def record_source_call(source_kind: str) -> None:
    key = str(source_kind or "").strip().lower()
    run_id = current_run_id()
    if run_id:
        counts = _SOURCE_CALL_COUNTS_BY_RUN.setdefault(run_id, {})
        counts[key] = counts.get(key, 0) + 1
        return
    counts = _SOURCE_CALL_COUNTS.get()
    if not counts:
        counts = {}
        _SOURCE_CALL_COUNTS.set(counts)
    counts[key] = counts.get(key, 0) + 1


def copy_runtime_context() -> Context:
    return copy_context()
