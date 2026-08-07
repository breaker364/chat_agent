from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import re
import time
from pathlib import Path
from typing import Any, AsyncGenerator

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.runnables import Runnable
from langgraph.prebuilt import create_react_agent

from .config import (
    create_chat_deepseek,
    load_agent_memory_config,
    load_context_compaction_config,
    load_llm_config,
)
from .agentic_research.config import load_agentic_research_config
from .agentic_research.runtime import (
    EVIDENCE_TOOL_NAMES,
    AgenticResearchRuntime,
    build_agentic_research_runtime,
)
from .context_compaction import (
    ContextCompactionError,
    build_compaction_cache,
    canonical_history_fingerprint,
    compact_history_with_llm,
    get_context_compaction_lock,
    is_compaction_cache_usable,
    partition_history,
    should_compact_context,
    validate_summary_text,
)
from .prompts import load_agent_policy, load_system_prompt
from .session_events import get_session_event_hub
from .session_store import SessionStore
from .memory import MemoryContextProvider
from .skills import get_skill_catalog_text
from .token_counter import count_text_tokens
from .runtime_context import current_run_id, current_session_id
from .run_append import consume_pending_append_commands
from .tools import get_all_tools, _normalize_tool_payload_for_key
from .vision import VisionConfigurationError, analyze_image_files, extract_image_paths

SYSTEM_PROMPT = load_system_prompt()
AGENT_POLICY = load_agent_policy()


MAX_AGENT_STEPS = 180
MAX_WEB_SEARCH_CALLS = 10
MAX_WEB_FETCH_CALLS = 5
MAX_AGENT_REPAIR_PASSES = 2
MAX_BACKGROUND_SUBAGENT_POLLS = 6
MAX_HISTORY_MESSAGES = 25
MAX_HISTORY_TOTAL_CHARS = 970_000
MAX_HISTORY_MESSAGE_CHARS = 8_000
HEARTBEAT_EMIT_INTERVAL_SECONDS = 3
_SENSITIVE_TEXT_RE = re.compile(
    r"(?:api[_-]?key|authorization|token|password|secret|cookie|credential)\s*[:=]?\s*\S+",
    re.IGNORECASE,
)


def _append_marker(sequence: int) -> str:
    return f"用户追加指令（运行中补充，第 {sequence} 条）："


def _record_append_command_injected(session_id: str, command_payload: dict[str, Any]) -> None:
    try:
        SessionStore(Path.cwd()).record_append_command_event(session_id, command_payload)
    except Exception:
        pass
    try:
        get_session_event_hub().publish(
            session_id,
            {
                "stage": "append_command_injected",
                "message": "Injected appended instruction before the next model call.",
                "session_id": session_id,
                "run_id": command_payload.get("run_id"),
                "append_id": command_payload.get("append_id"),
                "status": command_payload.get("status"),
                "sequence": command_payload.get("sequence"),
            },
        )
    except Exception:
        pass


def _inject_pending_append_commands_into_messages(
    session_id: str,
    run_id: str,
    messages: list[BaseMessage],
) -> list[dict[str, Any]]:
    normalized_session_id = str(session_id or "default").strip() or "default"
    normalized_run_id = str(run_id or "").strip()
    if not normalized_run_id:
        return []
    commands = consume_pending_append_commands(normalized_session_id, normalized_run_id)
    if not commands:
        return []
    injected: list[dict[str, Any]] = []
    for command in commands:
        payload = command.to_dict()
        messages.append(HumanMessage(content=f"{_append_marker(command.sequence)}\n{command.content}"))
        _record_append_command_injected(normalized_session_id, payload)
        injected.append(payload)
    return injected


def _copy_input_with_appendable_messages(input_value: Any) -> tuple[Any, list[BaseMessage] | None]:
    if isinstance(input_value, dict):
        raw_messages = input_value.get("messages")
        if isinstance(raw_messages, (list, tuple)):
            copied_messages = list(raw_messages)
            return {**input_value, "messages": copied_messages}, copied_messages
        return input_value, None
    if isinstance(input_value, (list, tuple)):
        copied_messages = list(input_value)
        return copied_messages, copied_messages
    return input_value, None


def _inject_pending_append_commands_into_model_input(input_value: Any) -> Any:
    rewritten_input, messages = _copy_input_with_appendable_messages(input_value)
    if messages is None:
        return input_value
    session_id = current_session_id()
    run_id = current_run_id()
    if not session_id or not run_id:
        return input_value
    _inject_pending_append_commands_into_messages(session_id, run_id, messages)
    return rewritten_input


class AppendAwareChatModel(Runnable[Any, Any]):
    """Runnable proxy that checks run-scoped append commands before model calls."""

    def __init__(self, model: Any) -> None:
        self._model = model

    def __getattr__(self, name: str) -> Any:
        return getattr(self._model, name)

    def bind_tools(self, *args: Any, **kwargs: Any) -> "AppendAwareChatModel":
        return AppendAwareChatModel(self._model.bind_tools(*args, **kwargs))

    def invoke(self, input: Any, config: Any = None, **kwargs: Any) -> Any:
        injected_input = _inject_pending_append_commands_into_model_input(input)
        if config is None:
            return self._model.invoke(injected_input, **kwargs)
        return self._model.invoke(injected_input, config, **kwargs)

    async def ainvoke(self, input: Any, config: Any = None, **kwargs: Any) -> Any:
        injected_input = _inject_pending_append_commands_into_model_input(input)
        if config is None:
            return await self._model.ainvoke(injected_input, **kwargs)
        return await self._model.ainvoke(injected_input, config, **kwargs)

    def stream(self, input: Any, config: Any = None, **kwargs: Any) -> Any:
        injected_input = _inject_pending_append_commands_into_model_input(input)
        if config is None:
            return self._model.stream(injected_input, **kwargs)
        return self._model.stream(injected_input, config, **kwargs)

    async def astream(self, input: Any, config: Any = None, **kwargs: Any) -> Any:
        injected_input = _inject_pending_append_commands_into_model_input(input)
        if config is None:
            async for chunk in self._model.astream(injected_input, **kwargs):
                yield chunk
        else:
            async for chunk in self._model.astream(injected_input, config, **kwargs):
                yield chunk


def wrap_chat_model_with_append_injection(model: Any) -> AppendAwareChatModel:
    return AppendAwareChatModel(model)


def estimate_tokens_from_text(text: str) -> int:
    return count_text_tokens(text or "")


def get_model_context_window(model_name: str) -> int | None:
    """Get the configured context window size for the specified model.

    Returns:
        The context window size in tokens, or None if not configured or invalid.
    """
    try:
        from .config import load_app_config
        config = load_app_config()
        configured_model = config.get("model", "")
        if configured_model != model_name:
            return None
        context_window = config.get("model_context_window")
        if isinstance(context_window, int) and context_window > 0:
            return context_window
        return None
    except Exception:
        return None


def check_context_capacity(
    context_token_estimate: int,
    model_name: str,
) -> dict[str, Any]:
    """Check the remaining context capacity for the current model.

    Args:
        context_token_estimate: Estimated token count of current context.
        model_name: The model name to check capacity for.

    Returns:
        A dictionary containing:
        - model_context_window: The configured context window size (or None).
        - context_token_estimate: The input token estimate.
        - remaining_tokens: Remaining tokens before limit (or None if unknown).
        - is_exceeded: Whether the context exceeds the model limit.
    """
    model_limit = get_model_context_window(model_name)

    if model_limit is None:
        return {
            "model_context_window": None,
            "context_token_estimate": context_token_estimate,
            "remaining_tokens": None,
            "is_exceeded": False,
        }

    remaining = max(0, model_limit - context_token_estimate)
    return {
        "model_context_window": model_limit,
        "context_token_estimate": context_token_estimate,
        "remaining_tokens": remaining,
        "is_exceeded": context_token_estimate > model_limit,
    }


def _history_entry_text(entry: dict[str, Any]) -> str:
    """Serialize structured history without dropping native tool payload fields."""
    if entry.get("role") in {
        "assistant_tool_calls",
        "tool",
        "legacy_tool_result",
        "malformed_tool_result",
        "historical_tool_context",
        "context_summary",
    }:
        return json.dumps(entry, ensure_ascii=False, sort_keys=True, default=str)
    return str(entry.get("content") or "")


def _estimate_context_metrics(
    history_items: list[dict[str, Any]] | None,
    *,
    effective_message: str,
    skill_catalog_text: str,
    resume_text: str,
    memory_context: str = "",
) -> dict[str, Any]:
    history_payloads = [_history_entry_text(entry) for entry in (history_items or [])]
    protected_tool_chars = sum(
        len(payload)
        for entry, payload in zip(history_items or [], history_payloads)
        if entry.get("protected_tool_history")
    )
    context_char_count = sum(len(payload) for payload in history_payloads)
    context_char_count += len(SYSTEM_PROMPT) + len(AGENT_POLICY or "") + len(effective_message)
    context_message_count = len(history_payloads) + 2
    if skill_catalog_text:
        context_char_count += len(skill_catalog_text)
        context_message_count += 1
    if resume_text:
        context_char_count += len(resume_text)
        context_message_count += 1
    if memory_context:
        context_char_count += len(memory_context)
        context_message_count += 1
    context_token_estimate = estimate_tokens_from_text("".join(
        [
            SYSTEM_PROMPT,
            AGENT_POLICY or "",
            memory_context or "",
            skill_catalog_text or "",
            resume_text or "",
            effective_message,
        ]
        + history_payloads
    ))
    return {
        "history_payloads": history_payloads,
        "protected_tool_chars": protected_tool_chars,
        "context_char_count": context_char_count,
        "context_message_count": context_message_count,
        "context_token_estimate": context_token_estimate,
    }


async def _compact_history_if_needed(
    history: list[dict[str, Any]] | None,
    *,
    session_id: str,
    model_config: dict[str, Any],
    model_context_window: int | None,
    context_token_estimate: int,
    settings: dict[str, Any],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    source_history = [dict(item) for item in (history or []) if isinstance(item, dict)]
    retain_recent_turns = int(settings.get("retain_recent_turns", 3))
    partition = partition_history(source_history, retain_recent_turns=retain_recent_turns)
    old_turn_count = max(0, len(partition.completed_turns) - retain_recent_turns)
    details: dict[str, Any] = {
        "triggered": False,
        "cache_hit": False,
        "protected_turn_count": min(len(partition.completed_turns), retain_recent_turns),
        "eligible_turn_count": old_turn_count,
        "eligible_item_count": len(partition.eligible_items),
        "model_context_window": model_context_window,
        "context_token_estimate_before": context_token_estimate,
        "remaining_tokens_before": (
            model_context_window - context_token_estimate
            if model_context_window is not None
            else None
        ),
        "model_calls": 0,
    }
    if not settings.get("enabled", True):
        details["skip_reason"] = "disabled"
        return source_history, details
    if model_context_window is None:
        details["skip_reason"] = "unknown_context_window"
        return source_history, details
    if not should_compact_context(
        context_token_estimate,
        model_context_window,
        int(settings.get("trigger_remaining_tokens", 20_000)),
    ):
        details["skip_reason"] = "above_threshold"
        return source_history, details
    if not partition.eligible_items:
        details["skip_reason"] = "no_eligible_history"
        return source_history, details

    model_name = str(model_config.get("model") or "")
    store = SessionStore(Path.cwd())
    current_fingerprint = canonical_history_fingerprint(partition.eligible_items)
    async with get_context_compaction_lock(session_id):
        cache = store.get_context_compaction(session_id)
        cached_summary = ""
        merge_items: list[dict[str, Any]] | None = None
        cache_usable = is_compaction_cache_usable(
            cache,
            partition.eligible_items,
            model_name=model_name,
            allow_extended_source=True,
            expected_boundary_index=partition.boundary_index,
            expected_covered_turn_count=old_turn_count,
        )
        if cache_usable and cache is not None:
            try:
                validate_summary_text(
                    str(cache.get("summary") or ""),
                    cache.get("literal_ledger") or [],
                    max_output_tokens=int(settings.get("summary_max_output_tokens", 4_096)),
                )
            except ContextCompactionError:
                cache_usable = False
        if cache_usable and cache is not None:
            covered_count = int(cache.get("covered_item_count", 0))
            cached_summary = str(cache.get("summary") or "")
            if covered_count == len(partition.eligible_items):
                details["triggered"] = True
                details["cache_hit"] = True
                details["summary"] = cached_summary
                details["source_fingerprint"] = current_fingerprint
                return [
                    {
                        "role": "context_summary",
                        "content": cached_summary,
                        "context_compaction": True,
                        "source_fingerprint": current_fingerprint,
                        "covered_turn_count": old_turn_count,
                    },
                    *partition.protected_items,
                ], details
            if covered_count < len(partition.eligible_items):
                merge_items = [
                    {"role": "context_summary", "content": cached_summary},
                    *partition.eligible_items[covered_count:],
                ]

        source_items = merge_items or partition.eligible_items

        def generate_summary() -> Any:
            llm = create_chat_deepseek(
                model_config,
                temperature=0.0,
                streaming=False,
                max_tokens=int(settings.get("summary_max_output_tokens", 4_096)),
            )
            return compact_history_with_llm(
                llm,
                source_items,
                settings=settings,
            )

        try:
            result = await asyncio.to_thread(generate_summary)
        except ContextCompactionError:
            raise
        except Exception as exc:
            raise ContextCompactionError(
                f"Context compaction failed: {exc}",
                code="context_compaction_failed",
            ) from exc

        details["triggered"] = True
        details["model_calls"] = result.model_calls
        details["source_fingerprint"] = current_fingerprint
        cache_payload = build_compaction_cache(
            result,
            model_name=model_name,
            boundary_index=partition.boundary_index,
            context_token_estimate_before=context_token_estimate,
            context_token_estimate_after=estimate_tokens_from_text(result.summary),
        )
        cache_payload.update(
            {
                "source_fingerprint": current_fingerprint,
                "covered_item_count": len(partition.eligible_items),
                "covered_turn_count": old_turn_count,
            }
        )
        try:
            store.save_context_compaction(session_id, cache_payload)
        except Exception as exc:
            raise ContextCompactionError(
                f"Context compaction cache persistence failed: {exc}",
                code="context_compaction_failed",
            ) from exc
        details["summary"] = result.summary
        return [
            {
                "role": "context_summary",
                "content": result.summary,
                "context_compaction": True,
                "source_fingerprint": current_fingerprint,
                "covered_turn_count": old_turn_count,
            },
            *partition.protected_items,
        ], details


def _tool_message_content(content: Any) -> str | list[Any]:
    if isinstance(content, (str, list)):
        return content
    return json.dumps(content, ensure_ascii=False, default=str)


def _append_native_history_messages(messages: list[BaseMessage], history_items: list[dict[str, Any]]) -> None:
    """Replay protected tool entries using LangChain's native call/result protocol."""
    invalid_tool_call_ids: set[str] = set()
    for entry in history_items:
        role = entry.get("role", "")
        if role == "user":
            messages.append(HumanMessage(content=str(entry.get("content") or "")))
        elif role == "assistant":
            messages.append(AIMessage(content=str(entry.get("content") or "")))
        elif role == "assistant_tool_calls":
            tool_calls = entry.get("tool_calls")
            if not isinstance(tool_calls, list) or not tool_calls:
                continue
            valid_tool_calls: list[dict[str, Any]] = []
            malformed_tool_calls: list[Any] = []
            for tool_call in tool_calls:
                if not isinstance(tool_call, dict):
                    malformed_tool_calls.append(tool_call)
                    continue
                tool_call_id = str(tool_call.get("id") or "").strip()
                name = str(tool_call.get("name") or "").strip()
                arguments = tool_call.get("args")
                if not tool_call_id or not name or not isinstance(arguments, dict):
                    if tool_call_id:
                        invalid_tool_call_ids.add(tool_call_id)
                    malformed_tool_calls.append(tool_call)
                    continue
                valid_tool_calls.append(
                    {"name": name, "args": arguments, "id": tool_call_id, "type": "tool_call"}
                )
            if valid_tool_calls:
                messages.append(AIMessage(content="", tool_calls=valid_tool_calls))
            for malformed_tool_call in malformed_tool_calls:
                messages.append(
                    AIMessage(
                        content=json.dumps(
                            {"malformed_tool_call": True, "tool_call": malformed_tool_call},
                            ensure_ascii=False,
                            default=str,
                        )
                    )
                )
        elif role == "tool":
            tool_call_id = str(entry.get("tool_call_id") or "").strip()
            if not tool_call_id:
                continue
            if tool_call_id in invalid_tool_call_ids:
                messages.append(
                    AIMessage(
                        content=json.dumps(
                            {
                                "malformed_tool_result": True,
                                "tool_call_id": tool_call_id,
                                "name": entry.get("name"),
                                "content": entry.get("content"),
                            },
                            ensure_ascii=False,
                            default=str,
                        )
                    )
                )
                continue
            messages.append(
                ToolMessage(
                    content=_tool_message_content(entry.get("content")),
                    tool_call_id=tool_call_id,
                    name=str(entry.get("name") or "tool"),
                )
            )
        elif role in {"historical_tool_context", "context_summary"}:
            messages.append(AIMessage(content=str(entry.get("content") or "")))
        elif role in {"legacy_tool_result", "malformed_tool_result"}:
            # An orphaned legacy result has no valid native call to reference.
            # Preserve it explicitly instead of silently dropping the data.
            messages.append(
                AIMessage(
                    content=json.dumps(
                        {
                            "legacy_unmatched_tool_result": role == "legacy_tool_result",
                            "malformed_tool_result": role == "malformed_tool_result",
                            "name": entry.get("name"),
                            "tool_call_id": entry.get("tool_call_id"),
                            "content": entry.get("content"),
                        },
                        ensure_ascii=False,
                        default=str,
                    )
                )
            )


def _repair_dangling_tool_call_messages(messages: list[BaseMessage]) -> None:
    """Downgrade native tool calls that lack a matching ToolMessage before provider calls."""
    tool_message_ids = {
        str(message.tool_call_id)
        for message in messages
        if isinstance(message, ToolMessage) and getattr(message, "tool_call_id", None)
    }
    if not tool_message_ids and not any(isinstance(message, AIMessage) and message.tool_calls for message in messages):
        return

    repaired: list[BaseMessage] = []
    changed = False
    for message in messages:
        if not isinstance(message, AIMessage) or not message.tool_calls:
            repaired.append(message)
            continue

        valid_calls: list[dict[str, Any]] = []
        dangling_calls: list[dict[str, Any]] = []
        for tool_call in message.tool_calls:
            tool_call_id = str(tool_call.get("id") or "")
            if tool_call_id and tool_call_id in tool_message_ids:
                valid_calls.append(tool_call)
            else:
                dangling_calls.append(tool_call)

        if valid_calls:
            if len(valid_calls) == len(message.tool_calls):
                repaired.append(message)
            else:
                repaired.append(AIMessage(content=message.content or "", tool_calls=valid_calls))
                changed = True
        if dangling_calls:
            changed = True
            repaired.append(
                AIMessage(
                    content=json.dumps(
                        {
                            "dangling_tool_call": True,
                            "tool_call_ids": [str(call.get("id") or "") for call in dangling_calls],
                            "tool_names": [str(call.get("name") or "tool") for call in dangling_calls],
                        },
                        ensure_ascii=False,
                        default=str,
                    )
                )
            )

    if changed:
        messages[:] = repaired


def _safe_summary(value: Any, limit: int = 240) -> str:
    """Return a short user-safe summary without raw diagnostics or credentials."""
    lines = [line.strip() for line in str(value or "").splitlines() if line.strip()]
    safe_lines = [
        line for line in lines
        if "traceback" not in line.lower() and not _SENSITIVE_TEXT_RE.search(line)
    ]
    return " ".join(safe_lines)[:limit]


def build_exception_analysis_context(
    *,
    exc_text: str,
    active_tool: str | None,
    tool_call_history: list[dict[str, Any]],
    tool_errors: list[dict[str, Any]],
    latest_activity: dict[str, Any] | None,
    partial_response: str,
) -> dict[str, Any]:
    """Collect bounded, sanitized evidence for a user-facing failure analysis."""
    exception_summary = ""
    for line in str(exc_text or "").splitlines():
        candidate = line.strip()
        if re.match(r"^[A-Za-z_][A-Za-z0-9_.]*(Error|Exception):", candidate):
            exception_summary = candidate
            break
    exception_summary = _safe_summary(exception_summary or exc_text, 300) or "Unknown execution error."

    recent_tools = [
        str(item.get("tool") or "")[:80]
        for item in tool_call_history[-8:]
        if isinstance(item, dict) and item.get("tool")
    ]
    recent_errors = [
        {"tool": str(item.get("tool") or "tool")[:80], "summary": _safe_summary(item.get("message"), 180) or "Tool failed."}
        for item in tool_errors[-5:]
        if isinstance(item, dict)
    ]
    activity_summary = _safe_summary((latest_activity or {}).get("summary"), 240)
    return {
        "exception_summary": exception_summary,
        "active_tool": str(active_tool or "")[:80],
        "recent_tools": recent_tools,
        "recent_errors": recent_errors,
        "latest_activity": activity_summary,
        "partial_response": _safe_summary(partial_response, 800),
    }


_EXCEPTION_ANALYSIS_FIELDS = (
    "what_happened",
    "likely_cause",
    "completion_status",
    "next_step",
)


async def analyze_exception_for_user(context: dict[str, Any], invoke_analysis: Any) -> dict[str, str] | None:
    """Run an injectable analyzer and accept only complete, safe structured output."""
    try:
        raw = invoke_analysis(context)
        if inspect.isawaitable(raw):
            raw = await raw
        if hasattr(raw, "content"):
            raw = raw.content
        parsed = json.loads(raw) if isinstance(raw, str) else raw
        if not isinstance(parsed, dict):
            return None
        normalized: dict[str, str] = {}
        for field in _EXCEPTION_ANALYSIS_FIELDS:
            value = str(parsed.get(field) or "").strip()
            if not value or len(value) > 600 or "traceback" in value.lower() or _SENSITIVE_TEXT_RE.search(value):
                return None
            normalized[field] = value
        return normalized
    except Exception:
        return None


def format_exception_analysis(analysis: dict[str, str]) -> str:
    """Render a validated exception analysis without exposing raw diagnostics."""
    return (
        "任务未完成。\n\n"
        "**问题分析**\n"
        f"- 发生情况：{analysis['what_happened']}\n"
        f"- 可能原因：{analysis['likely_cause']}\n"
        f"- 完成情况：{analysis['completion_status']}\n\n"
        "**建议下一步**\n"
        f"- {analysis['next_step']}"
    )


def build_activity_event(
    *,
    state: str,
    summary: str,
    elapsed_seconds: int | float | None,
    evidence: str = "",
    judgment: str = "",
    next_step: str = "",
    progress: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Create a bounded, user-facing activity payload from observable events."""
    normalized_state = state if state in {"understanding", "working", "judging", "completed", "blocked"} else "working"
    payload: dict[str, Any] = {
        "state": normalized_state,
        "summary": _safe_summary(summary, 300) or "正在处理任务。",
        "elapsed_seconds": max(0, int(elapsed_seconds or 0)),
    }
    for key, value in (("evidence", evidence), ("judgment", judgment), ("next_step", next_step)):
        safe_value = _safe_summary(value, 300)
        if safe_value:
            payload[key] = safe_value
    if isinstance(progress, dict):
        current = progress.get("current")
        total = progress.get("total")
        if isinstance(current, int) and not isinstance(current, bool) and isinstance(total, int) and not isinstance(total, bool) and total > 0:
            payload["progress"] = {
                "current": max(0, min(current, total)),
                "total": total,
                "label": _safe_summary(progress.get("label"), 120),
            }
    return payload


def _format_resume_context(context: dict[str, Any]) -> str:
    if not context:
        return ""
    lines: list[str] = []
    primary = context.get("primary_result")
    if isinstance(primary, dict) and (primary.get("url") or primary.get("path") or primary.get("summary")):
        lines.append("Primary result already registered:")
        lines.append(
            "- {type}: {target} | status={status} | verified={verified} | summary={summary}".format(
                type=primary.get("type", "result"),
                target=primary.get("url") or primary.get("path") or primary.get("token") or "",
                status=primary.get("status", ""),
                verified=primary.get("verified", False),
                summary=str(primary.get("summary") or "")[:300],
            )
        )
    stage_results = context.get("stage_results")
    if isinstance(stage_results, dict) and stage_results:
        lines.append("Reusable completed stage results:")
        for name, result in list(stage_results.items())[:8]:
            if not isinstance(result, dict):
                continue
            if str(result.get("status") or "") not in {"completed", "verified"}:
                continue
            lines.append(
                f"- {name}: result_ref={result.get('result_ref') or ''} | summary={str(result.get('summary') or '')[:220]}"
            )
    unfinished = context.get("unfinished_todos")
    if isinstance(unfinished, list) and unfinished:
        lines.append("Unfinished task plan items:")
        for item in unfinished[:6]:
            if isinstance(item, dict):
                lines.append(f"- {item.get('task_id')}: {item.get('status')} | {item.get('content')}")
    if not lines:
        return ""
    return (
        "Session resume context. Reuse completed stages and primary results; "
        "do not rerun expensive analysis or recreate outputs unless the user explicitly asks.\n"
        + "\n".join(lines)
    )


def _extract_usage_metadata(value: Any) -> dict[str, int]:
    """Normalize token usage from LangChain and provider response shapes."""
    if value is None:
        return {}
    candidates: list[Any] = []
    if isinstance(value, dict):
        candidates.extend(
            [
                value.get("usage_metadata"),
                value.get("response_metadata"),
                value.get("token_usage"),
                value.get("usage"),
                value,
            ]
        )
        if isinstance(value.get("response_metadata"), dict):
            candidates.append(value["response_metadata"].get("token_usage"))
    else:
        candidates.extend(
            [
                getattr(value, "usage_metadata", None),
                getattr(value, "response_metadata", None),
                getattr(value, "token_usage", None),
                getattr(value, "usage", None),
            ]
        )

    usage: dict[str, int] = {}
    for candidate in candidates:
        if candidate is None:
            continue
        if not isinstance(candidate, dict):
            candidate = getattr(candidate, "model_dump", lambda: {})()
        if not isinstance(candidate, dict):
            continue

        nested = candidate.get("token_usage") or candidate.get("usage")
        if isinstance(nested, dict):
            candidates.append(nested)

        for key in (
            "input_tokens",
            "output_tokens",
            "total_tokens",
            "prompt_tokens",
            "completion_tokens",
            "prompt_cache_hit_tokens",
            "prompt_cache_miss_tokens",
        ):
            item = candidate.get(key)
            if isinstance(item, bool) or not isinstance(item, int):
                continue
            usage[key] = max(usage.get(key, 0), item)

        input_details = candidate.get("input_token_details")
        if isinstance(input_details, dict):
            cache_read = input_details.get("cache_read")
            if isinstance(cache_read, int) and not isinstance(cache_read, bool):
                usage["prompt_cache_hit_tokens"] = max(
                    usage.get("prompt_cache_hit_tokens", 0),
                    cache_read,
                )

        prompt_details = candidate.get("prompt_tokens_details")
        if isinstance(prompt_details, dict):
            cached_tokens = prompt_details.get("cached_tokens")
            if isinstance(cached_tokens, int) and not isinstance(cached_tokens, bool):
                usage["prompt_cache_hit_tokens"] = max(
                    usage.get("prompt_cache_hit_tokens", 0),
                    cached_tokens,
                )

    if "input_tokens" not in usage and "prompt_tokens" in usage:
        usage["input_tokens"] = usage["prompt_tokens"]
    if "output_tokens" not in usage and "completion_tokens" in usage:
        usage["output_tokens"] = usage["completion_tokens"]
    if "total_tokens" not in usage and ("input_tokens" in usage or "output_tokens" in usage):
        usage["total_tokens"] = usage.get("input_tokens", 0) + usage.get("output_tokens", 0)
    if "prompt_cache_miss_tokens" not in usage and "input_tokens" in usage:
        usage["prompt_cache_miss_tokens"] = max(
            0,
            usage["input_tokens"] - usage.get("prompt_cache_hit_tokens", 0),
        )
    return usage


async def build_agent(
    config_path: str | Path | None = None,
    workspace_dir: str | Path | None = None,
) -> Any:
    """Build and return a compiled LangGraph react agent."""
    cfg = load_llm_config(config_path)
    llm = wrap_chat_model_with_append_injection(create_chat_deepseek(cfg))
    tools = await get_all_tools(workspace_dir=workspace_dir)
    workspace = Path(workspace_dir or Path.cwd()).resolve()
    research_runtime = build_agentic_research_runtime(
        model=llm,
        tools=tools,
        workspace_root=workspace,
        config=load_agentic_research_config(),
    )
    agent = create_react_agent(
        model=llm,
        tools=_react_tools_for_runtime(tools, research_runtime),
        state_schema=None,
    )
    agent.name = "chat_agent"
    agent.agentic_research_runtime = research_runtime
    return agent


def _react_tools_for_runtime(tools: list[Any], runtime: AgenticResearchRuntime | None) -> list[Any]:
    """Keep evidence collection bounded without changing non-evidence tool authority."""
    if runtime is None:
        return list(tools)
    return [tool for tool in tools if str(getattr(tool, "name", "")) not in EVIDENCE_TOOL_NAMES]


def run_agentic_research(
    agent: Any,
    message: str,
    *,
    knowledge_policy: str | None = None,
    knowledge_mode: bool | None = None,
) -> Any | None:
    runtime = getattr(agent, "agentic_research_runtime", None)
    if runtime is None:
        return None
    return runtime.run(
        message,
        knowledge_policy=knowledge_policy,
        knowledge_mode=knowledge_mode,
    )


async def stream_agent_events(
    agent: Any,
    message: str,
    session_id: str = "default",
    history: list[dict[str, str]] | None = None,
    synthesis_context: str = "",
) -> AsyncGenerator[dict[str, Any], None]:
    """Stream agent execution events as dicts suitable for SSE serialization.

    Yields dicts with keys:
      - ``event`` - event type label (``text``, ``tool_call``, ``tool_result``, ``done``)
      - ``data`` - JSON payload (token text, tool name + args, etc.)
    """
    config = {"recursion_limit": MAX_AGENT_STEPS}
    collected_text = ""
    active_tool: str | None = None
    progress_count = 0
    run_started_at = time.monotonic()
    web_search_calls = 0
    web_fetch_calls = 0
    last_web_search_payload: dict[str, Any] | None = None
    last_web_fetch_results: list[str] = []
    tool_call_names: list[str] = []
    tool_errors: list[dict[str, str]] = []
    tool_successes: list[dict[str, str]] = []
    step_states: dict[str, dict[str, str]] = {}
    tool_call_history: list[dict[str, Any]] = []  # persists across repair passes
    emitted_tool_call_keys: set[str] = set()
    tool_result_suppression_queue: dict[str, list[bool]] = {}
    repair_passes = 0
    launched_background_subagents: list[dict[str, Any]] = []
    last_heartbeat_emitted_at = 0.0
    current_model_usage: dict[str, int] = {}
    accumulated_model_usage: dict[str, int] = {}
    latest_activity: dict[str, Any] = {}
    pending_tool_call_ids: dict[str, list[str]] = {}
    tool_call_ids_by_run_id: dict[str, str] = {}
    fallback_tool_sequence = 0
    history_fingerprint = hashlib.sha256(
        json.dumps(history or [], ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()[:16]

    def activity_frame(state: str, summary: str, **details: Any) -> dict[str, Any]:
        nonlocal latest_activity
        latest_activity = build_activity_event(
            state=state,
            summary=summary,
            elapsed_seconds=max(0, int(time.monotonic() - run_started_at)),
            evidence=str(details.get("evidence") or ""),
            judgment=str(details.get("judgment") or ""),
            next_step=str(details.get("next_step") or ""),
            progress=details.get("progress"),
        )
        return {"event": "activity", "data": json.dumps(latest_activity, ensure_ascii=False)}

    def to_jsonable(value: Any) -> Any:
        try:
            json.dumps(value, ensure_ascii=False)
            return value
        except TypeError:
            return str(value)

    def sanitize_tool_payload(value: Any) -> Any:
        if isinstance(value, dict):
            cleaned: dict[str, Any] = {}
            for key, item in value.items():
                key_str = str(key)
                if key_str in {"runtime", "config", "callbacks", "store", "context", "state"}:
                    continue
                cleaned[key_str] = sanitize_tool_payload(item)
            return cleaned
        if isinstance(value, (list, tuple)):
            return [sanitize_tool_payload(item) for item in value]
        return to_jsonable(value)

    def lossless_tool_payload(value: Any) -> Any:
        if isinstance(value, dict):
            return {str(key): lossless_tool_payload(item) for key, item in value.items()}
        if isinstance(value, (list, tuple)):
            return [lossless_tool_payload(item) for item in value]
        return to_jsonable(value)

    def runtime_tool_call_key(tool_name: str, arguments: Any) -> str:
        try:
            normalized_arguments = _normalize_tool_payload_for_key(tool_name, arguments)
            normalized = json.dumps(
                normalized_arguments or {},
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                default=str,
            )
        except Exception:
            normalized = str(arguments)
        return f"{tool_name}:{normalized}"

    def next_fallback_tool_call_id(tool_name: str) -> str:
        nonlocal fallback_tool_sequence
        fallback_tool_sequence += 1
        safe_name = re.sub(r"[^a-zA-Z0-9_.-]+", "-", tool_name).strip("-") or "tool"
        return f"fallback-{history_fingerprint}-{safe_name}-{fallback_tool_sequence}"

    def start_tool_call_id(event: dict[str, Any], tool_name: str) -> str:
        run_id = str(event.get("run_id") or "").strip()
        tool_call_id = run_id or next_fallback_tool_call_id(tool_name)
        if run_id:
            tool_call_ids_by_run_id[run_id] = tool_call_id
        pending_tool_call_ids.setdefault(tool_name, []).append(tool_call_id)
        return tool_call_id

    def end_tool_call_id(event: dict[str, Any], tool_name: str) -> str:
        run_id = str(event.get("run_id") or "").strip()
        if run_id and run_id in tool_call_ids_by_run_id:
            tool_call_id = tool_call_ids_by_run_id.pop(run_id)
            pending = pending_tool_call_ids.get(tool_name, [])
            if tool_call_id in pending:
                pending.remove(tool_call_id)
            if not pending:
                pending_tool_call_ids.pop(tool_name, None)
            return tool_call_id
        pending = pending_tool_call_ids.get(tool_name, [])
        if pending:
            tool_call_id = pending.pop(0)
            if not pending:
                pending_tool_call_ids.pop(tool_name, None)
            return tool_call_id
        return run_id or next_fallback_tool_call_id(tool_name)

    def step_for_tool(tool_name: str) -> str:
        lowered = (tool_name or "").lower()
        if lowered in {"get-current-date", "get-station-code-of-citys", "get-station-code-by-names", "get-tickets"}:
            return "data_query"
        if lowered == "feishu_login_status" or "feishu" in lowered or "lark" in lowered:
            return "target_write"
        if lowered in {"read_skill_detail", "use_skill"}:
            return "context_gathering"
        if lowered in {"read_file", "list_directory", "get_file_info", "web_search", "web_fetch", "fetch_webpage"}:
            return "context_gathering"
        if lowered in {"copy_file", "write_file", "append_file", "delete_file", "run_python_file"}:
            return "target_write" if lowered != "run_python_file" else "verification"
        return lowered or "tool_work"

    def mark_step(step: str, status: str, detail: str = "") -> None:
        step_states[step] = {
            "status": status,
            "detail": detail[:800],
        }

    def completed_step_summary() -> str:
        completed = [
            f"- {name}: {state.get('detail') or 'completed'}"
            for name, state in sorted(step_states.items())
            if state.get("status") == "completed"
        ]
        return "\n".join(completed) if completed else "- No completed subtasks recorded yet."

    def failed_step_summary() -> str:
        failed = [
            f"- {name}: {state.get('detail') or 'failed'}"
            for name, state in sorted(step_states.items())
            if state.get("status") in {"failed", "blocked"}
        ]
        return "\n".join(failed) if failed else "- No failed subtasks recorded."

    def unfinished_step_summary() -> str:
        unfinished = [
            f"- {name}: {state.get('status')} - {state.get('detail') or ''}"
            for name, state in sorted(step_states.items())
            if state.get("status") != "completed"
        ]
        return "\n".join(unfinished) if unfinished else "- No unfinished subtasks recorded."

    def merge_usage_delta(target: dict[str, int], usage: dict[str, int]) -> None:
        for key, value in usage.items():
            if isinstance(value, bool) or not isinstance(value, int):
                continue
            target[key] = target.get(key, 0) + value

    def extract_usage_metadata(value: Any) -> dict[str, int]:
        return _extract_usage_metadata(value)

    def build_usage_payload() -> dict[str, Any]:
        hit = accumulated_model_usage.get("prompt_cache_hit_tokens", 0)
        miss = accumulated_model_usage.get("prompt_cache_miss_tokens", 0)
        cache_total = hit + miss
        return {
            **accumulated_model_usage,
            "prompt_cache_total_tokens": cache_total,
            "prompt_cache_hit_rate": (hit / cache_total) if cache_total else None,
        }

    def compress_history_text(text: str, limit: int = MAX_HISTORY_MESSAGE_CHARS) -> str:
        normalized = (text or "").strip()
        if len(normalized) <= limit:
            return normalized
        head = normalized[: limit // 2]
        tail = normalized[-(limit // 3) :]
        omitted = len(normalized) - len(head) - len(tail)
        return f"{head}\n\n[... omitted {omitted} chars from earlier content ...]\n\n{tail}"

    def trim_history(history_items: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
        if not history_items:
            return []
        trimmed: list[dict[str, Any]] = []
        total_chars = 0
        # Keep the most recent turns first, then restore chronological order.
        for item in reversed(history_items):
            role = item.get("role", "")
            if role == "context_summary":
                trimmed.append(dict(item))
                continue
            if role in {"assistant_tool_calls", "tool", "legacy_tool_result", "malformed_tool_result"}:
                # Protected tool records are replayed exactly as persisted.
                trimmed.append(item)
                continue
            if role == "historical_tool_context":
                content = compress_history_text(str(item.get("content") or ""))
                projected = total_chars + len(content)
                if trimmed and (len(trimmed) >= MAX_HISTORY_MESSAGES or projected > MAX_HISTORY_TOTAL_CHARS):
                    continue
                trimmed.append({
                    "role": role,
                    "content": content,
                    "historical_tool_history": True,
                })
                total_chars = projected
                continue
            if role not in {"user", "assistant"}:
                continue
            content = compress_history_text(item.get("content", ""))
            projected = total_chars + len(content)
            if trimmed and (len(trimmed) >= MAX_HISTORY_MESSAGES or projected > MAX_HISTORY_TOTAL_CHARS):
                continue
            trimmed.append({"role": role, "content": content})
            total_chars = projected
        trimmed.reverse()
        return trimmed

    def build_search_fallback(payload: dict[str, Any] | None) -> str:
        """Build a fallback response. Uses web_fetch results when available,
        otherwise synthesizes from search snippets via a quick LLM call."""

        # --- Case 1: we have web_fetch results — use them directly ---
        if last_web_fetch_results:
            parts: list[str] = []
            for i, fetch_text in enumerate(last_web_fetch_results[:3], 1):
                parts.append(f"Source {i}:\n{fetch_text}")
            joined = "\n\n---\n\n".join(parts)

            # If the model already collected some text, prepend it
            if collected_text and should_use_collected_text_as_final(collected_text):
                return f"{collected_text}\n\n---\n\nSupporting evidence:\n{joined}"

            # Otherwise synthesize from fetch results via a quick LLM call
            try:
                from .config import create_chat_deepseek, load_llm_config
                cfg = load_llm_config()
                llm = create_chat_deepseek(cfg, temperature=0.0, streaming=False)
                system_prompt = (
                    "You are a helpful assistant. Synthesize the following web-sourced information "
                    "into a concise, well-structured answer for the user. "
                    "Include specific facts, dates, numbers, and source attribution. "
                    "Be objective and note any contradictions between sources."
                )
                user_prompt = (
                    f"User question: {message}\n\n"
                    f"Information retrieved from web pages:\n{joined}\n\n"
                    f"Based on the above, provide a comprehensive answer to the user's question."
                )
                response = llm.invoke([
                    SystemMessage(content=system_prompt),
                    HumanMessage(content=user_prompt),
                ])
                llm_answer = response.content if hasattr(response, "content") else str(response)
                if llm_answer and len(llm_answer.strip()) > 20:
                    return llm_answer.strip()
            except Exception:
                pass
            return joined

        # --- Case 2: only search snippets available ---
        if not payload:
            return "I could not find enough reliable search results. Please narrow the topic or add more specific keywords."
        results = payload.get("results") or []
        note = payload.get("note") or ""
        error = payload.get("error") or ""
        if error:
            return f"Search failed: {error}"
        if not results:
            return str(note or "I could not find enough reliable search results. Please narrow the topic or add more specific keywords.")

        # Build a quick synthesis from snippets
        snippets_text = ""
        for item in results[:5]:
            title = item.get("title", "")
            snippet = item.get("snippet", "")
            url = item.get("url", "")
            snippets_text += f"- [{title}]({url}): {snippet}\n"

        try:
            from .config import create_chat_deepseek, load_llm_config
            cfg = load_llm_config()
            llm = create_chat_deepseek(cfg, temperature=0.0, streaming=False)
            system_prompt = (
                "You are a helpful assistant. Below are search result snippets for a user's query. "
                "Synthesize them into a concise, informative answer. Include specific facts where available. "
                "If the snippets lack enough detail for a complete answer, say so honestly."
            )
            user_prompt = (
                f"User question: {message}\n\n"
                f"Search snippets:\n{snippets_text}\n\n"
                f"Based on these snippets, provide a helpful answer to the user."
            )
            response = llm.invoke([
                SystemMessage(content=system_prompt),
                HumanMessage(content=user_prompt),
            ])
            llm_answer = response.content if hasattr(response, "content") else str(response)
            if llm_answer and len(llm_answer.strip()) > 20:
                return llm_answer.strip()
        except Exception:
            pass

        # Fallback: just list sources
        lines: list[str] = [
            f"I found {len(results)} search results but could not synthesize a complete answer. Key sources:"
        ]
        for item in results[:3]:
            title = item.get("title", "Untitled")
            url = item.get("url", "")
            snippet = item.get("snippet", "")[:120]
            lines.append(f"- [{title}]({url}) — {snippet}")
        return "\n".join(lines)

    def should_use_collected_text_as_final(text: str) -> bool:
        return bool((text or "").strip())

    def has_subagent_evidence() -> bool:
        lower_names = {name.lower() for name in tool_call_names}
        return "agent" in lower_names

    def has_verification_evidence() -> bool:
        lower_names = {name.lower() for name in tool_call_names}
        return (
            "run_python_file" in lower_names
            or "verification" in lower_names
            or "agent" in lower_names and "verification" in collected_text.lower()
        )

    def has_unresolved_background_subagents() -> bool:
        return any(task.get("status") not in {"completed", "idle", "failed"} for task in launched_background_subagents)

    def output_indicates_tool_error(tool_name: str, output: str) -> bool:
        text = (output or "").strip()
        if not text:
            return False
        try:
            payload = json.loads(text)
            if isinstance(payload, dict):
                if payload.get("success") is False or payload.get("error"):
                    return True
                if str(payload.get("status") or "").lower() in {"failed", "error", "blocked"}:
                    return True
        except Exception:
            pass
        return False

    def build_retry_instruction(reason: str) -> str:
        recent_errors = tool_errors[-5:]
        if recent_errors:
            error_lines = []
            for item in recent_errors:
                preview = item.get("message", "")
                if len(preview) > 1200:
                    preview = preview[:1200] + "\n[... truncated ...]"
                error_lines.append(f"- Tool `{item.get('tool', 'tool')}` failed/returned error:\n{preview}")
            errors_text = "\n".join(error_lines)
        else:
            errors_text = "- No structured tool error captured."

        # Build a concrete inventory of every tool already called this run,
        # so the model can SEE what it already did and avoid restarting.
        history_lines: list[str] = []
        seen_tools: dict[str, int] = {}
        seen_results: dict[str, str] = {}
        for entry in tool_call_history:
            tname = entry.get("tool", "tool")
            if entry.get("phase") == "start":
                seen_tools[tname] = seen_tools.get(tname, 0) + 1
            elif entry.get("phase") == "end":
                key = f"{tname}:{entry.get('ok', True)}"
                preview = str(entry.get("result_preview", ""))[:200]
                if preview and preview not in seen_results:
                    seen_results[key] = preview

        if seen_tools:
            history_lines.append("Already-completed tool calls (DO NOT re-run these exact calls):")
            for tname, count in sorted(seen_tools.items()):
                history_lines.append(f"  - {tname}: called {count} time(s)")
        if seen_results:
            history_lines.append("Key results you already have (reuse, do not re-query):")
            for key, preview in sorted(seen_results.items()):
                history_lines.append(f"  - {key}: {preview}")

        inventory = "\n".join(history_lines) if history_lines else "- No tool call history recorded for this run."

        return (
            f"{AGENT_POLICY}\n\n"
            "IMPORTANT: You are in a CONTINUATION pass. Do NOT restart from scratch. "
            "Continue exactly where you left off. Only fix the FAILED subtask(s); "
            "do NOT repeat completed work or re-run successful tool calls.\n\n"
            f"Reason for continuation: {reason}\n\n"
            f"{inventory}\n\n"
            f"Completed subtasks that must be reused, not repeated:\n{completed_step_summary()}\n\n"
            f"Only these unfinished subtasks may be worked on:\n{unfinished_step_summary()}\n\n"
            f"Failed or blocked subtasks to repair:\n{failed_step_summary()}\n\n"
            f"Recent debug/tool errors:\n{errors_text}\n\n"
            "Follow the policy above. Re-run only corrected calls for the failed subtask and verify concrete outputs before finalizing."
        )

    def build_tool_use_continuation_instruction(reason: str) -> str:
        tool_summary = build_tool_summary()
        return (
            f"{AGENT_POLICY}\n\n"
            "IMPORTANT: You are continuing the same user request because the previous model pass ended "
            "before a usable final answer was produced. Decide the next step yourself: "
            "if more tool_use is needed, call the required tool(s) now; if no more tools are needed, "
            "provide the final answer now. Do not restart completed work or repeat identical successful tool calls.\n\n"
            f"Reason for continuation: {reason}\n\n"
            f"Tools already called in this run: {tool_summary}\n\n"
            f"Completed subtasks that must be reused, not repeated:\n{completed_step_summary()}\n\n"
            f"Unfinished subtasks, if any:\n{unfinished_step_summary()}\n\n"
            "Stop only by giving a final answer when you determine no additional tool_use is required."
        )

    def completion_gate_context() -> dict[str, Any] | None:
        """Return persisted completion context when this run should stop immediately."""
        current_run_recorded_primary = any(
            (tool_name or "").lower() == "record_primary_result"
            for tool_name in tool_call_names
        )
        if not current_run_recorded_primary:
            return None
        try:
            context = SessionStore(Path.cwd()).get_resume_context(session_id)
        except Exception:
            return None

        primary = context.get("primary_result")
        completed = context.get("completed_todos")
        unfinished = context.get("unfinished_todos")
        if not isinstance(primary, dict):
            return None
        if str(primary.get("source_tool") or "") != "record_primary_result":
            return None
        if not isinstance(completed, list) or not completed:
            return None
        if not isinstance(unfinished, list) or unfinished:
            return None
        return context

    def forced_completion_response(context: dict[str, Any]) -> str:
        primary = context.get("primary_result") if isinstance(context, dict) else {}
        if not isinstance(primary, dict):
            primary = {}
        completed = context.get("completed_todos") if isinstance(context, dict) else []
        if not isinstance(completed, list):
            completed = []

        title = str(primary.get("title") or primary.get("type") or "Result").strip()
        target = str(
            primary.get("url")
            or primary.get("path")
            or primary.get("token")
            or primary.get("table_id")
            or ""
        ).strip()
        summary = str(primary.get("summary") or "").strip()

        lines = ["任务已完成。", "", "**主要结果**"]
        if target:
            lines.append(f"- {title}: {target}")
        else:
            lines.append(f"- {title}")
        if summary:
            lines.append(f"- 摘要: {summary}")
        if completed:
            lines.extend(["", "**已完成事项**"])
            for todo in completed[:8]:
                if not isinstance(todo, dict):
                    continue
                label = str(todo.get("content") or todo.get("task_id") or "").strip()
                detail = str(todo.get("details") or todo.get("result_ref") or "completed").strip()
                if label:
                    lines.append(f"- {label}: {detail}")
        return "\n".join(lines).strip()

    def looks_incomplete(final_text: str) -> bool:
        return not bool((final_text or "").strip())

    def audit_completion_with_policy(final_text: str) -> dict[str, Any]:
        try:
            cfg = load_llm_config()
            llm = create_chat_deepseek(cfg, temperature=0.0, streaming=False)
            tool_summary = build_tool_summary()
            error_summary = json.dumps(tool_errors[-5:], ensure_ascii=False, indent=2)
            prompt = (
                f"{AGENT_POLICY}\n\n"
                "Audit the current agent run. Return only JSON with keys: "
                "complete (boolean), reason (string), required_next_action (string), status (completed|failed|needs_retry).\n\n"
                f"User request:\n{message}\n\n"
                f"Final text:\n{final_text[:4000]}\n\n"
                f"Tool summary:\n{tool_summary}\n\n"
                f"Recent tool errors:\n{error_summary}\n"
            )
            response = llm.invoke([
                SystemMessage(content="You are a strict completion auditor. Return valid JSON only."),
                HumanMessage(content=prompt),
            ])
            raw = response.content if hasattr(response, "content") else str(response)
            parsed = json.loads(str(raw).strip())
            if isinstance(parsed, dict):
                return parsed
        except Exception as exc:
            return {
                "complete": bool((final_text or "").strip()) and not tool_errors,
                "reason": f"policy audit fallback after error: {exc}",
                "required_next_action": "continue or report concrete blocker",
                "status": "completed" if final_text.strip() and not tool_errors else "needs_retry",
            }
        return {
            "complete": bool((final_text or "").strip()) and not tool_errors,
            "reason": "policy audit returned invalid shape",
            "required_next_action": "continue or report concrete blocker",
            "status": "completed" if final_text.strip() and not tool_errors else "needs_retry",
        }
    def build_tool_summary() -> str:
        if not tool_call_names:
            return "none"
        counts: dict[str, int] = {}
        for tool_name in tool_call_names:
            counts[tool_name] = counts.get(tool_name, 0) + 1
        return ", ".join(
            f"{name} x{count}" if count > 1 else name
            for name, count in sorted(counts.items())
        )

    def fallback_exception_for_user(exc_text: str) -> str:
        lines = [line.strip() for line in (exc_text or "").splitlines() if line.strip()]
        exception_line = ""
        for line in reversed(lines):
            if re.match(r"^[A-Za-z_][A-Za-z0-9_.]*(Error|Exception):", line):
                exception_line = line
                break
        if not exception_line:
            exception_line = lines[-1] if lines else "Unknown execution error."
        lowered = exception_line.lower()
        if "could not determine the requested operation" in lowered:
            diagnosis = "The skill runner could not map the request to a supported standardized command."
        elif "missing <token|url>" in lowered:
            diagnosis = "The command is missing a required token or URL."
        elif "exceeded" in lowered:
            diagnosis = "A runtime budget or tool-call limit was exceeded."
        else:
            diagnosis = "A tool or subprocess failed and the task did not complete."
        return (
            "任务未完成。\n\n"
            "**错误分析**\n"
            f"- 异常摘要: `{exception_line}`\n"
            f"- 可能原因: {diagnosis}\n"
            "- 处理要求: 不应直接返回原始 traceback，应修正工具命令、参数或执行路径后继续。\n\n"
            "**建议下一步**\n"
            "- 根据异常摘要调整调用；如果是命令格式问题，改用标准 CLI 命令后重试。"
        )

    def inject_pending_append_commands() -> list[dict[str, Any]]:
        run_id = current_run_id()
        if not run_id:
            return []
        return _inject_pending_append_commands_into_messages(session_id, run_id, messages)

    def append_injected_debug_events(commands: list[dict[str, Any]]) -> list[dict[str, Any]]:
        events: list[dict[str, Any]] = []
        for command in commands:
            events.append(
                {
                    "event": "debug",
                    "data": json.dumps(
                        {
                            "stage": "append_command_injected",
                            "message": "Injected appended instruction before the next model call.",
                            "elapsed_seconds": max(0, int(time.monotonic() - run_started_at)),
                            "session_id": session_id,
                            "run_id": command.get("run_id"),
                            "append_id": command.get("append_id"),
                            "status": command.get("status"),
                            "sequence": command.get("sequence"),
                        },
                        ensure_ascii=False,
                    ),
                }
            )
        return events

    async def summarize_exception_for_user(exc_text: str) -> str:
        context = build_exception_analysis_context(
            exc_text=exc_text,
            active_tool=active_tool,
            tool_call_history=tool_call_history,
            tool_errors=tool_errors,
            latest_activity=latest_activity,
            partial_response=collected_text,
        )

        def invoke_analysis(payload: dict[str, Any]) -> Any:
            cfg = load_llm_config()
            llm = create_chat_deepseek(cfg, temperature=0.0, streaming=False)
            prompt = (
                "You explain an agent failure to an end user. Return JSON only with "
                "what_happened, likely_cause, completion_status, next_step. "
                "Use concise Chinese, only state evidence in the context, and never include raw diagnostics, secrets, or tracebacks.\n\n"
                + json.dumps(payload, ensure_ascii=False)
            )
            return llm.invoke([SystemMessage(content="Return valid JSON only."), HumanMessage(content=prompt)])

        analysis = await analyze_exception_for_user(context, lambda payload: asyncio.to_thread(invoke_analysis, payload))
        return format_exception_analysis(analysis) if analysis else fallback_exception_for_user(exc_text)

    def ensure_completion_summary(
        final_text: str,
        *,
        status: str,
        failure_reason: str = "",
    ) -> str:
        body = (final_text or "").strip()
        lower_body = body.lower()
        if "执行摘要" in body or "execution summary" in lower_body:
            return body

        elapsed_seconds = max(0, int(time.monotonic() - run_started_at))
        verification_note = "yes" if has_verification_evidence() else "not detected"
        subagent_note = "yes" if has_subagent_evidence() else "not used"
        lines = [
            "**执行摘要**",
            f"- 状态: {status}",
            f"- 用时: {elapsed_seconds}s",
            f"- 工具调用: {build_tool_summary()}",
            f"- 子 Agent: {subagent_note}",
            f"- 验证步骤: {verification_note}",
        ]
        if failure_reason:
            lines.append(f"- 失败原因: {failure_reason}")
        if has_unresolved_background_subagents():
            lines.append("- 未完成项: still waiting for background subagent completion")

        summary = "\n".join(lines)
        return f"{body}\n\n{summary}" if body else summary

    def looks_incomplete(final_text: str) -> bool:
        normalized = (final_text or "").strip().lower()
        if not normalized:
            return True
        markers = [
            "still waiting",
            "let me",
            "i will",
            "i'll",
            "需要进一步",
            "接下来",
            "我会",
            "还需要",
            "正在",
        ]
        return any(marker in normalized for marker in markers)

    messages: list[BaseMessage] = [SystemMessage(content=SYSTEM_PROMPT)]
    if AGENT_POLICY:
        messages.append(SystemMessage(content=f"Agent behavior policy:\n{AGENT_POLICY}"))
    memory_context = ""
    try:
        memory_config = load_agent_memory_config(workspace_dir=Path.cwd())
        memory_context = MemoryContextProvider.from_config(memory_config).get_context()
    except Exception:
        memory_context = ""
    if memory_context:
        messages.append(SystemMessage(content=memory_context))
    if synthesis_context:
        messages.append(SystemMessage(content=synthesis_context[:24_000]))
    skill_catalog_text = get_skill_catalog_text(Path.cwd())
    if skill_catalog_text:
        messages.append(
            SystemMessage(
                content=(
                    "Skill catalog summary:\n"
                    f"{skill_catalog_text}\n"
                    "Decide whether a skill is needed from this summary first. "
                    "If needed, read the full skill detail before executing it."
                )
            )
        )
    resume_context = {}
    try:
        resume_context = SessionStore(Path.cwd()).get_resume_context(session_id)
    except Exception:
        resume_context = {}
    resume_text = _format_resume_context(resume_context)
    if resume_text:
        messages.append(SystemMessage(content=resume_text))
    raw_history = [dict(item) for item in (history or []) if isinstance(item, dict)]
    effective_history: list[dict[str, Any]] = raw_history
    effective_message = message
    detected_image_paths = extract_image_paths(message, Path.cwd())
    if detected_image_paths:
        image_tool_name = "analyze_images" if len(detected_image_paths) > 1 else "analyze_image"
        image_tool_call_id = next_fallback_tool_call_id(image_tool_name)
        tool_call_names.append(image_tool_name)
        image_arguments = {
            "paths": detected_image_paths,
            "prompt": "Automatically analyze attached images before main-agent reasoning.",
        }
        yield {
            "event": "tool_transcript_call",
            "data": json.dumps(
                {
                    "name": image_tool_name,
                    "tool_call_id": image_tool_call_id,
                    "arguments": image_arguments,
                },
                ensure_ascii=False,
            ),
        }
        yield {
            "event": "tool_call",
            "data": json.dumps(
                {
                    "name": image_tool_name,
                    "tool_call_id": image_tool_call_id,
                    "arguments": image_arguments,
                },
                ensure_ascii=False,
            ),
        }
        yield {
            "event": "progress",
            "data": json.dumps(
                {
                    "message": f"Analyzing {len(detected_image_paths)} attached image(s).",
                    "elapsed_seconds": max(0, int(time.monotonic() - run_started_at)),
                    "active_tool": image_tool_name,
                },
                ensure_ascii=False,
            ),
        }
        try:
            vision_result = await asyncio.to_thread(
                analyze_image_files,
                detected_image_paths,
                (
                    "Analyze the attached image or images for the user's request. "
                    "Extract visible text, important objects, layout, relationships, "
                    "errors, charts, and any evidence needed for an accurate answer.\n\n"
                    f"User request:\n{message[:4000]}"
                ),
                Path.cwd(),
            )
            mark_step("image_analysis", "completed", "Multimodal image analysis succeeded")
            effective_message = (
                f"{message}\n\n"
                "Automatically generated multimodal analysis follows. Treat it as "
                "visual evidence, reconcile it with other files or tools, and provide "
                "the final answer yourself.\n\n"
                f"{vision_result}"
            )
            yield {
                "event": "tool_transcript_result",
                "data": json.dumps(
                    {
                        "name": image_tool_name,
                        "tool_call_id": image_tool_call_id,
                        "content": vision_result,
                    },
                    ensure_ascii=False,
                ),
            }
            yield {
                "event": "tool_result",
                "data": json.dumps(
                    {
                        "name": image_tool_name,
                        "tool_call_id": image_tool_call_id,
                        "content": vision_result,
                    },
                    ensure_ascii=False,
                ),
            }
        except VisionConfigurationError as exc:
            mark_step("image_analysis", "blocked", str(exc))
            effective_message = (
                f"{message}\n\n"
                "Image analysis was requested but the multimodal model is not configured. "
                f"Configuration error: {exc}"
            )
            yield {
                "event": "tool_transcript_result",
                "data": json.dumps(
                    {
                        "name": image_tool_name,
                        "tool_call_id": image_tool_call_id,
                        "content": json.dumps(
                            {"success": False, "error": str(exc)},
                            ensure_ascii=False,
                        ),
                    },
                    ensure_ascii=False,
                ),
            }
            yield {
                "event": "tool_result",
                "data": json.dumps(
                    {
                        "name": image_tool_name,
                        "tool_call_id": image_tool_call_id,
                        "content": json.dumps(
                            {"success": False, "error": str(exc)},
                            ensure_ascii=False,
                        ),
                    },
                    ensure_ascii=False,
                ),
            }
        except Exception as exc:
            mark_step("image_analysis", "failed", str(exc))
            effective_message = (
                f"{message}\n\n"
                f"Automatic image analysis failed: {exc}. "
                "Continue with available evidence or retry with the analyze_image tool."
            )
            yield {
                "event": "tool_transcript_result",
                "data": json.dumps(
                    {
                        "name": image_tool_name,
                        "tool_call_id": image_tool_call_id,
                        "content": json.dumps(
                            {"success": False, "error": str(exc)},
                            ensure_ascii=False,
                        ),
                    },
                    ensure_ascii=False,
                ),
            }
            yield {
                "event": "tool_result",
                "data": json.dumps(
                    {
                        "name": image_tool_name,
                        "tool_call_id": image_tool_call_id,
                        "content": json.dumps(
                            {"success": False, "error": str(exc)},
                            ensure_ascii=False,
                        ),
                    },
                    ensure_ascii=False,
                ),
            }

    model_config = load_llm_config()
    model_name = model_config.get("model", "")
    compaction_settings = load_context_compaction_config()
    pre_compaction_metrics = _estimate_context_metrics(
        raw_history,
        effective_message=effective_message,
        skill_catalog_text=skill_catalog_text,
        resume_text=resume_text,
        memory_context=memory_context,
    )
    pre_capacity_info = check_context_capacity(
        pre_compaction_metrics["context_token_estimate"],
        model_name,
    )
    pre_partition = partition_history(
        raw_history,
        retain_recent_turns=int(compaction_settings.get("retain_recent_turns", 3)),
    )
    pre_triggered = should_compact_context(
        pre_compaction_metrics["context_token_estimate"],
        pre_capacity_info["model_context_window"],
        int(compaction_settings.get("trigger_remaining_tokens", 20_000)),
    )
    if compaction_settings.get("enabled", True) and pre_triggered and pre_partition.eligible_items:
        yield {
            "event": "debug",
            "data": json.dumps(
                {
                    "stage": "context_compaction_started",
                    "message": "Context compaction started.",
                    "session_id": session_id,
                    "context_token_estimate_before": pre_compaction_metrics["context_token_estimate"],
                    "remaining_tokens_before": pre_capacity_info["remaining_tokens"],
                    "protected_turn_count": min(
                        len(pre_partition.completed_turns),
                        int(compaction_settings.get("retain_recent_turns", 3)),
                    ),
                    "eligible_turn_count": max(
                        0,
                        len(pre_partition.completed_turns)
                        - int(compaction_settings.get("retain_recent_turns", 3)),
                    ),
                },
                ensure_ascii=False,
            ),
        }

    try:
        effective_history, compaction_details = await _compact_history_if_needed(
            raw_history,
            session_id=session_id,
            model_config=model_config,
            model_context_window=pre_capacity_info["model_context_window"],
            context_token_estimate=pre_compaction_metrics["context_token_estimate"],
            settings=compaction_settings,
        )
    except ContextCompactionError as exc:
        yield {
            "event": "debug",
            "data": json.dumps(
                {
                    "stage": "context_compaction_failed",
                    "message": str(exc),
                    "code": exc.code,
                    "session_id": session_id,
                    "context_token_estimate_before": pre_compaction_metrics["context_token_estimate"],
                    "remaining_tokens_before": pre_capacity_info["remaining_tokens"],
                },
                ensure_ascii=False,
            ),
        }
        yield {
            "event": "error",
            "data": json.dumps(
                {
                    "code": exc.code,
                    "message": str(exc),
                },
                ensure_ascii=False,
            ),
        }
        yield {"event": "done", "data": json.dumps(str(exc), ensure_ascii=False)}
        return

    # A validated compaction projection already contains the complete protected
    # turns. Ordinary lossy trimming must not rewrite that protected source.
    if not compaction_details.get("triggered"):
        effective_history = trim_history(effective_history)
    context_metrics = _estimate_context_metrics(
        effective_history,
        effective_message=effective_message,
        skill_catalog_text=skill_catalog_text,
        resume_text=resume_text,
        memory_context=memory_context,
    )
    history_payloads = context_metrics["history_payloads"]
    protected_tool_chars = context_metrics["protected_tool_chars"]
    context_char_count = context_metrics["context_char_count"]
    context_message_count = context_metrics["context_message_count"]
    context_token_estimate = context_metrics["context_token_estimate"]

    if compaction_details.get("skip_reason") == "unknown_context_window":
        yield {
            "event": "debug",
            "data": json.dumps(
                {
                    "stage": "skipped_unknown_context_window",
                    "message": "Context compaction skipped because the model context window is unknown.",
                    "session_id": session_id,
                },
                ensure_ascii=False,
            ),
        }
    elif compaction_details.get("cache_hit"):
        yield {
            "event": "debug",
            "data": json.dumps(
                {
                    "stage": "context_compaction_reused",
                    "message": "Validated context summary cache reused.",
                    "session_id": session_id,
                    "context_token_estimate_after": context_token_estimate,
                    "remaining_tokens_after": (
                        pre_capacity_info["model_context_window"] - context_token_estimate
                        if pre_capacity_info["model_context_window"] is not None
                        else None
                    ),
                    "protected_turn_count": compaction_details.get("protected_turn_count", 0),
                    "eligible_turn_count": compaction_details.get("eligible_turn_count", 0),
                    "cache_hit": True,
                },
                ensure_ascii=False,
            ),
        }
    elif compaction_details.get("triggered"):
        yield {
            "event": "debug",
            "data": json.dumps(
                {
                    "stage": "context_compaction_completed",
                    "message": "Context compaction completed.",
                    "session_id": session_id,
                    "context_token_estimate_after": context_token_estimate,
                    "remaining_tokens_after": (
                        pre_capacity_info["model_context_window"] - context_token_estimate
                        if pre_capacity_info["model_context_window"] is not None
                        else None
                    ),
                    "protected_turn_count": compaction_details.get("protected_turn_count", 0),
                    "eligible_turn_count": compaction_details.get("eligible_turn_count", 0),
                    "cache_hit": False,
                    "model_calls": compaction_details.get("model_calls", 0),
                },
                ensure_ascii=False,
            ),
        }
    if protected_tool_chars and context_char_count > MAX_HISTORY_TOTAL_CHARS:
        capacity_message = (
            "Protected tool history exceeds the configured context budget; "
            "no tool arguments or results were truncated."
        )
        capacity_code = (
            "context_compaction_capacity_exceeded"
            if compaction_details.get("triggered")
            else "protected_context_capacity_exceeded"
        )
        if compaction_details.get("triggered"):
            yield {
                "event": "debug",
                "data": json.dumps(
                    {
                        "stage": "context_compaction_failed",
                        "message": capacity_message,
                        "code": capacity_code,
                        "session_id": session_id,
                        "context_token_estimate_after": context_metrics["context_token_estimate"],
                        "protected_tool_chars": protected_tool_chars,
                    },
                    ensure_ascii=False,
                ),
            }
        yield {
            "event": "error",
            "data": json.dumps(
                {
                    "code": capacity_code,
                    "message": capacity_message,
                    "context_chars": context_char_count,
                    "protected_tool_chars": protected_tool_chars,
                    "context_budget_chars": MAX_HISTORY_TOTAL_CHARS,
                },
                ensure_ascii=False,
            ),
        }
        yield {"event": "done", "data": json.dumps(capacity_message, ensure_ascii=False)}
        return

    malformed_protected_entries = [
        entry for entry in effective_history
        if entry.get("role") == "malformed_tool_result"
    ]
    if malformed_protected_entries:
        yield {
            "event": "debug",
            "data": json.dumps(
                {
                    "stage": "malformed_protected_tool_history",
                    "message": "Malformed protected tool results were preserved as diagnostics; native replay was skipped for those results.",
                    "count": len(malformed_protected_entries),
                },
                ensure_ascii=False,
            ),
        }

    if effective_history:
        _append_native_history_messages(messages, effective_history)

    messages.append(HumanMessage(content=effective_message))

    _repair_dangling_tool_call_messages(messages)
    capacity_info = check_context_capacity(context_token_estimate, model_name)
    if compaction_details.get("triggered") and capacity_info["is_exceeded"]:
        capacity_message = (
            "Context compaction completed, but the protected recent history and required prompt "
            "still exceed the configured model context window."
        )
        yield {
            "event": "debug",
            "data": json.dumps(
                {
                    "stage": "context_compaction_failed",
                    "message": capacity_message,
                    "code": "context_compaction_capacity_exceeded",
                    "session_id": session_id,
                    "context_token_estimate_after": context_token_estimate,
                    "model_context_window": capacity_info["model_context_window"],
                },
                ensure_ascii=False,
            ),
        }
        yield {
            "event": "error",
            "data": json.dumps(
                {
                    "code": "context_compaction_capacity_exceeded",
                    "message": capacity_message,
                    "context_token_estimate": context_token_estimate,
                    "model_context_window": capacity_info["model_context_window"],
                },
                ensure_ascii=False,
            ),
        }
        yield {"event": "done", "data": json.dumps(capacity_message, ensure_ascii=False)}
        return

    injected_append_commands = inject_pending_append_commands()
    event_stream = agent.astream_events(
        {"messages": messages},
        config=config,
        version="v2",
    ).__aiter__()

    yield activity_frame(
        "understanding",
        "正在理解您的任务并准备处理步骤。",
        next_step="开始检查可用信息和所需操作。",
        progress={"current": 1, "total": 3, "label": "理解任务"},
    )

    yield {
        "event": "debug",
        "data": json.dumps(
            {
                "stage": "agent_start",
                "message": "Agent run started.",
                "elapsed_seconds": 0,
                "session_id": session_id,
                "max_steps": MAX_AGENT_STEPS,
                "context_messages": context_message_count,
                "context_chars": context_char_count,
                "context_token_estimate": context_token_estimate,
                "model_context_window": capacity_info["model_context_window"],
                "remaining_tokens": capacity_info["remaining_tokens"],
                "is_context_exceeded": capacity_info["is_exceeded"],
            },
            ensure_ascii=False,
        ),
    }
    for append_event in append_injected_debug_events(injected_append_commands):
        yield append_event

    heartbeat_interval = 1.0
    pending_event: asyncio.Task[Any] | None = None
    while True:
        if pending_event is None:
            pending_event = asyncio.create_task(event_stream.__anext__())
        try:
            event = await asyncio.wait_for(asyncio.shield(pending_event), timeout=heartbeat_interval)
            pending_event = None
        except asyncio.TimeoutError:
            progress_count += 1
            elapsed_seconds = max(1, int(time.monotonic() - run_started_at))
            message_text = (
                f"Still waiting for tool `{active_tool}` to return..."
                if active_tool
                else "Still waiting for the model to respond..."
            )
            now = time.monotonic()
            if now - last_heartbeat_emitted_at < HEARTBEAT_EMIT_INTERVAL_SECONDS:
                continue
            last_heartbeat_emitted_at = now
            yield {
                "event": "progress",
                "data": json.dumps(
                    {
                        "message": message_text,
                        "elapsed_seconds": elapsed_seconds,
                        "active_tool": active_tool,
                    },
                    ensure_ascii=False,
                ),
            }
            yield activity_frame(
                "working",
                "正在等待当前操作返回结果。",
                next_step="收到结果后继续判断下一步。",
                progress={"current": 2, "total": 3, "label": "执行任务"},
            )
            yield {
                "event": "debug",
                "data": json.dumps(
                    {
                        "stage": "heartbeat",
                        "message": message_text,
                        "elapsed_seconds": elapsed_seconds,
                        "active_tool": active_tool,
                    },
                    ensure_ascii=False,
                ),
            }
            continue
        except StopAsyncIteration:
            pending_event = None
            break
        except Exception as exc:
            if repair_passes < MAX_AGENT_REPAIR_PASSES:
                repair_passes += 1
                tool_errors.append({"tool": active_tool or "agent_stream", "message": str(exc)})
                messages.append(HumanMessage(content=build_retry_instruction(f"agent event stream raised: {exc}")))
                _repair_dangling_tool_call_messages(messages)
                injected_append_commands = inject_pending_append_commands()
                event_stream = agent.astream_events(
                    {"messages": messages},
                    config=config,
                    version="v2",
                ).__aiter__()
                pending_event = None
                active_tool = None
                yield {
                    "event": "debug",
                    "data": json.dumps(
                        {
                            "stage": "agent_error_retry",
                            "message": "Agent/tool error captured; retrying with the error context.",
                            "elapsed_seconds": max(0, int(time.monotonic() - run_started_at)),
                            "repair_pass": repair_passes,
                            "error": str(exc),
                        },
                        ensure_ascii=False,
                    ),
                }
                yield activity_frame(
                    "judging",
                    "已发现执行问题，正在根据已有信息调整处理方式。",
                    next_step="重试未完成的步骤。",
                    progress={"current": 2, "total": 3, "label": "调整处理"},
                )
                for append_event in append_injected_debug_events(injected_append_commands):
                    yield append_event
                continue
            error_message = await summarize_exception_for_user(str(exc))
            yield activity_frame(
                "blocked",
                "任务因执行问题暂时受阻。",
                judgment=error_message,
                next_step="请根据错误分析调整后重试。",
                progress={"current": 3, "total": 3, "label": "处理受阻"},
            )
            yield {
                "event": "error",
                "data": json.dumps(
                    {
                        "message": error_message,
                        "elapsed_seconds": max(0, int(time.monotonic() - run_started_at)),
                    },
                    ensure_ascii=False,
                ),
            }
            fallback_text = collected_text.strip() or error_message
            yield {
                "event": "done",
                "data": json.dumps(
                    ensure_completion_summary(fallback_text, status="failed", failure_reason="agent/tool execution error"),
                    ensure_ascii=False,
                ),
            }
            return

        kind = event.get("event", "")
        name = event.get("name", "")
        data = event.get("data", {})

        if kind == "on_chat_model_stream":
            chunk = data.get("chunk", None)
            if chunk is not None:
                usage = extract_usage_metadata(chunk)
                if usage:
                    current_model_usage = usage
                token = chunk.content if isinstance(chunk.content, str) else ""
                if token:
                    collected_text += token
                    yield {"event": "text", "data": json.dumps(token, ensure_ascii=False)}

        elif kind == "on_tool_start":
            transcript_input_data = lossless_tool_payload(data.get("input", {}))
            input_data = sanitize_tool_payload(data.get("input", {}))
            tool_call_id = start_tool_call_id(event, name)
            display_key = runtime_tool_call_key(name, input_data)
            is_duplicate_display_call = display_key in emitted_tool_call_keys
            tool_result_suppression_queue.setdefault(name, []).append(is_duplicate_display_call)
            if not is_duplicate_display_call:
                emitted_tool_call_keys.add(display_key)
                tool_call_names.append(name)
            tool_call_history.append(
                {
                    "phase": "start",
                    "tool": name,
                    "tool_call_id": tool_call_id,
                    "arguments": input_data,
                    "at": time.monotonic(),
                }
            )
            yield {
                "event": "tool_transcript_call",
                "data": json.dumps(
                    {"name": name, "tool_call_id": tool_call_id, "arguments": transcript_input_data},
                    ensure_ascii=False,
                    default=str,
                ),
            }
            yield activity_frame(
                "working",
                f"正在执行 {name}。",
                next_step="等待该操作返回结果。",
                progress={"current": 2, "total": 3, "label": "执行任务"},
            )
            if name == "web_search":
                web_search_calls += 1
                if web_search_calls > MAX_WEB_SEARCH_CALLS:
                    fallback_message = (
                        collected_text
                        if should_use_collected_text_as_final(collected_text) and not last_web_search_payload
                        else build_search_fallback(last_web_search_payload)
                    )
                    yield {
                        "event": "debug",
                        "data": json.dumps(
                            {
                                "stage": "search_budget_exhausted",
                                "message": f"Blocked repeated web_search call. Max allowed per request: {MAX_WEB_SEARCH_CALLS}.",
                                "elapsed_seconds": max(0, int(time.monotonic() - run_started_at)),
                                "tool": name,
                                "arguments": input_data,
                            },
                            ensure_ascii=False,
                        ),
                    }
                    yield {
                        "event": "done",
                        "data": json.dumps(
                            ensure_completion_summary(
                                fallback_message,
                                status="failed",
                                failure_reason=f"web_search exceeded {MAX_WEB_SEARCH_CALLS} calls",
                            ),
                            ensure_ascii=False,
                        ),
                    }
                    return
            elif name in {"web_fetch", "fetch_webpage"}:
                web_fetch_calls += 1
                if web_fetch_calls > MAX_WEB_FETCH_CALLS:
                    fallback_message = (
                        collected_text
                        if should_use_collected_text_as_final(collected_text)
                        else build_search_fallback(last_web_search_payload)
                    )
                    yield {
                        "event": "debug",
                        "data": json.dumps(
                            {
                                "stage": "fetch_budget_exhausted",
                                "message": f"Blocked repeated web_fetch call. Max allowed per request: {MAX_WEB_FETCH_CALLS}.",
                                "elapsed_seconds": max(0, int(time.monotonic() - run_started_at)),
                                "tool": name,
                                "arguments": input_data,
                            },
                            ensure_ascii=False,
                        ),
                    }
                    yield {
                        "event": "done",
                        "data": json.dumps(
                            ensure_completion_summary(
                                fallback_message,
                                status="failed",
                                failure_reason=f"web_fetch exceeded {MAX_WEB_FETCH_CALLS} calls",
                            ),
                            ensure_ascii=False,
                        ),
                    }
                    return
            active_tool = name
            progress_count = 0
            elapsed_seconds = max(0, int(time.monotonic() - run_started_at))
            if not is_duplicate_display_call:
                yield {
                    "event": "tool_call",
                    "data": json.dumps(
                        {"name": name, "tool_call_id": tool_call_id, "arguments": input_data},
                        default=str,
                        ensure_ascii=False,
                    ),
                }
            yield {
                "event": "debug",
                "data": json.dumps(
                    {
                        "stage": "tool_duplicate_suppressed" if is_duplicate_display_call else "tool_start",
                        "message": (
                            f"Suppressed duplicate tool event for `{name}`; runtime will reuse or block as needed."
                            if is_duplicate_display_call
                            else f"Calling tool `{name}`."
                        ),
                        "elapsed_seconds": elapsed_seconds,
                        "tool": name,
                        "arguments": input_data,
                    },
                    ensure_ascii=False,
                ),
            }

        elif kind == "on_tool_end":
            output = data.get("output", "")
            output_str = output.content if isinstance(output, BaseMessage) else str(output)
            tool_name = name or active_tool or "tool"
            tool_call_id = end_tool_call_id(event, tool_name)
            is_error = output_indicates_tool_error(tool_name, output_str)
            if is_error:
                tool_errors.append({"tool": tool_name, "message": output_str[:4000]})
                mark_step(step_for_tool(tool_name), "failed", f"`{tool_name}` failed: {output_str[:400]}")
            else:
                tool_successes.append({"tool": tool_name, "message": output_str[:800]})
                mark_step(step_for_tool(tool_name), "completed", f"`{tool_name}` succeeded")
            # Record result into history so repair passes can reference it
            result_preview = output_str[:500] if output_str else "(empty)"
            tool_call_history.append(
                {
                    "phase": "end",
                    "tool": tool_name,
                    "tool_call_id": tool_call_id,
                    "ok": not is_error,
                    "result_preview": result_preview,
                    "at": time.monotonic(),
                }
            )
            if tool_name == "web_search":
                try:
                    last_web_search_payload = json.loads(output_str)
                except Exception:
                    last_web_search_payload = None
            elif tool_name in {"web_fetch", "fetch_webpage"}:
                try:
                    fetch_payload = json.loads(output_str)
                    fetch_result = fetch_payload.get("result", "")
                    if fetch_result and len(fetch_result) > 80:
                        last_web_fetch_results.append(fetch_result)
                except Exception:
                    pass
            elif tool_name == "Agent":
                try:
                    agent_payload = json.loads(output_str)
                    if agent_payload.get("status") == "async_launched":
                        launched_background_subagents.append(agent_payload)
                except Exception:
                    pass
            active_tool = None
            progress_count = 0
            elapsed_seconds = max(0, int(time.monotonic() - run_started_at))
            suppression_queue = tool_result_suppression_queue.get(tool_name, [])
            suppress_result = bool(suppression_queue.pop(0)) if suppression_queue else False
            if not suppression_queue:
                tool_result_suppression_queue.pop(tool_name, None)
            yield {
                "event": "tool_transcript_result",
                "data": json.dumps(
                    {"name": tool_name, "tool_call_id": tool_call_id, "content": output_str},
                    ensure_ascii=False,
                ),
            }
            if not suppress_result:
                yield {
                    "event": "tool_result",
                    "data": json.dumps(
                        {"name": tool_name, "tool_call_id": tool_call_id, "content": output_str},
                        ensure_ascii=False,
                    ),
                }
            yield {
                "event": "debug",
                "data": json.dumps(
                    {
                        "stage": "tool_duplicate_result_suppressed" if suppress_result else "tool_end",
                        "message": (
                            f"Suppressed duplicate result event for `{tool_name}`."
                            if suppress_result
                            else f"Tool `{tool_name}` returned."
                        ),
                        "elapsed_seconds": elapsed_seconds,
                        "tool": tool_name,
                        "content_preview": output_str[:300],
                    },
                    ensure_ascii=False,
                ),
            }
            forced_context = completion_gate_context()
            if forced_context:
                final_text = forced_completion_response(forced_context)
                yield {
                    "event": "debug",
                    "data": json.dumps(
                        {
                            "stage": "forced_final_after_completion_gate",
                            "message": "All task plan todos are completed and a primary result is registered; stopping without another model/tool pass.",
                            "elapsed_seconds": elapsed_seconds,
                            "tool": tool_name,
                        },
                        ensure_ascii=False,
                    ),
                }
                yield {
                    "event": "done",
                    "data": json.dumps(
                        ensure_completion_summary(final_text, status="completed"),
                        ensure_ascii=False,
                    ),
                }
                return

        elif kind == "on_chat_model_start":
            current_model_usage = {}
            yield {
                "event": "debug",
                "data": json.dumps(
                    {
                        "stage": "model_start",
                        "message": "Model started generating.",
                        "elapsed_seconds": max(0, int(time.monotonic() - run_started_at)),
                    },
                    ensure_ascii=False,
                ),
            }

        elif kind == "on_chat_model_end":
            usage = extract_usage_metadata(data.get("output"))
            if usage:
                current_model_usage = usage
            if current_model_usage:
                merge_usage_delta(accumulated_model_usage, current_model_usage)
                yield {
                    "event": "debug",
                    "data": json.dumps(
                        {
                            "stage": "model_usage",
                            "message": "Model usage received.",
                            "elapsed_seconds": max(0, int(time.monotonic() - run_started_at)),
                            "usage": build_usage_payload(),
                        },
                        ensure_ascii=False,
                    ),
                }
                current_model_usage = {}
            yield {
                "event": "debug",
                "data": json.dumps(
                    {
                        "stage": "model_end",
                        "message": "Model finished generating.",
                        "elapsed_seconds": max(0, int(time.monotonic() - run_started_at)),
                    },
                    ensure_ascii=False,
                ),
            }

        elif kind == "on_chain_start" and name == "LangGraph":
            yield {
                "event": "debug",
                "data": json.dumps(
                    {
                        "stage": "graph_start",
                        "message": "LangGraph execution started.",
                        "elapsed_seconds": max(0, int(time.monotonic() - run_started_at)),
                    },
                    ensure_ascii=False,
                ),
            }

        elif kind == "on_chain_end" and name == "LangGraph":
            if launched_background_subagents:
                from .tools import get_subagent_task

                for _ in range(MAX_BACKGROUND_SUBAGENT_POLLS):
                    updated = []
                    for task in launched_background_subagents:
                        agent_id = task.get("agent_id", "")
                        if not agent_id:
                            continue
                        try:
                            task_payload = json.loads(get_subagent_task.invoke({"agent_id": agent_id}))
                        except Exception:
                            task_payload = task
                        updated.append(task_payload)
                    launched_background_subagents[:] = updated
                    if not has_unresolved_background_subagents():
                        background_notes = []
                        for task in launched_background_subagents:
                            if task.get("result"):
                                background_notes.append(
                                    f"[Background {task.get('subagent_type', 'subagent')} {task.get('agent_id', '')}] {task.get('result')}"
                                )
                        if background_notes:
                            collected_text += "\n\n" + "\n".join(background_notes)
                        break
                    await asyncio.sleep(1)

            final_text = collected_text
            if not should_use_collected_text_as_final(final_text) and last_web_search_payload:
                final_text = build_search_fallback(last_web_search_payload)
            forced_context = completion_gate_context()
            if forced_context and looks_incomplete(final_text):
                final_text = forced_completion_response(forced_context)
                yield {
                    "event": "debug",
                    "data": json.dumps(
                        {
                            "stage": "forced_final_after_completion_gate",
                            "message": "All task plan todos are completed and a primary result is registered; stopping before repair continuation.",
                            "elapsed_seconds": max(0, int(time.monotonic() - run_started_at)),
                        },
                        ensure_ascii=False,
                    ),
                }
                yield {
                    "event": "done",
                    "data": json.dumps(
                        ensure_completion_summary(final_text, status="completed"),
                        ensure_ascii=False,
                    ),
                }
                return
            if looks_incomplete(final_text) and repair_passes < MAX_AGENT_REPAIR_PASSES:
                repair_passes += 1
                messages.append(
                    HumanMessage(
                        content=build_tool_use_continuation_instruction(
                            "model pass ended without a usable final answer"
                        )
                    )
                )
                _repair_dangling_tool_call_messages(messages)
                injected_append_commands = inject_pending_append_commands()
                event_stream = agent.astream_events(
                    {"messages": messages},
                    config=config,
                    version="v2",
                ).__aiter__()
                pending_event = None
                active_tool = None
                yield {
                    "event": "debug",
                    "data": json.dumps(
                        {
                            "stage": "agent_tool_use_continuation",
                            "message": "Model ended without a final answer; continuing so the agent can decide whether more tool_use is needed.",
                            "elapsed_seconds": max(0, int(time.monotonic() - run_started_at)),
                            "repair_pass": repair_passes,
                        },
                        ensure_ascii=False,
                    ),
                }
                for append_event in append_injected_debug_events(injected_append_commands):
                    yield append_event
                continue
            recent_tool_error = bool(tool_errors)
            audit = audit_completion_with_policy(final_text)
            needs_repair = (
                repair_passes < MAX_AGENT_REPAIR_PASSES
                and (
                    recent_tool_error
                    or not bool(audit.get("complete"))
                    or has_unresolved_background_subagents()
                )
            )
            if needs_repair:
                repair_passes += 1
                reasons = []
                if recent_tool_error:
                    reasons.append("one or more tool calls returned errors")
                if not bool(audit.get("complete")):
                    reasons.append(str(audit.get("reason") or "policy audit says task is incomplete"))
                if has_unresolved_background_subagents():
                    reasons.append("background subagents are unresolved")
                repair_instruction = build_retry_instruction("; ".join(reasons) or "completion audit failed")
                messages.append(HumanMessage(content=repair_instruction))
                _repair_dangling_tool_call_messages(messages)
                injected_append_commands = inject_pending_append_commands()
                event_stream = agent.astream_events(
                    {"messages": messages},
                    config=config,
                    version="v2",
                ).__aiter__()
                pending_event = None
                yield {
                    "event": "debug",
                    "data": json.dumps(
                        {
                            "stage": "completion_audit_retry",
                            "message": "Completion audit found unfinished/error state; retrying with debug context.",
                            "elapsed_seconds": max(0, int(time.monotonic() - run_started_at)),
                            "repair_pass": repair_passes,
                            "reasons": reasons,
                            "tool_errors": tool_errors[-5:],
                            "completed_subtasks": completed_step_summary(),
                            "failed_subtasks": failed_step_summary(),
                        },
                        ensure_ascii=False,
                    ),
                }
                for append_event in append_injected_debug_events(injected_append_commands):
                    yield append_event
                tool_errors.clear()
                continue
            yield {
                "event": "debug",
                "data": json.dumps(
                    {
                        "stage": "graph_end",
                        "message": "LangGraph execution finished.",
                        "elapsed_seconds": max(0, int(time.monotonic() - run_started_at)),
                    },
                    ensure_ascii=False,
                ),
            }
            yield {
                "event": "done",
                "data": json.dumps(
                    ensure_completion_summary(
                        final_text,
                        status="completed" if not tool_errors else "failed",
                        failure_reason="; ".join(error.get("tool", "tool") for error in tool_errors) if tool_errors else "",
                    ),
                    ensure_ascii=False,
                ),
            }
            return

    yield {
        "event": "debug",
        "data": json.dumps(
            {
                "stage": "agent_end",
                "message": "Agent run finished.",
                "elapsed_seconds": max(0, int(time.monotonic() - run_started_at)),
            },
            ensure_ascii=False,
        ),
    }
    if not collected_text:
        yield {
            "event": "debug",
            "data": json.dumps(
                {
                    "stage": "agent_end",
                    "message": f"Agent stopped before producing final text. Max configured steps: {MAX_AGENT_STEPS}.",
                    "elapsed_seconds": max(0, int(time.monotonic() - run_started_at)),
                    "max_steps": MAX_AGENT_STEPS,
                },
                ensure_ascii=False,
            ),
        }
    final_text = collected_text
    if not should_use_collected_text_as_final(final_text) and last_web_search_payload:
        final_text = build_search_fallback(last_web_search_payload)
    yield {
        "event": "done",
        "data": json.dumps(
            ensure_completion_summary(
                final_text,
                status="failed" if not final_text.strip() else "completed",
                failure_reason="agent stopped before producing final text" if not final_text.strip() else "",
            ),
            ensure_ascii=False,
        ),
    }


async def simple_chat(
    agent: Any,
    message: str,
    session_id: str = "default",
    history: list[dict[str, str]] | None = None,
) -> str:
    """Non-streaming convenience wrapper - returns the final answer text."""
    async for event in stream_agent_events(agent, message, session_id, history):
        if event["event"] == "done":
            data = event["data"]
            try:
                return json.loads(data)
            except Exception:
                return str(data)
    return ""
