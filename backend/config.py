from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any, Mapping

PROJECT_ROOT = Path(__file__).resolve().parent.parent
RUNTIME_CONFIG_PATH = PROJECT_ROOT / "runtime_config.json"
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config.json"
DEFAULT_TAVILY_BASE_URL = "https://api.tavily.com"
DEFAULT_MCD_MCP_URL = "https://mcp.mcd.cn"


DEFAULT_SHELL_TOOLS_CONFIG: dict[str, Any] = {
    "enabled": True,
    "bash_enabled": False,
    "bash_executable": "",
    "environment_allowlist": [],
    "default_timeout_seconds": 30,
    "max_timeout_seconds": 120,
    "max_output_chars": 12_000,
    "max_glob_matches": 200,
    "max_grep_matches": 200,
    "max_file_bytes": 1 * 1024 * 1024,
    "max_pattern_chars": 4096,
    "max_scope_chars": 2048,
    "max_command_chars": 8192,
}


DEFAULT_WORKFLOW_CONFIG: dict[str, Any] = {
    "enabled": False,
    "max_tasks": 12,
    "max_parallel_tasks": 4,
    "max_task_attempts": 2,
    "max_replans": 1,
    "executor_max_steps": 8,
    "checkpoint_backend": "memory",
    "tool_capabilities": {},
}


class WorkflowConfigError(ValueError):
    """Raised when workflow orchestration configuration is invalid."""


class WorkflowConfig(dict[str, Any]):
    """Mapping-compatible validated orchestration settings."""


def load_workflow_config(raw: Mapping[str, Any] | None = None) -> WorkflowConfig:
    """Load bounded, deployment-owned settings for the opt-in workflow runtime."""
    if raw is None:
        runtime = load_runtime_config()
        raw = runtime.get("workflow") if isinstance(runtime, Mapping) else {}
    elif isinstance(raw, Mapping) and isinstance(raw.get("workflow"), Mapping):
        raw = raw["workflow"]
    if not isinstance(raw, Mapping):
        raise WorkflowConfigError("workflow configuration must be an object")
    unknown = set(raw) - set(DEFAULT_WORKFLOW_CONFIG)
    if unknown:
        raise WorkflowConfigError("workflow configuration contains unsupported keys")
    enabled = raw.get("enabled", DEFAULT_WORKFLOW_CONFIG["enabled"])
    if not isinstance(enabled, bool):
        raise WorkflowConfigError("workflow.enabled must be a boolean")

    def bounded(key: str, minimum: int, maximum: int) -> int:
        value = raw.get(key, DEFAULT_WORKFLOW_CONFIG[key])
        if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
            raise WorkflowConfigError(f"workflow.{key} must be an integer between {minimum} and {maximum}")
        return value

    backend = str(raw.get("checkpoint_backend", DEFAULT_WORKFLOW_CONFIG["checkpoint_backend"]) or "").strip().lower()
    if backend != "memory":
        raise WorkflowConfigError("workflow.checkpoint_backend is unsupported")
    raw_tool_capabilities = raw.get("tool_capabilities", DEFAULT_WORKFLOW_CONFIG["tool_capabilities"])
    if not isinstance(raw_tool_capabilities, Mapping) or len(raw_tool_capabilities) > 256:
        raise WorkflowConfigError("workflow.tool_capabilities must be a bounded object")
    tool_capabilities: dict[str, list[str]] = {}
    for raw_tool_name, raw_capabilities in raw_tool_capabilities.items():
        tool_name = str(raw_tool_name or "").strip()
        if not tool_name or len(tool_name) > 160:
            raise WorkflowConfigError("workflow.tool_capabilities contains an invalid tool name")
        if not isinstance(raw_capabilities, (list, tuple)) or len(raw_capabilities) > 16:
            raise WorkflowConfigError("workflow.tool_capabilities values must be bounded lists")
        normalized_capabilities: list[str] = []
        for raw_capability in raw_capabilities:
            capability = str(raw_capability or "").strip().lower()
            if not re.fullmatch(r"[a-z][a-z0-9_.-]{0,79}", capability):
                raise WorkflowConfigError("workflow.tool_capabilities contains an invalid capability")
            if capability not in normalized_capabilities:
                normalized_capabilities.append(capability)
        tool_capabilities[tool_name] = normalized_capabilities
    return WorkflowConfig(
        enabled=enabled,
        max_tasks=bounded("max_tasks", 1, 64),
        max_parallel_tasks=bounded("max_parallel_tasks", 1, 16),
        max_task_attempts=bounded("max_task_attempts", 1, 8),
        max_replans=bounded("max_replans", 0, 8),
        executor_max_steps=bounded("executor_max_steps", 1, 64),
        checkpoint_backend=backend,
        tool_capabilities=tool_capabilities,
    )


class ShellToolsConfigError(ValueError):
    """Raised when shell-tool configuration is unsafe or unsupported."""


_SHELL_TOOLS_ALLOWED_KEYS = frozenset(DEFAULT_SHELL_TOOLS_CONFIG)
_SHELL_TOOLS_SENSITIVE_MARKERS = (
    "secret",
    "token",
    "password",
    "credential",
    "authorization",
    "cookie",
    "api_key",
    "access_key",
)


def load_shell_tools_config(raw: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Load validated, bounded settings for the generic workspace shell tools."""
    if raw is None:
        runtime = load_runtime_config()
        section = runtime.get("shell_tools") if isinstance(runtime, Mapping) else None
        raw = section if isinstance(section, Mapping) else {}
    elif isinstance(raw, Mapping) and isinstance(raw.get("shell_tools"), Mapping):
        raw = raw["shell_tools"]
    if not isinstance(raw, Mapping):
        raise ShellToolsConfigError("shell_tools configuration must be an object")

    for key in raw:
        normalized = str(key).strip().lower()
        if any(marker in normalized for marker in _SHELL_TOOLS_SENSITIVE_MARKERS):
            raise ShellToolsConfigError("shell_tools configuration cannot contain credentials")
        if key not in _SHELL_TOOLS_ALLOWED_KEYS:
            raise ShellToolsConfigError(f"unsupported shell_tools configuration key: {key}")

    def bounded_int(key: str, minimum: int, maximum: int) -> int:
        value = raw.get(key, DEFAULT_SHELL_TOOLS_CONFIG[key])
        if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
            raise ShellToolsConfigError(f"{key} must be an integer between {minimum} and {maximum}")
        return value

    enabled = raw.get("enabled", DEFAULT_SHELL_TOOLS_CONFIG["enabled"])
    bash_enabled = raw.get("bash_enabled", DEFAULT_SHELL_TOOLS_CONFIG["bash_enabled"])
    if not isinstance(enabled, bool) or not isinstance(bash_enabled, bool):
        raise ShellToolsConfigError("enabled and bash_enabled must be booleans")

    executable = str(raw.get("bash_executable", DEFAULT_SHELL_TOOLS_CONFIG["bash_executable"]) or "").strip()
    if len(executable) > 4096 or "\x00" in executable:
        raise ShellToolsConfigError("bash_executable is invalid or too long")

    raw_environment = raw.get("environment_allowlist", DEFAULT_SHELL_TOOLS_CONFIG["environment_allowlist"])
    if not isinstance(raw_environment, (list, tuple, set)):
        raise ShellToolsConfigError("environment_allowlist must be a list")
    environment_allowlist: list[str] = []
    for item in raw_environment:
        name = str(item or "").strip()
        lowered = name.lower()
        if not name or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name) or len(name) > 80:
            raise ShellToolsConfigError("environment_allowlist contains an invalid variable name")
        if any(marker in lowered for marker in _SHELL_TOOLS_SENSITIVE_MARKERS):
            raise ShellToolsConfigError("environment_allowlist cannot include credential-like variables")
        if name not in environment_allowlist:
            environment_allowlist.append(name)

    default_timeout = bounded_int("default_timeout_seconds", 1, 120)
    max_timeout = bounded_int("max_timeout_seconds", 1, 300)
    if default_timeout > max_timeout:
        raise ShellToolsConfigError("default_timeout_seconds cannot exceed max_timeout_seconds")

    return {
        "enabled": enabled,
        "bash_enabled": bash_enabled,
        "bash_executable": executable,
        "environment_allowlist": environment_allowlist,
        "default_timeout_seconds": default_timeout,
        "max_timeout_seconds": max_timeout,
        "max_output_chars": bounded_int("max_output_chars", 256, 100_000),
        "max_glob_matches": bounded_int("max_glob_matches", 1, 10_000),
        "max_grep_matches": bounded_int("max_grep_matches", 1, 10_000),
        "max_file_bytes": bounded_int("max_file_bytes", 1, 100 * 1024 * 1024),
        "max_pattern_chars": bounded_int("max_pattern_chars", 1, 16_384),
        "max_scope_chars": bounded_int("max_scope_chars", 1, 16_384),
        "max_command_chars": bounded_int("max_command_chars", 1, 32_768),
    }


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
    "reserved_output_tokens": 4_096,
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
        "reserved_output_tokens": positive_int("reserved_output_tokens"),
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
