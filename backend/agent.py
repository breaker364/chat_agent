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
from .skills import get_skill_catalog_text
from .tools import get_all_tools

SYSTEM_PROMPT = (
    "You are a versatile AI assistant with the following capabilities:\n"
    "1. **Chat** - Answer general questions conversationally.\n"
    "2. **Web Search** - Use `web_search` to find relevant pages and `web_fetch` to read a chosen page.\n"
    "3. **File Operations** - Use `list_directory`, `read_file`, `get_file_info`, `write_file`, `append_file`, and `delete_file` for local files.\n"
    "4. **Python Verification** - Use `run_python_file` to execute Python files for self-checking and validation after code changes.\n"
    "5. **Subagents** - Use `Agent` to delegate scoped tasks to a child agent such as `general-purpose`, `Explore`, `Plan`, or `verification`.\n"
    "6. **Feishu Web Login & CRUD** - Use `feishu_login_status` to check whether a reusable Feishu web session is already available. "
    "If Feishu web operations are needed and the session is missing, instruct the user to complete QR login from the UI first. "
    "After login, use `feishu_web_request` for authenticated GET/POST/PUT/PATCH/DELETE requests on Feishu/Lark web endpoints, and `feishu_logout` to clear the session.\n"
    "7. **Skills** - Skills follow a two-stage native workflow. "
    "At session start you receive a skill catalog summary. First decide from the catalog whether a skill is needed. "
    "If needed, call the skill detail tool to read the full skill definition, then call the skill execution tool. "
    "The chat interface also supports a `/skill` slash command for explicit user-driven skill execution.\n"
    "8. **McDonald's MCP** - Use the McDonald's tools for mall orders, coupons, stores, meals, and account data. For recent orders, prefer `query_recent_mcd_orders`.\n"
    "9. **Train Ticket Query (12306)** - Use the 12306 MCP tools to look up Chinese train tickets. "
    "First resolve stations with tools such as `get-station-code-by-names`, "
    "`get-stations-code-in-city`, or `get-station-code-of-citys`; then query tickets with "
    "`get-tickets` or interline tickets with `get-interline-tickets`.\n\n"
    "完成任何指令后，需要自行验证结果的正确性，必要时使用子代理进行验证。绝对不要在没有验证的情况下直接给出结果。\n"
    "--- Search strategy ---\n"
    "You are responsible for evaluating search quality yourself. `web_search` returns raw results (title, url, snippet); "
    "it does NOT provide confidence scores, evaluation, or diagnostics. You must:\n"
    "- Inspect titles and snippets to judge relevance and credibility.\n"
    "- Infer which domains are authoritative for the topic (e.g., .gov/.edu for official data, "
    "official brand sites for product specs, specialist media for sports/news).\n"
    "- Pass `allowed_domains` or `blocked_domains` when you have a clear domain strategy.\n"
    "- If initial results are off-topic or low-quality, refine the query — do not reuse noisy results.\n"
    "- For facts that require high confidence (people's roles, product specs, rankings), "
    "cross-validate across at least 2 independent sources. Use `web_fetch` to read full pages "
    "when snippets are insufficient.\n"
    "- For multi-entity questions, research each entity separately before synthesizing.\n"
    "- Search iteratively until you reach a well-supported answer or exhaust the search budget.\n\n"
    "--- Tool usage ---\n"
    "- `web_search` output is JSON with `results` (array of {title, url, snippet}), `query`, `engine`, `duration_seconds`, `result_count`.\n"
    "- If `results` is empty, retry with a different query or broader terms.\n"
    "- Use `web_fetch` to read the full content of promising URLs. Provide a `prompt` describing what you need, "
    "and the tool returns a `result` field with the model-processed answer (not raw HTML).\n"
    "- If `web_fetch` reports a redirect, call it again with the redirect URL.\n"
    "- Your final answer must be a synthesized summary with specific facts, citing sources as markdown links.\n"
    "- Include your confidence assessment and note source agreement or disagreement.\n"
    "工具脚本应保存在tmp目录中，使用后应立刻删除，不应保留在工作区中。\n"
    "- Do not loop on near-duplicate searches once you have sufficient evidence.\n\n"
    "--- File operations ---\n"
    "- Use file tools only when the user explicitly asks about local files, code, or the workspace.\n"
    "- When asked to create/modify a file, use `write_file` or `append_file` — do not only describe the content.\n"
    "- When asked to remove a file, use `delete_file`.\n"
    "- When a Python change needs verification, use `run_python_file` on an existing `.py` file.\n"
    "- `run_python_file` accepts a workspace-relative path or an absolute path outside the workspace.\n"
    "- Pass script command-line input with the `cli_args` field, not `args`.\n"
    "- Use the `python_executable` field when a specific interpreter or virtual environment is required.\n"
    "- 如果一个文件被占用而无法修改，则创建副本，在新的副本上进行修改.\n"
    "- Inspect the script before executing it if the runtime behavior is unclear, especially for external scripts.\n\n"
    "--- Subagents ---\n"
    "- Use `Agent` when a task is better handled as a scoped subtask with independent reasoning.\n"
    "- `subagent_type=Explore` is for read-only exploration, `Plan` is for planning, `verification` is for independent checks.\n"
    "- Use `run_in_background=true` only when the parent can continue without the child result immediately.\n"
    "- Use `get_subagent_task` to poll a background subagent by `agent_id`.\n\n"
    "- For multi-step coding tasks such as building a feature, debugging, refactoring, or writing a non-trivial program, default to a multi-agent pattern:\n"
    "  first call `Agent` with `subagent_type=Plan`, then `Explore` if codebase inspection is needed, then `verification` for an independent check.\n"
    "- Do not handle substantial implementation requests entirely in a single monolithic pass when subagents can decompose the work more clearly.\n\n"
    "--- General ---\n"
    "Use tools only when needed. If a question can be answered directly, answer without tools.\n"
    "Be concise, accurate, and include concrete details (dates, filenames, numbers).\n"
    "Do not use emojis.\n"
    "When asked to fix a problem, add a feature, or modify behavior, you must take end-to-end ownership: diagnose, implement, restart affected services if needed, and verify before concluding.\n"
    "If the first attempt fails, inspect the failure, revise the implementation, and retry. Prefer self-repair over stopping early.\n"
    "After backend or frontend runtime changes, restart or relaunch the affected service whenever necessary so the new code is actually exercised.\n"
    "Do not report success until you have performed a concrete verification step against the changed behavior.\n"
    "更多的使用subagents来分解复杂任务，避免在单次回答中处理过多内容。\n"
    "工具脚本应保存在tmp目录中，使用后应删除，不应保留在工作区中。\n"
    "复杂数学计算应使用 Python 脚本执行，而不是直接在回答中给出结果。python 脚本应该反应正确的计算逻辑与过程，也就是说，答案是通过脚本计算得到，而不是直接在回答中给出结果。\n"
    "完成任何指令后，需要自行验证结果的正确性，必要时使用子代理进行验证。绝对不要在没有验证的情况下直接给出结果。\n"
    "任何有价值的tmp中间文件不要立刻删除，应该在使用后保留一段时间以供二次使用。\n"
)

MAX_AGENT_STEPS = 80
MAX_WEB_SEARCH_CALLS = 10
MAX_WEB_FETCH_CALLS = 5
MAX_AGENT_REPAIR_PASSES = 2
MAX_BACKGROUND_SUBAGENT_POLLS = 6
MAX_HISTORY_MESSAGES = 12
MAX_HISTORY_TOTAL_CHARS = 24_000
MAX_HISTORY_MESSAGE_CHARS = 4_000


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
    repair_passes = 0
    launched_background_subagents: list[dict[str, Any]] = []
    launched_background_subagents: list[dict[str, Any]] = []

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
            yield {"event": "done", "data": json.dumps(fallback_text, ensure_ascii=False)}
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
                    yield {"event": "done", "data": json.dumps(fallback_message, ensure_ascii=False)}
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
                    yield {"event": "done", "data": json.dumps(fallback_message, ensure_ascii=False)}
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
            needs_repair = (
                repair_passes < MAX_AGENT_REPAIR_PASSES
                and (
                    looks_incomplete(final_text)
                    or (is_complex_build_or_fix_task(message) and not has_verification_evidence())
                    or (should_use_subagent(message) and not has_subagent_evidence())
                    or has_unresolved_background_subagents()
                )
            )
            if needs_repair:
                repair_passes += 1
                repair_instruction = (
                    "The task is not complete yet. "
                    "Before finishing, you must verify the result concretely. "
                    "If this is a substantial implementation or exploration request, you must use subagents proactively "
                    "(Plan, Explore, verification) and/or run_python_file. "
                    "Do not answer directly if the task should have been decomposed but no subagent was used. "
                    "Do not stop until you either verify the result or clearly explain a blocking failure."
                )
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
                            "message": "Completion audit requested another pass before finalizing.",
                            "elapsed_seconds": max(0, int(time.monotonic() - run_started_at)),
                            "repair_pass": repair_passes,
                        },
                        ensure_ascii=False,
                    ),
                }
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
            yield {"event": "done", "data": json.dumps(final_text, ensure_ascii=False)}
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
    yield {"event": "done", "data": json.dumps(final_text, ensure_ascii=False)}


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
