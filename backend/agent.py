from __future__ import annotations

import asyncio
import json
import re
import time
from pathlib import Path
from typing import Any, AsyncGenerator

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage
from langgraph.prebuilt import create_react_agent

from .config import create_chat_deepseek, load_llm_config
from .prompts import load_agent_policy, load_system_prompt
from .session_store import SessionStore
from .skills import get_skill_catalog_text
from .token_counter import count_text_tokens
from .tools import get_all_tools, _normalize_tool_payload_for_key
from .vision import VisionConfigurationError, analyze_image_files, extract_image_paths

SYSTEM_PROMPT = load_system_prompt()
AGENT_POLICY = load_agent_policy()


MAX_AGENT_STEPS = 80
MAX_WEB_SEARCH_CALLS = 10
MAX_WEB_FETCH_CALLS = 5
MAX_AGENT_REPAIR_PASSES = 2
MAX_BACKGROUND_SUBAGENT_POLLS = 6
MAX_HISTORY_MESSAGES = 12
MAX_HISTORY_TOTAL_CHARS = 24_000
MAX_HISTORY_MESSAGE_CHARS = 4_000
HEARTBEAT_EMIT_INTERVAL_SECONDS = 3


def estimate_tokens_from_text(text: str) -> int:
    return count_text_tokens(text or "")


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
    llm = create_chat_deepseek(cfg)
    tools = await get_all_tools(workspace_dir=workspace_dir)
    agent = create_react_agent(
        model=llm,
        tools=tools,
        state_schema=None,
    )
    agent.name = "chat_agent"
    return agent


async def stream_agent_events(
    agent: Any,
    message: str,
    session_id: str = "default",
    history: list[dict[str, str]] | None = None,
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
        if lowered in {"write_file", "append_file", "delete_file", "run_python_file"}:
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

    def trim_history(history_items: list[dict[str, str]] | None) -> list[dict[str, str]]:
        if not history_items:
            return []
        trimmed: list[dict[str, str]] = []
        total_chars = 0
        # Keep the most recent turns first, then restore chronological order.
        for item in reversed(history_items):
            role = item.get("role", "")
            if role not in {"user", "assistant"}:
                continue
            content = compress_history_text(item.get("content", ""))
            projected = total_chars + len(content)
            if trimmed and (len(trimmed) >= MAX_HISTORY_MESSAGES or projected > MAX_HISTORY_TOTAL_CHARS):
                break
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
    effective_history = trim_history(history)
    effective_message = message
    detected_image_paths = extract_image_paths(message, Path.cwd())
    if detected_image_paths:
        image_tool_name = "analyze_images" if len(detected_image_paths) > 1 else "analyze_image"
        tool_call_names.append(image_tool_name)
        yield {
            "event": "tool_call",
            "data": json.dumps(
                {
                    "name": image_tool_name,
                    "arguments": {
                        "paths": detected_image_paths,
                        "prompt": "Automatically analyze attached images before main-agent reasoning.",
                    },
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
                "event": "tool_result",
                "data": json.dumps(
                    {
                        "name": image_tool_name,
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
                "event": "tool_result",
                "data": json.dumps(
                    {
                        "name": image_tool_name,
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
                "event": "tool_result",
                "data": json.dumps(
                    {
                        "name": image_tool_name,
                        "content": json.dumps(
                            {"success": False, "error": str(exc)},
                            ensure_ascii=False,
                        ),
                    },
                    ensure_ascii=False,
                ),
            }

    context_message_count = len(effective_history) + 2
    context_char_count = sum(len(msg.get("content", "")) for msg in effective_history)
    context_char_count += len(SYSTEM_PROMPT) + len(effective_message)
    if skill_catalog_text:
        context_char_count += len(skill_catalog_text)
        context_message_count += 1
    if resume_text:
        context_char_count += len(resume_text)
        context_message_count += 1
    context_token_estimate = estimate_tokens_from_text("".join(
        [SYSTEM_PROMPT, AGENT_POLICY or "", skill_catalog_text or "", resume_text or "", effective_message]
        + [msg.get("content", "") for msg in effective_history]
    ))

    if effective_history:
        for msg in effective_history:
            role = msg.get("role", "")
            content = msg.get("content", "")
            if role == "user":
                messages.append(HumanMessage(content=content))
            elif role == "assistant":
                messages.append(AIMessage(content=content))

    messages.append(HumanMessage(content=effective_message))

    event_stream = agent.astream_events(
        {"messages": messages},
        config=config,
        version="v2",
    ).__aiter__()

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
            },
            ensure_ascii=False,
        ),
    }

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
                continue
            yield {
                "event": "error",
                "data": json.dumps(
                    {
                        "message": str(exc),
                        "elapsed_seconds": max(0, int(time.monotonic() - run_started_at)),
                    },
                    ensure_ascii=False,
                ),
            }
            fallback_text = collected_text.strip() or f"Agent execution failed: {exc}"
            yield {
                "event": "done",
                "data": json.dumps(
                    ensure_completion_summary(fallback_text, status="failed", failure_reason=str(exc)),
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
            input_data = sanitize_tool_payload(data.get("input", {}))
            display_key = runtime_tool_call_key(name, input_data)
            is_duplicate_display_call = display_key in emitted_tool_call_keys
            tool_result_suppression_queue.setdefault(name, []).append(is_duplicate_display_call)
            if not is_duplicate_display_call:
                emitted_tool_call_keys.add(display_key)
                tool_call_names.append(name)
            tool_call_history.append(
                {"phase": "start", "tool": name, "arguments": input_data, "at": time.monotonic()}
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
                        {"name": name, "arguments": input_data},
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
            if not suppress_result:
                yield {
                    "event": "tool_result",
                    "data": json.dumps(
                        {"name": tool_name, "content": output_str},
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

