from __future__ import annotations

from .subagents import AsyncSubagentManager

_SUBAGENT_MANAGER = AsyncSubagentManager()


def get_subagent_manager() -> AsyncSubagentManager:
    return _SUBAGENT_MANAGER
