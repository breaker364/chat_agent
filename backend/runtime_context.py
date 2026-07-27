from __future__ import annotations

import os
from contextvars import Context, ContextVar, Token, copy_context


_CURRENT_SESSION_ID_ENV = "CHAT_AGENT_SESSION_ID"
_CURRENT_RUN_ID_ENV = "CHAT_AGENT_RUN_ID"

_SESSION_ID: ContextVar[str] = ContextVar("chat_agent_session_id", default="")
_RUN_ID: ContextVar[str] = ContextVar("chat_agent_run_id", default="")


def bind_runtime_context(session_id: str, run_id: str) -> tuple[Token[str], Token[str]]:
    return (
        _SESSION_ID.set(str(session_id or "").strip()),
        _RUN_ID.set(str(run_id or "").strip()),
    )


def reset_runtime_context(tokens: tuple[Token[str], Token[str]] | None) -> None:
    if not tokens:
        return
    session_token, run_token = tokens
    _SESSION_ID.reset(session_token)
    _RUN_ID.reset(run_token)


def current_session_id() -> str:
    return (_SESSION_ID.get() or os.environ.get(_CURRENT_SESSION_ID_ENV) or "").strip()


def current_run_id() -> str:
    return (_RUN_ID.get() or os.environ.get(_CURRENT_RUN_ID_ENV) or "").strip()


def copy_runtime_context() -> Context:
    return copy_context()
