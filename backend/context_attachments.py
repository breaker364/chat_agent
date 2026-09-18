"""Hard re-injection of precise state after context compaction.

Compaction summaries can paraphrase away exact state. This module scans the
compacted (eligible) history for tool evidence — files that were read, skills
whose full text was loaded, background subagent tasks that were launched — and
rebuilds budgeted attachment items from live sources so the post-compaction
context keeps that state verbatim.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any, Callable, Iterable

from .token_counter import count_text_tokens


FILE_READ_TOOL_NAME = "read_file"
SKILL_READ_TOOL_NAMES = {"read_skill_detail", "use_skill"}
AGENT_LAUNCH_TOOL_NAME = "Agent"
AGENT_STATUS_TOOL_NAME = "get_subagent_task"

DEFAULT_ATTACHMENT_SETTINGS: dict[str, Any] = {
    "attachments_enabled": True,
    "attachment_total_tokens": 16_000,
    "attachment_file_max_tokens": 4_000,
    "attachment_file_limit": 5,
    "attachment_skill_max_tokens": 2_000,
    "attachment_skill_limit": 3,
}

_MIN_HEAD_TOKENS = 16
_SUBAGENT_TASK_LIMIT = 10
_SUBAGENT_PREVIEW_CHARS = 200
_TRUNCATION_NOTE = "\n[... attachment truncated ...]"


def _append_unique(values: list[str], value: str) -> None:
    if value and value not in values:
        values.append(value)


def _extract_agent_id(content: Any) -> str:
    if not isinstance(content, str):
        return ""
    try:
        payload = json.loads(content)
    except (TypeError, ValueError):
        return ""
    if isinstance(payload, dict):
        agent_id = payload.get("agent_id")
        return str(agent_id).strip() if agent_id else ""
    return ""


def collect_attachment_sources(
    eligible_items: Iterable[dict[str, Any]] | None,
) -> dict[str, list[str]]:
    """Scan compacted history (most recent first) for re-injectable sources."""
    files: list[str] = []
    skills: list[str] = []
    agent_ids: list[str] = []
    for item in reversed([entry for entry in (eligible_items or []) if isinstance(entry, dict)]):
        role = str(item.get("role") or "")
        if role == "assistant_tool_calls":
            for call in item.get("tool_calls") or []:
                if not isinstance(call, dict):
                    continue
                name = str(call.get("name") or "")
                args = call.get("args") if isinstance(call.get("args"), dict) else {}
                if name == FILE_READ_TOOL_NAME:
                    _append_unique(files, str(args.get("path") or "").strip())
                elif name in SKILL_READ_TOOL_NAMES:
                    _append_unique(skills, str(args.get("skill_name") or "").strip())
                elif name == AGENT_STATUS_TOOL_NAME:
                    _append_unique(agent_ids, str(args.get("agent_id") or "").strip())
        elif role == "tool" and str(item.get("name") or "") == AGENT_LAUNCH_TOOL_NAME:
            _append_unique(agent_ids, _extract_agent_id(item.get("content")))
    return {"files": files, "skills": skills, "agent_ids": agent_ids}


def _empty_details() -> dict[str, Any]:
    return {
        "counts": {"file": 0, "skill": 0, "subagent": 0},
        "tokens": 0,
        "skipped": [],
    }


def _make_item(
    attachment_type: str,
    title: str,
    content: str,
    source_ref: str,
    token_count: int,
) -> dict[str, Any]:
    return {
        "role": "context_attachment",
        "attachment_type": attachment_type,
        "title": title,
        "content": content,
        "source_ref": source_ref,
        "token_count": int(token_count),
    }


def _head_within_token_budget(
    text: str,
    *,
    per_item_budget: int,
    remaining_budget: int,
) -> tuple[str, int]:
    """Return (content, token_count) fitting both budgets; content ends with a
    truncation note when the source had to be cut."""
    budget = max(0, min(int(per_item_budget), int(remaining_budget)))
    text = text or ""
    if budget < _MIN_HEAD_TOKENS:
        return "", 0
    if count_text_tokens(text) <= budget:
        return text, count_text_tokens(text)

    def fits(prefix_length: int) -> bool:
        return count_text_tokens(text[:prefix_length] + _TRUNCATION_NOTE) <= budget

    low, high = 0, len(text)
    while low < high:
        mid = (low + high + 1) // 2
        if fits(mid):
            low = mid
        else:
            high = mid - 1
    head = text[:low].rstrip()
    content = head + _TRUNCATION_NOTE
    return content, count_text_tokens(content)


def _read_fresh_file(path: str) -> tuple[str, str | None]:
    """Re-read a file through the agent's own read tool so permission
    semantics match what the agent was allowed to read."""
    try:
        from .tools import read_file as read_file_tool

        result = read_file_tool.invoke({"path": path})
    except Exception as exc:
        return "", str(exc)
    text = result if isinstance(result, str) else str(result)
    if text.startswith(("File not found:", "Access denied:", "Failed to read")):
        return "", text.splitlines()[0]
    if not text.strip():
        return "", "empty content"
    return text, None


def _build_file_items(
    paths: Iterable[str],
    *,
    options: dict[str, Any],
    remaining: int,
    skipped: list[str],
) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    limit = int(options["attachment_file_limit"])
    for path in paths:
        if len(items) >= limit:
            skipped.append(f"file {path}: attachment file limit ({limit}) reached")
            continue
        if remaining < _MIN_HEAD_TOKENS:
            skipped.append(f"file {path}: token budget exhausted")
            continue
        text, error = _read_fresh_file(path)
        if error:
            skipped.append(f"file {path}: {error}")
            continue
        head, token_count = _head_within_token_budget(
            text,
            per_item_budget=int(options["attachment_file_max_tokens"]),
            remaining_budget=remaining,
        )
        if token_count <= 0:
            skipped.append(f"file {path}: token budget exhausted")
            continue
        content = (
            f"[Context attachment] File: {path}\n"
            f'Head content after context compaction; re-run read_file("{path}") '
            "for full current content.\n"
            f"---\n{head}\n---"
        )
        items.append(_make_item("file", f"File: {path}", content, path, token_count))
        remaining -= token_count
    return items


def _build_skill_items(
    names: Iterable[str],
    *,
    workspace_root: Path,
    options: dict[str, Any],
    remaining: int,
    skipped: list[str],
) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    limit = int(options["attachment_skill_limit"])
    for name in names:
        if len(items) >= limit:
            skipped.append(f"skill {name}: attachment skill limit ({limit}) reached")
            continue
        if remaining < _MIN_HEAD_TOKENS:
            skipped.append(f"skill {name}: token budget exhausted")
            continue
        try:
            from .skills import get_installed_skill

            skill = get_installed_skill(workspace_root, name)
        except Exception as exc:
            skill = None
            skipped.append(f"skill {name}: lookup failed: {exc}")
            continue
        if skill is None:
            skipped.append(f"skill {name}: not installed")
            continue
        source_path = Path(skill.source_path) if skill.source_path else None
        if source_path is not None and source_path.is_dir():
            candidate = source_path / "SKILL.md"
            source_path = candidate if candidate.is_file() else None
        body = ""
        if source_path is not None and source_path.is_file():
            try:
                body = source_path.read_text(encoding="utf-8", errors="replace")
            except Exception as exc:
                skipped.append(f"skill {name}: unreadable source: {exc}")
                continue
        elif skill.prompt_template:
            body = skill.prompt_template
        head, token_count = _head_within_token_budget(
            body,
            per_item_budget=int(options["attachment_skill_max_tokens"]),
            remaining_budget=remaining,
        )
        if token_count <= 0:
            skipped.append(f"skill {name}: token budget exhausted")
            continue
        source_text = str(source_path) if source_path is not None else name
        content = (
            f"[Context attachment] Skill: {name}\n"
            f"Head of the skill definition after context compaction; "
            f"the full text lives at {source_text} and can be re-read when needed.\n"
            f"---\n{head}\n---"
        )
        items.append(_make_item("skill", f"Skill: {name}", content, source_text, token_count))
        remaining -= token_count
    return items


def _default_task_reader(workspace_root: Path) -> Callable[[str], dict[str, Any] | None]:
    def reader(agent_id: str) -> dict[str, Any] | None:
        from .subagent_runtime import get_subagent_manager

        return get_subagent_manager().get_task(agent_id, workspace_dir=workspace_root)

    return reader


def _build_subagent_item(
    agent_ids: Iterable[str],
    *,
    workspace_root: Path,
    remaining: int,
    task_reader: Callable[[str], dict[str, Any] | None] | None,
    skipped: list[str],
) -> dict[str, Any] | None:
    reader = task_reader or _default_task_reader(workspace_root)
    resolved: list[dict[str, Any]] = []
    for agent_id in list(agent_ids)[:_SUBAGENT_TASK_LIMIT]:
        try:
            task = reader(agent_id)
        except Exception as exc:
            task = None
            skipped.append(f"subagent task {agent_id}: lookup failed: {exc}")
            continue
        if isinstance(task, dict):
            resolved.append(task)
        else:
            skipped.append(f"subagent task {agent_id}: not found")
    if not resolved:
        return None
    lines = [
        "[Context attachment] Background subagent tasks",
        "Current states re-checked after context compaction; "
        "do not re-dispatch tasks that are still running:",
    ]
    for task in resolved:
        agent_id = str(task.get("agent_id") or "unknown")
        status = str(task.get("status") or "unknown")
        description = str(task.get("description") or "")[:_SUBAGENT_PREVIEW_CHARS]
        outcome = str(task.get("result") or task.get("error") or "")[:_SUBAGENT_PREVIEW_CHARS]
        line = f"- {agent_id} | {status} | {description}"
        if outcome:
            line += f" | {outcome}"
        lines.append(line)
    content, token_count = _head_within_token_budget(
        "\n".join(lines),
        per_item_budget=remaining,
        remaining_budget=remaining,
    )
    if token_count <= 0:
        for task in resolved:
            skipped.append(f"subagent task {task.get('agent_id')}: token budget exhausted")
        return None
    return _make_item("subagent", "Background subagent tasks", content, "", token_count)


def build_context_attachments(
    sources: dict[str, list[str]] | None,
    *,
    workspace_root: Path | None = None,
    settings: dict[str, Any] | None = None,
    task_reader: Callable[[str], dict[str, Any] | None] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Build budgeted context_attachment items; never raises for bad sources."""
    options = {**DEFAULT_ATTACHMENT_SETTINGS, **(settings or {})}
    details = _empty_details()
    if not options.get("attachments_enabled", True):
        return [], details
    sources = sources or {}
    if not any(sources.get(key) for key in ("files", "skills", "agent_ids")):
        return [], details

    root = Path(workspace_root or Path.cwd()).resolve()
    remaining = int(options["attachment_total_tokens"])
    items: list[dict[str, Any]] = []

    file_items = _build_file_items(
        sources.get("files") or [], options=options, remaining=remaining, skipped=details["skipped"]
    )
    items.extend(file_items)
    details["counts"]["file"] = len(file_items)
    remaining -= sum(item["token_count"] for item in file_items)

    skill_items = _build_skill_items(
        sources.get("skills") or [],
        workspace_root=root,
        options=options,
        remaining=remaining,
        skipped=details["skipped"],
    )
    items.extend(skill_items)
    details["counts"]["skill"] = len(skill_items)
    remaining -= sum(item["token_count"] for item in skill_items)

    subagent_item = _build_subagent_item(
        sources.get("agent_ids") or [],
        workspace_root=root,
        remaining=remaining,
        task_reader=task_reader,
        skipped=details["skipped"],
    )
    if subagent_item is not None:
        items.append(subagent_item)
        details["counts"]["subagent"] = 1
        remaining -= subagent_item["token_count"]

    details["tokens"] = sum(item["token_count"] for item in items)
    return items, details
