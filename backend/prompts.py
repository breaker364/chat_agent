from __future__ import annotations

from pathlib import Path


PROMPTS_DIR = Path(__file__).resolve().parent / "prompts"
SYSTEM_PROMPT_PATH = PROMPTS_DIR / "system_prompt.md"
AGENT_POLICY_PATH = PROMPTS_DIR / "agent_policy.md"


def load_system_prompt() -> str:
    if SYSTEM_PROMPT_PATH.exists():
        return SYSTEM_PROMPT_PATH.read_text(encoding="utf-8")
    raise FileNotFoundError(f"System prompt file not found: {SYSTEM_PROMPT_PATH}")


def load_agent_policy() -> str:
    if AGENT_POLICY_PATH.exists():
        return AGENT_POLICY_PATH.read_text(encoding="utf-8")
    return ""
