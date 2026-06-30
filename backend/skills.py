"""Skill management for Chat Agent.

Native-style skill lookup from the project-local `skills/` folder.
Supports directory-based skills with `SKILL.md` plus optional `resources/`
and retains compatibility with legacy `*.json` skill files.
"""

from __future__ import annotations

import asyncio
import json
import re
import shutil
from dataclasses import asdict, dataclass, field
from pathlib import Path
from queue import Queue
from threading import Thread
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_core.tools import tool
from pydantic import BaseModel, Field


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
    lines.append("Use the skill details tool only when you decide a skill is needed.")
    return "\n".join(lines)


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


async def execute_skill(root: Path, skill_name: str, params: dict[str, Any]) -> str:
    from .config import create_chat_deepseek, load_llm_config

    skill = get_installed_skill(root, skill_name)
    if skill is None:
        raise ValueError(f"Skill '{skill_name}' is not installed.")

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
    request: str = Field(..., description="Natural-language request for the skill.")


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
