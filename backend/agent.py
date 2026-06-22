from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any, AsyncGenerator

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage
from langgraph.prebuilt import create_react_agent

from .config import create_chat_deepseek, load_llm_config
from .tools import get_all_tools

SYSTEM_PROMPT = (
    "You are a versatile AI assistant with the following capabilities:\n"
    "1. **Chat** - Answer general questions conversationally.\n"
    "2. **Web Search** - Use `web_search` to find relevant pages and `web_fetch` to read a chosen page.\n"
    "3. **File Operations** - Use `list_directory`, `read_file`, `get_file_info`, `write_file`, `append_file`, and `delete_file` for local files.\n"
    "4. **Train Ticket Query (12306)** - Use the 12306 MCP tools to look up Chinese train tickets. "
    "First resolve stations with tools such as `get-station-code-by-names`, "
    "`get-stations-code-in-city`, or `get-station-code-of-citys`; then query tickets with "
    "`get-tickets` or interline tickets with `get-interline-tickets`.\n\n"
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
    "- Do not loop on near-duplicate searches once you have sufficient evidence.\n\n"
    "--- File operations ---\n"
    "- Use file tools only when the user explicitly asks about local files, code, or the workspace.\n"
    "- When asked to create/modify a file, use `write_file` or `append_file` — do not only describe the content.\n"
    "- When asked to remove a file, use `delete_file`.\n\n"
    "--- General ---\n"
    "Use tools only when needed. If a question can be answered directly, answer without tools.\n"
    "Be concise, accurate, and include concrete details (dates, filenames, numbers).\n"
    "Do not use emojis."
)

MAX_AGENT_STEPS = 25
MAX_WEB_SEARCH_CALLS = 6
MAX_WEB_FETCH_CALLS = 3


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
    messages: list[BaseMessage] = [
        SystemMessage(content=SYSTEM_PROMPT),
    ]
    if history:
        for msg in history:
            role = msg.get("role", "")
            content = msg.get("content", "")
            if role == "user":
                messages.append(HumanMessage(content=content))
            elif role == "assistant":
                messages.append(AIMessage(content=content))

    messages.append(HumanMessage(content=message))

    config = {"recursion_limit": MAX_AGENT_STEPS}
    collected_text = ""
    active_tool: str | None = None
    progress_count = 0
    run_started_at = time.monotonic()
    web_search_calls = 0
    web_fetch_calls = 0
    last_web_search_payload: dict[str, Any] | None = None
    last_web_fetch_results: list[str] = []

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
            raise

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
            final_text = collected_text
            if not should_use_collected_text_as_final(final_text) and last_web_search_payload:
                final_text = build_search_fallback(last_web_search_payload)
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
