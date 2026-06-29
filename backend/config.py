from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config.json"
LEGACY_LLM_CONFIG_PATH = Path(
    r"D:\NoobhekProject\cae-fusion-demo\code\LLM_tools\llm_provider_deepseek.json"
)
DEFAULT_TAVILY_BASE_URL = "https://api.tavily.com"
DEFAULT_MCD_MCP_URL = "https://mcp.mcd.cn"


def _resolve_config_path(config_path: str | Path | None = None) -> Path:
    if config_path:
        return Path(config_path).expanduser().resolve()
    if DEFAULT_CONFIG_PATH.exists():
        return DEFAULT_CONFIG_PATH.resolve()
    return LEGACY_LLM_CONFIG_PATH.resolve()


def load_app_config(config_path: str | Path | None = None) -> dict[str, Any]:
    """Load the local app config JSON, falling back to the legacy LLM config file."""
    resolved = _resolve_config_path(config_path)
    if not resolved.exists():
        raise FileNotFoundError(f"Config file not found: {resolved}")
    raw = resolved.read_text(encoding="utf-8")
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError(f"Config file must contain a JSON object: {resolved}")
    data["config_path"] = str(resolved)
    return data


def load_llm_config(config_path: str | Path | None = None) -> dict[str, str]:
    """Load LLM provider configuration from JSON.

    Required keys: `base_url`, `api_key`, `model`.
    """
    data = load_app_config(config_path)
    for key in ("base_url", "api_key", "model"):
        if key not in data or not str(data.get(key, "")).strip():
            raise ValueError(f"Missing or empty '{key}' in {data['config_path']}")
    return {
        "base_url": str(data["base_url"]).rstrip("/"),
        "api_key": str(data["api_key"]),
        "model": str(data["model"]),
        "config_path": str(data["config_path"]),
    }


def load_tavily_config(config_path: str | Path | None = None) -> dict[str, str | None]:
    """Load Tavily configuration from env or config JSON."""
    data = load_app_config(config_path)
    base_url = (
        os.environ.get("TAVILY_BASE_URL")
        or os.environ.get("TAVILY_ENDPOINT_URL")
        or str(data.get("tavily_base_url", "")).strip()
        or DEFAULT_TAVILY_BASE_URL
    ).rstrip("/")
    api_key = (
        os.environ.get("TAVILY_API_KEY")
        or str(data.get("tavily_api_key", "")).strip()
        or None
    )
    return {
        "base_url": base_url,
        "api_key": api_key,
        "config_path": str(data["config_path"]),
    }


def load_mcd_mcp_config(config_path: str | Path | None = None) -> dict[str, str | None]:
    """Load McDonald's MCP configuration from env or config JSON."""
    data = load_app_config(config_path)
    url = (
        os.environ.get("MCD_MCP_URL")
        or str(data.get("mcd_mcp_url", "")).strip()
        or DEFAULT_MCD_MCP_URL
    ).rstrip("/")
    token = (
        os.environ.get("MCD_MCP_TOKEN")
        or str(data.get("mcd_mcp_token", "")).strip()
        or None
    )
    return {
        "url": url,
        "token": token,
        "config_path": str(data["config_path"]),
    }


def create_chat_deepseek(config: dict[str, Any] | None = None, **overrides: Any) -> Any:
    """Create a ChatDeepSeek instance from config dict + overrides."""
    from langchain_deepseek import ChatDeepSeek

    if config is None:
        config = load_llm_config()
    params = {
        "model": config["model"],
        "api_key": config["api_key"],
        "base_url": config["base_url"],
        "temperature": 0.0,
        "streaming": True,
    }
    params.update(overrides)
    return ChatDeepSeek(**params)
