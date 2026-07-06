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
from .prompts import load_system_prompt
from .skills import get_skill_catalog_text
from .tools import get_all_tools

SYSTEM_PROMPT = load_system_prompt()


MAX_AGENT_STEPS = 80
MAX_WEB_SEARCH_CALLS = 10
MAX_WEB_FETCH_CALLS = 5
MAX_AGENT_REPAIR_PASSES = 4
MAX_BACKGROUND_SUBAGENT_POLLS = 6
MAX_HISTORY_MESSAGES = 12
MAX_HISTORY_TOTAL_CHARS = 24_000
MAX_HISTORY_MESSAGE_CHARS = 4_000
HEARTBEAT_EMIT_INTERVAL_SECONDS = 3


def estimate_tokens_from_text(text: str) -> int:
    normalized = text or ""
    if not normalized:
        return 0
    return max(1, (len(normalized) + 3) // 4)


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
    repair_passes = 0
    launched_background_subagents: list[dict[str, Any]] = []
    last_heartbeat_emitted_at = 0.0

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
        normalized = (text or "").strip()
        if not normalized:
            return False
        weak_markers = [
            "让我再查一下",
            "我再查一下",
            "我再搜索一下",
            "让我搜索一下",
            "let me check",
            "let me search",
            "let me look",
            "i'll search",
        ]
        if len(normalized) < 40 and any(marker in normalized.lower() for marker in weak_markers):
            return False
        return True

    def is_complex_build_or_fix_task(user_message: str) -> bool:
        normalized = (user_message or "").lower()
        patterns = [
            "fix",
            "bug",
            "debug",
            "refactor",
            "implement",
            "build",
            "write a",
            "写一个",
            "实现",
            "修复",
            "增加功能",
            "修改功能",
            "重构",
            "程序",
            "应用",
        ]
        return any(pattern in normalized for pattern in patterns)

    def is_simple_direct_task(user_message: str) -> bool:
        normalized = (user_message or "").lower()
        direct_patterns = [
            "read file",
            "open file",
            "find function",
            "find class",
            "list files",
            "read ",
            "grep ",
            "读取文件",
            "打开文件",
            "查找函数",
            "查找类",
            "列出文件",
        ]
        blocking_patterns = [
            "fix",
            "implement",
            "build",
            "refactor",
            "write a",
            "修复",
            "实现",
            "写一个",
            "重构",
        ]
        return any(pattern in normalized for pattern in direct_patterns) and not any(
            pattern in normalized for pattern in blocking_patterns
        )

    def should_use_subagent(user_message: str) -> bool:
        if is_simple_direct_task(user_message):
            return False
        normalized = (user_message or "").lower()
        broad_patterns = [
            "analyze",
            "architecture",
            "design",
            "plan",
            "search the codebase",
            "inspect the project",
            "大范围",
            "架构",
            "设计",
            "规划",
            "分析",
        ]
        return is_complex_build_or_fix_task(user_message) or any(pattern in normalized for pattern in broad_patterns)

    def build_routing_guidance(user_message: str) -> str:
        lines: list[str] = []
        if is_simple_direct_task(user_message):
            lines.append(
                "This looks like a simple directed task. Prefer direct tools such as Read, Grep, Glob, Bash, or a single focused tool call instead of spawning subagents."
            )
        if should_use_subagent(user_message):
            lines.append(
                "This task should be decomposed. Prefer a layered route: Plan first, Explore if broader inspection is needed, and verification before final completion."
            )
        if is_complex_build_or_fix_task(user_message):
            lines.append(
                "This is a non-trivial implementation or repair request. Verification is required before completion."
            )
        return "\n".join(lines)

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
        lower = text.lower()
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
        benign_markers = {"not detected", "no debug events yet"}
        if any(marker in lower for marker in benign_markers):
            return False
        error_markers = [
            "traceback", "exception", "runtimeerror", "syntaxerror",
            "permissionerror", "filenotfounderror", "not found", "file not found",
            "failed", "error:", "missing required", "invalid ", "no active session",
            "session is missing", "session expired", "404 not found",
            "拒绝访问", "不存在", "失败", "报错",
        ]
        return any(marker in lower for marker in error_markers)

    def requires_completion_verification(user_message: str) -> bool:
        normalized = (user_message or "").lower()
        patterns = [
            "download", "save", "write", "edit", "modify", "create", "delete",
            "fix", "implement", "run", "verify", "下载", "保存", "写入",
            "修改", "更改", "创建", "新建", "删除", "修复", "实现", "验证",
            "图片", "表格", "文件", "tmp",
        ]
        return any(pattern in normalized for pattern in patterns)

    def has_completion_verification_evidence() -> bool:
        lower_names = {name.lower() for name in tool_call_names}
        verification_tools = {
            "run_python_file", "get_file_info", "list_directory", "read_file",
            "feishu_login_status", "get_subagent_task",
        }
        return has_verification_evidence() or bool(lower_names & verification_tools)

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
        return (
            "The task is not complete. Continue from the current state and fix the issue without asking the user to paste the error again.\n"
            f"Reason: {reason}\n\n"
            f"Recent debug/tool errors:\n{errors_text}\n\n"
            "Required next actions:\n"
            "1. Inspect the error and adjust the command, arguments, path, sheet/table/cell selector, or implementation.\n"
            "2. Re-run the corrected tool call or verification step.\n"
            "3. Do not finish until the requested output exists or a concrete blocker is proven.\n"
            "4. If the task created/downloaded/saved/modified anything, verify it with a concrete read/list/stat/API check."
        )

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

    routing_guidance = build_routing_guidance(message)
    messages: list[BaseMessage] = [SystemMessage(content=SYSTEM_PROMPT)]
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
    if routing_guidance:
        messages.append(SystemMessage(content=routing_guidance))
    effective_history = trim_history(history)
    context_message_count = len(effective_history) + 2
    context_char_count = sum(len(msg.get("content", "")) for msg in effective_history)
    context_char_count += len(SYSTEM_PROMPT) + len(message)
    if skill_catalog_text:
        context_char_count += len(skill_catalog_text)
        context_message_count += 1
    if routing_guidance:
        context_char_count += len(routing_guidance)
        context_message_count += 1
    context_token_estimate = estimate_tokens_from_text("".join(
        [SYSTEM_PROMPT, skill_catalog_text or "", routing_guidance or "", message]
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

    messages.append(HumanMessage(content=message))

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
                token = chunk.content if isinstance(chunk.content, str) else ""
                if token:
                    collected_text += token
                    yield {"event": "text", "data": json.dumps(token, ensure_ascii=False)}

        elif kind == "on_tool_start":
            input_data = sanitize_tool_payload(data.get("input", {}))
            tool_call_names.append(name)
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
                        "stage": "tool_start",
                        "message": f"Calling tool `{name}`.",
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
            if output_indicates_tool_error(tool_name, output_str):
                tool_errors.append({"tool": tool_name, "message": output_str[:4000]})
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
                        "stage": "tool_end",
                        "message": f"Tool `{tool_name}` returned.",
                        "elapsed_seconds": elapsed_seconds,
                        "tool": tool_name,
                        "content_preview": output_str[:300],
                    },
                    ensure_ascii=False,
                ),
            }

        elif kind == "on_chat_model_start":
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
            verification_required = requires_completion_verification(message)
            recent_tool_error = bool(tool_errors)
            needs_repair = (
                repair_passes < MAX_AGENT_REPAIR_PASSES
                and (
                    recent_tool_error
                    or looks_incomplete(final_text)
                    or (is_complex_build_or_fix_task(message) and not has_verification_evidence())
                    or (verification_required and not has_completion_verification_evidence())
                    or (should_use_subagent(message) and not has_subagent_evidence())
                    or has_unresolved_background_subagents()
                )
            )
            if needs_repair:
                repair_passes += 1
                reasons = []
                if recent_tool_error:
                    reasons.append("one or more tool calls returned errors")
                if looks_incomplete(final_text):
                    reasons.append("final text looks incomplete")
                if is_complex_build_or_fix_task(message) and not has_verification_evidence():
                    reasons.append("implementation/repair task lacks verification")
                if verification_required and not has_completion_verification_evidence():
                    reasons.append("requested save/download/modify task lacks concrete verification")
                if should_use_subagent(message) and not has_subagent_evidence():
                    reasons.append("complex task lacks subagent use")
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
