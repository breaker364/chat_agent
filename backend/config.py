from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent
RUNTIME_CONFIG_PATH = PROJECT_ROOT / "runtime_config.json"
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config.json"
DEFAULT_TAVILY_BASE_URL = "https://api.tavily.com"
DEFAULT_MCD_MCP_URL = "https://mcp.mcd.cn"


def load_runtime_config(config_path: str | Path | None = None) -> dict[str, Any]:
    """Load deploy-time paths, ports, and local runtime settings."""
    resolved = Path(
        config_path
        or os.environ.get("CHAT_AGENT_RUNTIME_CONFIG", "")
        or RUNTIME_CONFIG_PATH
    ).expanduser()
    if not resolved.is_absolute():
        resolved = PROJECT_ROOT / resolved
    if not resolved.exists():
        return {}
    data = json.loads(resolved.read_text(encoding="utf-8"))
    return data if isinstance(data, dict) else {}


def get_runtime_value(section: str, key: str, default: Any = None) -> Any:
    """Return a single runtime config value with environment override support."""
    env_key = f"CHAT_AGENT_{section}_{key}".upper()
    if env_key in os.environ:
        return os.environ[env_key]
    section_data = load_runtime_config().get(section, {})
    if isinstance(section_data, dict) and key in section_data:
        return section_data[key]
    return default


def resolve_runtime_path(value: str | Path | None, default: str | Path) -> Path:
    raw = Path(str(value or default)).expanduser()
    if raw.is_absolute():
        return raw.resolve()
    return (PROJECT_ROOT / raw).resolve()


def _resolve_config_path(config_path: str | Path | None = None) -> Path:
    if config_path:
        return Path(config_path).expanduser().resolve()
    configured = resolve_runtime_path(
        get_runtime_value("paths", "llm_config_path", "config.json"),
        DEFAULT_CONFIG_PATH,
    )
    if configured.exists():
        return configured
    legacy = str(get_runtime_value("paths", "legacy_llm_config_path", "") or "").strip()
    if legacy:
        return Path(legacy).expanduser().resolve()
    return DEFAULT_CONFIG_PATH.resolve()


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


DEFAULT_CONTEXT_COMPACTION_CONFIG: dict[str, Any] = {
    "enabled": True,
    "trigger_remaining_tokens": 20_000,
    "retain_recent_turns": 3,
    "summary_max_output_tokens": 4_096,
    "chunk_target_tokens": 12_000,
    "merge_target_tokens": 12_000,
    "max_retries": 1,
}


DEFAULT_AGENT_MEMORY_CONFIG: dict[str, Any] = {
    "enabled": False,
    "directory": "agent_memory",
    "recent_message_limit": 10,
    "max_index_lines": 200,
    "max_index_bytes": 25 * 1024,
    "max_record_bytes": 16 * 1024,
    "max_scan_records": 200,
    "max_name_length": 120,
    "max_description_length": 600,
    "max_candidates": 8,
    "extraction_timeout_seconds": 30,
}


def load_agent_memory_config(
    config_path: str | Path | None = None,
    *,
    workspace_dir: str | Path | None = None,
    raw: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Load bounded persistent-memory settings for one trusted workspace.

    The directory is intentionally constrained to the supplied workspace.  This
    MVP has no user ownership namespace, so callers must use it only for the
    current trusted workspace.
    """
    data = raw if isinstance(raw, dict) else load_app_config(config_path)
    section = data.get("agent_memory")
    section = section if isinstance(section, dict) else {}
    workspace = Path(workspace_dir or Path.cwd()).expanduser().resolve()

    def positive_int(key: str) -> int:
        default = int(DEFAULT_AGENT_MEMORY_CONFIG[key])
        value = section.get(key, default)
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            return default
        return value

    enabled = section.get("enabled", DEFAULT_AGENT_MEMORY_CONFIG["enabled"])
    if not isinstance(enabled, bool):
        enabled = bool(DEFAULT_AGENT_MEMORY_CONFIG["enabled"])

    configured_directory = section.get("directory", DEFAULT_AGENT_MEMORY_CONFIG["directory"])
    directory_text = str(configured_directory or "").strip()
    candidate = Path(directory_text or str(DEFAULT_AGENT_MEMORY_CONFIG["directory"])).expanduser()
    safe_relative_directory = (
        bool(directory_text or candidate)
        and not candidate.is_absolute()
        and "\x00" not in directory_text
        and candidate != Path(".")
        and ".." not in candidate.parts
    )
    resolved_directory = (workspace / candidate).resolve()
    try:
        if not safe_relative_directory:
            raise ValueError("memory directory must be a nested workspace path")
        resolved_directory.relative_to(workspace)
    except ValueError:
        resolved_directory = (workspace / str(DEFAULT_AGENT_MEMORY_CONFIG["directory"])).resolve()

    return {
        "enabled": enabled,
        "directory": str(resolved_directory),
        "recent_message_limit": positive_int("recent_message_limit"),
        "max_index_lines": positive_int("max_index_lines"),
        "max_index_bytes": positive_int("max_index_bytes"),
        "max_record_bytes": positive_int("max_record_bytes"),
        "max_scan_records": positive_int("max_scan_records"),
        "max_name_length": positive_int("max_name_length"),
        "max_description_length": positive_int("max_description_length"),
        "max_candidates": positive_int("max_candidates"),
        "extraction_timeout_seconds": positive_int("extraction_timeout_seconds"),
    }


def load_context_compaction_config(config_path: str | Path | None = None) -> dict[str, Any]:
    """Load optional context-compaction settings with safe defaults."""
    data = load_app_config(config_path)
    raw = data.get("context_compaction")
    raw = raw if isinstance(raw, dict) else {}

    def positive_int(key: str, *, allow_zero: bool = False) -> int:
        value = raw.get(key, DEFAULT_CONTEXT_COMPACTION_CONFIG[key])
        if isinstance(value, bool) or not isinstance(value, int):
            return int(DEFAULT_CONTEXT_COMPACTION_CONFIG[key])
        minimum = 0 if allow_zero else 1
        return value if value >= minimum else int(DEFAULT_CONTEXT_COMPACTION_CONFIG[key])

    enabled = raw.get("enabled", DEFAULT_CONTEXT_COMPACTION_CONFIG["enabled"])
    if not isinstance(enabled, bool):
        enabled = bool(DEFAULT_CONTEXT_COMPACTION_CONFIG["enabled"])
    return {
        "enabled": enabled,
        "trigger_remaining_tokens": positive_int("trigger_remaining_tokens"),
        "retain_recent_turns": positive_int("retain_recent_turns"),
        "summary_max_output_tokens": positive_int("summary_max_output_tokens"),
        "chunk_target_tokens": positive_int("chunk_target_tokens"),
        "merge_target_tokens": positive_int("merge_target_tokens"),
        "max_retries": positive_int("max_retries", allow_zero=True),
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


def load_vision_config() -> dict[str, Any]:
    """Load an OpenAI-compatible multimodal model configuration."""
    data = load_app_config()
    vision = data.get("vision", {})
    if not isinstance(vision, dict):
        vision = {}
    key_env = str(vision.get("api_key_env") or "VISION_API_KEY").strip()
    enabled_value = os.environ.get("VISION_ENABLED", vision.get("enabled", False))
    enabled = str(enabled_value).strip().lower() in {"1", "true", "yes", "on"}
    return {
        "enabled": enabled,
        "base_url": str(
            os.environ.get("VISION_BASE_URL")
            or vision.get("base_url")
            or ""
        ).rstrip("/"),
        "model": str(
            os.environ.get("VISION_MODEL")
            or vision.get("model")
            or ""
        ).strip(),
        "api_key": (
            os.environ.get(key_env)
            or str(vision.get("api_key") or "").strip()
        ),
        "api_key_env": key_env,
        "max_images_per_request": int(vision.get("max_images_per_request") or 4),
        "max_image_bytes": int(vision.get("max_image_bytes") or 20 * 1024 * 1024),
        "max_output_tokens": int(vision.get("max_output_tokens") or 2048),
        "timeout_seconds": int(vision.get("timeout_seconds") or 60),
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
