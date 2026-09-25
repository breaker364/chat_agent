"""Skill management for Chat Agent.

Native-style skill lookup from the project-local `skills/` folder.
Supports directory-based skills with `SKILL.md` plus optional `resources/`
and retains compatibility with legacy `*.json` skill files.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import subprocess
import shutil
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from queue import Queue
from threading import Thread
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_core.tools import tool
from pydantic import BaseModel, Field

from .mutation_guard import (
    CommandManifest,
    MutationLedger,
    append_conflicts_with_workflow,
    build_idempotency_key,
    expected_content_from_manifest,
    extract_current_content,
    extract_state_version,
    parse_standardized_remote_command,
)
from .mutation_manifest import normalize_command_manifest
from .runtime_context import current_run_id


logger = logging.getLogger(__name__)

_RUN_MUTATION_LEDGERS: dict[str, MutationLedger] = {}
@dataclass
class SkillDefinition:
    name: str
    description: str
    prompt_template: str
    version: str = "1.0.0"
    author: str = "Chat Agent"
    category: str = "general"
    icon: str = "FileText"
    params_schema: dict[str, Any] = field(
        default_factory=lambda: {"type": "object", "properties": {}, "required": []}
    )
    resource_hints: list[str] = field(default_factory=list)
    script_hints: list[str] = field(default_factory=list)
    source_path: str = ""
    manifest_config: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "SkillDefinition":
        return cls(
            name=data.get("name", "unknown"),
            description=data.get("description", ""),
            prompt_template=data.get("prompt_template", ""),
            version=data.get("version", "1.0.0"),
            author=data.get("author", "Chat Agent"),
            category=data.get("category", "general"),
            icon=data.get("icon", "FileText"),
            params_schema=data.get("params_schema", {"type": "object", "properties": {}, "required": []}),
            resource_hints=data.get("resource_hints", []),
            script_hints=data.get("script_hints", []),
            source_path=data.get("source_path", ""),
            manifest_config=data.get("manifest_config", {}) or {},
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class SkillCatalogItem:
    name: str
    description: str
    category: str
    version: str
    author: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _skills_dir(root: Path) -> Path:
    skills_dir = root / "skills"
    skills_dir.mkdir(parents=True, exist_ok=True)
    return skills_dir


def _safe_name(name: str) -> str:
    return "".join(c if c.isalnum() or c in "-_." else "_" for c in name).lower()


def _skill_json_path(root: Path, name: str) -> Path:
    return _skills_dir(root) / f"{_safe_name(name)}.json"


def _parse_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    if not text.startswith("---"):
        return {}, text
    match = re.match(r"^---\s*\n(.*?)\n---\s*\n?(.*)$", text, re.DOTALL)
    if not match:
        return {}, text
    raw_meta, body = match.groups()
    meta: dict[str, Any] = {}
    for line in raw_meta.splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        meta[key.strip()] = value.strip().strip('"').strip("'")
    return meta, body


def _build_prompt_from_skill_md(markdown_body: str) -> str:
    return (
        "Follow this skill definition exactly.\n\n"
        f"{markdown_body.strip()}\n\n"
        "If the user request does not provide a structured schema, infer the needed parameters from the request as faithfully as possible."
    )


def _load_manifest_config(path: Path) -> dict[str, Any]:
    config_path = path / "command_manifest.json"
    if not config_path.exists():
        return {}
    try:
        data = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Invalid command manifest at {config_path}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"Invalid command manifest at {config_path}: expected an object")

    subcommand_operations = data.get("subcommand_operations", {})
    if not isinstance(subcommand_operations, dict):
        raise ValueError(
            f"Invalid command manifest at {config_path}: subcommand_operations must be an object"
        )
    for family, operations in subcommand_operations.items():
        if not isinstance(family, str) or not isinstance(operations, dict):
            raise ValueError(
                f"Invalid command manifest at {config_path}: command families must map to objects"
            )
        if any(
            not isinstance(name, str) or not isinstance(operation, str)
            for name, operation in operations.items()
        ):
            raise ValueError(
                f"Invalid command manifest at {config_path}: subcommands and operations must be strings"
            )
    return data


def _load_skill_from_markdown_dir(path: Path) -> SkillDefinition | None:
    skill_md = path / "SKILL.md"
    if not skill_md.exists():
        return None
    text = skill_md.read_text(encoding="utf-8")
    meta, body = _parse_frontmatter(text)
    resources_dir = path / "resources"
    scripts_dir = path / "scripts"
    resource_hints = []
    script_hints = []
    if resources_dir.exists():
        for item in sorted(resources_dir.iterdir()):
            resource_hints.append(str(item))
    if scripts_dir.exists():
        for item in sorted(scripts_dir.iterdir()):
            script_hints.append(str(item))
    manifest_config = _load_manifest_config(path)
    return SkillDefinition(
        name=meta.get("name", path.name),
        description=meta.get("description", ""),
        prompt_template=_build_prompt_from_skill_md(body),
        version=meta.get("version", "1.0.0"),
        author=meta.get("author", meta.get("license", "Chat Agent")),
        category=meta.get("category", "general"),
        icon="FileText",
        params_schema={"type": "object", "properties": {}, "required": []},
        resource_hints=resource_hints,
        script_hints=script_hints,
        source_path=str(path),
        manifest_config=manifest_config,
    )


def _load_skill_from_json(path: Path) -> SkillDefinition | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    skill = SkillDefinition.from_dict(data)
    skill.source_path = str(path)
    return skill


def list_installed_skills(root: Path) -> list[SkillDefinition]:
    installed: list[SkillDefinition] = []
    skills_dir = _skills_dir(root)

    for path in sorted(skills_dir.iterdir()):
        if path.is_dir():
            skill = _load_skill_from_markdown_dir(path)
            if skill is not None:
                installed.append(skill)
        elif path.suffix.lower() == ".json":
            skill = _load_skill_from_json(path)
            if skill is not None:
                installed.append(skill)
    return installed


def get_installed_skill(root: Path, name: str) -> SkillDefinition | None:
    normalized = _safe_name(name)
    for skill in list_installed_skills(root):
        if _safe_name(skill.name) == normalized:
            return skill


def find_managed_skill_policy(root: Path, content: str) -> dict[str, str] | None:
    """Find a manifest-declared skill boundary referenced by script text.

    The generic runtime does not identify providers or entities itself.  A
    skill that owns a remote integration declares its own execution markers
    and the tool that must be used for that integration in its manifest.
    """
    text = str(content or "").casefold()
    if not text:
        return None
    try:
        skills = list_installed_skills(root)
    except Exception:
        return None
    for skill in skills:
        manifest = skill.manifest_config if isinstance(skill.manifest_config, dict) else {}
        raw_policy = manifest.get("execution_policy")
        if not isinstance(raw_policy, dict):
            continue
        raw_markers = raw_policy.get("script_markers")
        if not isinstance(raw_markers, (list, tuple, set)):
            continue
        markers = [str(marker or "").strip().casefold() for marker in raw_markers]
        if not any(marker and marker in text for marker in markers):
            continue
        required_tool = str(raw_policy.get("required_tool") or "use_skill").strip()
        skill_name = str(raw_policy.get("skill_name") or skill.name).strip()
        return {
            "required_tool": required_tool or "use_skill",
            "skill_name": skill_name or skill.name,
        }
    return None


def install_skill(root: Path, skill: SkillDefinition) -> SkillDefinition:
    _skill_json_path(root, skill.name).write_text(
        json.dumps(skill.to_dict(), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return skill


def install_skill_from_registry(root: Path, name: str) -> SkillDefinition | None:
    registry = get_builtin_registry()
    skill = registry.get(name)
    return install_skill(root, skill) if skill else None


def uninstall_skill(root: Path, name: str) -> bool:
    normalized = _safe_name(name)
    target_dir = _skills_dir(root) / normalized
    target_json = _skill_json_path(root, name)
    if target_dir.exists() and target_dir.is_dir():
        shutil.rmtree(target_dir)
        return True
    if target_json.exists():
        target_json.unlink()
        return True
    return False


def get_available_skills(root: Path) -> list[SkillDefinition]:
    installed_names = {_safe_name(skill.name) for skill in list_installed_skills(root)}
    return [skill for name, skill in get_builtin_registry().items() if _safe_name(name) not in installed_names]


def list_skill_catalog(root: Path) -> list[SkillCatalogItem]:
    return [
        SkillCatalogItem(
            name=skill.name,
            description=skill.description,
            category=skill.category,
            version=skill.version,
            author=skill.author,
        )
        for skill in list_installed_skills(root)
    ]


def get_skill_catalog_text(root: Path) -> str:
    items = list_skill_catalog(root)
    if not items:
        return "No installed skills are available."
    lines = ["Installed skills catalog:"]
    for item in items:
        lines.append(f"- {item.name}: {item.description} [category={item.category}]")
        # CLI-backed skills can publish a short command cheat-sheet so the
        # agent can form correct `use_skill` requests without a detail read.
        for hint in skill_catalog_command_hints(root, item.name):
            lines.append(f"  {hint}")
    lines.append("Use the skill details tool only when you decide a skill is needed.")
    return "\n".join(lines)


def skill_catalog_command_hints(root: Path, skill_name: str) -> list[str]:
    """Return the optional `catalog_commands` hints declared by a skill manifest."""
    skill = get_installed_skill(root, skill_name)
    if skill is None or not isinstance(skill.manifest_config, dict):
        return []
    hints = skill.manifest_config.get("catalog_commands")
    if not isinstance(hints, list):
        return []
    return [str(hint).strip() for hint in hints if str(hint).strip()][:40]


def get_skill_detail_text(root: Path, skill_name: str) -> str:
    skill = get_installed_skill(root, skill_name)
    if skill is None:
        raise ValueError(f"Skill '{skill_name}' is not installed.")
    lines = [
        f"Skill: {skill.name}",
        f"Description: {skill.description}",
        f"Category: {skill.category}",
        f"Version: {skill.version}",
        f"Author: {skill.author}",
    ]
    if skill.params_schema.get("properties"):
        lines.append("Parameters schema:")
        lines.append(json.dumps(skill.params_schema, ensure_ascii=False, indent=2))
    if skill.resource_hints:
        lines.append("Resources:")
        lines.extend(f"- {hint}" for hint in skill.resource_hints)
    if skill.script_hints:
        lines.append("Scripts:")
        lines.extend(f"- {hint}" for hint in skill.script_hints)
    lines.append("Prompt template:")
    lines.append(skill.prompt_template)
    return "\n".join(lines)


def fill_prompt_template(template: str, params: dict[str, Any]) -> str:
    result = template
    for key, value in params.items():
        result = result.replace("{" + key + "}", str(value))
    return result


def _skill_runner_path(skill: SkillDefinition) -> Path | None:
    if not skill.source_path:
        return None
    source = Path(skill.source_path)
    if source.is_file():
        source = source.parent
    runner = source / "skill_runner.py"
    return runner if runner.exists() else None


def _skill_runtime_environment(
    skill: SkillDefinition,
    workspace_root: Path | None,
) -> dict[str, str]:
    """Return the runner environment, including only manifest-declared paths.

    Skill runners must execute with the same workspace context as the agent.
    A skill may declare non-secret path variables in ``runtime_environment``;
    values are expanded from the runner's skill and workspace roots.  This
    keeps provider-specific session discovery in the skill manifest instead of
    making the generic agent runtime guess paths from a temporary cwd.
    """
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    env["CHAT_AGENT_SKILL_ROOT"] = str(Path(skill.source_path).resolve())
    if workspace_root is not None:
        env["CHAT_AGENT_WORKSPACE_ROOT"] = str(workspace_root.resolve())

    declared = skill.manifest_config.get("runtime_environment") if isinstance(skill.manifest_config, dict) else None
    if not isinstance(declared, dict):
        return env

    skill_root = Path(skill.source_path).resolve()
    workspace = (workspace_root or skill_root).resolve()
    for key, value in declared.items():
        name = str(key or "").strip()
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
            continue
        if not isinstance(value, str) or "\x00" in value:
            continue
        expanded = (
            value.replace("{skill_root}", str(skill_root))
            .replace("{workspace_root}", str(workspace))
        )
        env[name] = expanded
    return env


def _skill_failure_payload(
    params: dict[str, Any],
    error: Any,
    *,
    partial_stdout: str = "",
) -> dict[str, Any]:
    """Classify runner failures without turning remote writes into successes."""
    manifest = params.get("manifest") if isinstance(params, dict) else None
    manifest = manifest if isinstance(manifest, dict) else {}
    operation = str(manifest.get("operation") or "").strip().lower()
    if operation == "read":
        status = "verification_failed"
        category = "readback_failed"
    elif bool(manifest.get("mutating")) or operation in {"append", "edit", "replace", "create", "delete"}:
        status = "write_failed"
        category = "remote_write_failed"
    else:
        status = "skill_execution_failed"
        category = "runner_failed"
    payload: dict[str, Any] = {
        "status": status,
        "error_category": category,
        "error": str(error or "Skill runner failed.")[:1200],
        "operation": operation,
        "verified": False,
    }
    if partial_stdout.strip():
        payload["partial_output"] = partial_stdout[:4000]
    return payload


def _execute_skill_runner_sync(
    skill: SkillDefinition,
    params: dict[str, Any],
    workspace_root: Path | None = None,
) -> str:
    runner = _skill_runner_path(skill)
    if runner is None:
        raise FileNotFoundError(f"No skill runner found for skill '{skill.name}'.")

    env = _skill_runtime_environment(skill, workspace_root)
    process = subprocess.run(
        [sys.executable, str(runner)],
        input=json.dumps(params, ensure_ascii=False).encode("utf-8"),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=str(Path(skill.source_path).resolve()),
        env=env,
        check=False,
    )
    if process.returncode != 0:
        stderr_text = process.stderr.decode("utf-8", errors="replace").strip()
        stdout_text = process.stdout.decode("utf-8", errors="replace").strip()
        return json.dumps(
            _skill_failure_payload(
                params,
                stderr_text or f"Skill runner failed with exit code {process.returncode}.",
                partial_stdout=stdout_text,
            ),
            ensure_ascii=False,
        )
    stdout_text = process.stdout.decode("utf-8", errors="replace").strip()
    if not stdout_text:
        return ""
    try:
        parsed = json.loads(stdout_text)
    except json.JSONDecodeError:
        return stdout_text
    if isinstance(parsed, dict) and "result" in parsed:
        return str(parsed["result"])
    return json.dumps(parsed, ensure_ascii=False, indent=2)


def _skill_result_indicates_failure(result: Any) -> bool:
    """Recognize structured runner failures before recording mutation success."""
    if not isinstance(result, str):
        return False
    try:
        payload = json.loads(result)
    except (TypeError, json.JSONDecodeError):
        return False
    if not isinstance(payload, dict):
        return False
    if payload.get("success") is False or payload.get("error"):
        return True
    return str(payload.get("status") or "").strip().lower() in {
        "invalid_input",
        "permission_denied",
        "policy_denied",
        "budget_denied",
        "runtime_unavailable",
        "timed_out",
        "output_truncated",
        "command_failed",
        "verification_failed",
        "write_failed",
        "skill_execution_failed",
    }


def _unclassified_command_message(request: str, skill: Any, exc: Exception) -> str:
    """Build an actionable block message so the agent can self-correct in one retry."""
    parts = str(request or "").strip().split()
    family = " ".join(parts[:2]).lower() if len(parts) >= 2 else ""
    configured: dict = {}
    if isinstance(skill.manifest_config, dict):
        configured = skill.manifest_config.get("subcommand_operations") or {}
    valid = configured.get(family)
    message = "Unclassified standardized remote command was blocked before dispatch."
    if isinstance(valid, dict) and valid:
        message += f" Valid subcommands for '{family}': {', '.join(sorted(valid))}."
    else:
        catalog = skill.manifest_config.get("catalog_commands") if isinstance(skill.manifest_config, dict) else None
        if isinstance(catalog, list) and catalog:
            message += " Known command families: " + "; ".join(str(c).split(" |")[0] for c in catalog[:8]) + "."
    return message


async def execute_skill(root: Path, skill_name: str, params: dict[str, Any]) -> str:
    from .config import create_chat_deepseek, load_llm_config

    skill = get_installed_skill(root, skill_name)
    if skill is None:
        raise ValueError(f"Skill '{skill_name}' is not installed.")

    request = str(params.get("request") or "").strip()
    is_standardized = request.lower().startswith(("lark ", "lark_cli "))
    manifest: CommandManifest | None = None
    if is_standardized:
        try:
            subcommand_operations = None
            if isinstance(skill.manifest_config, dict):
                subcommand_operations = skill.manifest_config.get("subcommand_operations")
            request_manifest = normalize_command_manifest(
                request,
                subcommand_operations=subcommand_operations,
            )
        except ValueError as exc:
            raise ValueError(
                _unclassified_command_message(request, skill, exc)
            ) from exc
        supplied_manifest = params.get("manifest")
        if supplied_manifest is not None:
            manifest = normalize_command_manifest(supplied_manifest)
            if manifest != request_manifest:
                raise ValueError("Provided manifest disagrees with the standardized request.")
        else:
            manifest = request_manifest
        params = dict(params)
        params["manifest"] = manifest.to_dict()

    async def dispatch() -> str:
        runner = _skill_runner_path(skill)
        if runner is not None:
            return await asyncio.to_thread(_execute_skill_runner_sync, skill, params, root)

        cfg = load_llm_config()
        llm = create_chat_deepseek(cfg, temperature=0.3, streaming=False, max_tokens=8192)
        prompt = fill_prompt_template(skill.prompt_template, params)
        system_prompt = (
            "You are a skill execution engine. Follow the skill definition exactly. "
            "If resources or scripts are listed, treat them as part of the skill context."
        )
        response = await llm.ainvoke(
            [SystemMessage(content=system_prompt), HumanMessage(content=prompt)]
        )
        return response.content if hasattr(response, "content") else str(response)

    if manifest is None:
        return await dispatch()
    if not manifest.provider or not manifest.resource:
        raise ValueError("Command manifest is missing provider or resource.")
    if not manifest.mutating:
        return await dispatch()
    if append_conflicts_with_workflow(manifest):
        raise ValueError("append was blocked because the workflow requires a replacement operation.")

    run_id = current_run_id()
    if not run_id:
        return await dispatch()
    ledger = _RUN_MUTATION_LEDGERS.setdefault(run_id, MutationLedger())
    key = build_idempotency_key(manifest, params)
    existing = ledger.succeeded_result(key)
    if existing is not None:
        return str(existing.result)

    expected_content = expected_content_from_manifest(manifest)

    async def read_back_state() -> tuple[str, dict[str, Any] | None]:
        """Probe the target state via the generic read operation.

        Returns ("ok", payload), ("failed", None) for a broken read attempt, or
        ("unavailable", None) when no read-back channel exists for the skill.
        """
        if _skill_runner_path(skill) is None:
            return "unavailable", None
        read_request = f"{manifest.provider} {manifest.resource} read {manifest.target}".strip()
        read_params = {
            name: value
            for name, value in params.items()
            if name not in {"request", "manifest", "tool_call_id"}
        }
        read_params["request"] = read_request
        try:
            raw = await asyncio.to_thread(_execute_skill_runner_sync, skill, read_params, root)
        except Exception as exc:
            logger.warning("Mutation read-back failed for %s: %s", read_request, exc)
            return "failed", None
        try:
            payload = json.loads(raw)
        except (TypeError, json.JSONDecodeError):
            return "ok", {}
        return "ok", payload if isinstance(payload, dict) else {}

    if expected_content:
        precheck_status, precheck_state = await read_back_state()
        if precheck_status == "ok" and extract_current_content(precheck_state or {}) == expected_content:
            satisfied = {
                "status": "already_satisfied",
                "action_id": key,
                "message": "Target already matches the expected content; dispatch skipped.",
            }
            ledger.record_success(
                key,
                manifest,
                json.dumps(satisfied, ensure_ascii=False),
                after_version=extract_state_version(precheck_state or {}),
                verified=True,
            )
            return json.dumps(satisfied, ensure_ascii=False, indent=2)

    result = await dispatch()
    if _skill_result_indicates_failure(result):
        return result

    try:
        audit = json.loads(result)
    except (TypeError, json.JSONDecodeError):
        audit = {}
    audit_payload = audit if isinstance(audit, dict) else {}

    after_version = str(audit_payload.get("after_version") or "") or extract_state_version(audit_payload)
    read_status, probe_state = await read_back_state()
    verified = False
    verification = "unverified"
    if read_status == "failed":
        verification = "verification_failed"
    elif read_status == "ok":
        probe_state = probe_state or {}
        read_version = extract_state_version(probe_state)
        read_content = extract_current_content(probe_state)
        if after_version and read_version:
            verified = read_version == after_version
            verification = "verified" if verified else "verification_failed"
        elif expected_content and read_content:
            verified = read_content == expected_content
            verification = "verified" if verified else "verification_failed"

    if verification == "verification_failed":
        failure_payload = {
            "status": "verification_failed",
            "action_id": key,
            "error_category": "business_rejected",
            "retryable": False,
            "message": "Mutation dispatched but the read-back could not confirm the expected result.",
            "dispatch_result": result if isinstance(result, str) else str(result),
        }
        logger.warning("Mutation verification failed; key %s is not recorded as success.", key)
        return json.dumps(failure_payload, ensure_ascii=False, indent=2)

    if audit_payload:
        audit_payload.setdefault("action_id", key)
        audit_payload["verified"] = verified
        audit_payload["verification"] = verification
        result = json.dumps(audit_payload, ensure_ascii=False, indent=2)
    ledger.record_success(
        key,
        manifest,
        result,
        before_version=str(audit_payload.get("before_version") or ""),
        after_version=str(audit_payload.get("after_version") or after_version),
        verified=verified,
    )
    return result


def clear_run_mutation_ledger(run_id: str | None) -> None:
    if run_id:
        _RUN_MUTATION_LEDGERS.pop(str(run_id), None)


def _run_coro_in_thread(coro: Any) -> Any:
    queue: Queue[tuple[bool, Any]] = Queue(maxsize=1)

    def _target() -> None:
        try:
            queue.put((True, asyncio.run(coro)))
        except Exception as exc:
            queue.put((False, exc))

    thread = Thread(target=_target, daemon=True)
    thread.start()
    ok, value = queue.get()
    if ok:
        return value
    raise value


class SkillDetailInput(BaseModel):
    skill_name: str = Field(..., description="Installed skill name.")


class SkillExecuteInput(BaseModel):
    skill_name: str = Field(..., description="Installed skill name.")
    request: str = Field(
        ...,
        description=(
            "Skill request. For CLI-only skills, pass the exact standardized command; "
            "natural language is not accepted."
        ),
    )


def build_skill_tools(root: Path) -> list[Any]:
    @tool(args_schema=SkillDetailInput)
    def read_skill_detail(skill_name: str) -> str:
        """Read the full definition of an installed skill when you decide it is needed."""
        return get_skill_detail_text(root, skill_name)

    @tool(args_schema=SkillExecuteInput)
    def use_skill(skill_name: str, request: str) -> str:
        """Execute an installed skill after selecting it from the catalog."""
        skill = get_installed_skill(root, skill_name)
        if skill is None:
            raise ValueError(f"Skill '{skill_name}' is not installed.")

        params = {"request": request}
        if "system_description" in skill.params_schema.get("properties", {}):
            params["system_description"] = request
        if "diagram_type" in skill.params_schema.get("properties", {}):
            params.setdefault(
                "diagram_type",
                skill.params_schema.get("properties", {}).get("diagram_type", {}).get("default", "architecture"),
            )
        return _run_coro_in_thread(execute_skill(root, skill.name, params))

    read_skill_detail.metadata = {"type": "skill-meta"}
    use_skill.metadata = {"type": "skill-execute"}
    return [read_skill_detail, use_skill]
