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
    "Search quality rules:\n"
    "- Keep search queries tightly aligned with the user's topic; do not switch to adjacent topics.\n"
    "- For compound questions involving multiple people, companies, or comparisons, decompose the task and research each entity separately.\n"
    "- For sports/news/facts, prefer short literal queries using the user's original language first.\n"
    "- Before searching, reason about which websites are authoritative for this specific topic, then prefer those domains.\n"
    "- When a domain-focused search is appropriate, pass `allowed_domains` to `web_search` based on your own reasoning.\n"
    "- Domain choice must be inferred from the entity and topic, not from a fixed hardcoded list.\n"
    "- Prefer official websites first when the user asks for rankings, standings, specifications, schedules, regulations, prices, or structured facts.\n"
    "- If official websites are weak or incomplete, expand to reputable specialist media or trusted community sources for that field.\n"
    "- If the first domain choice is weak or irrelevant, switch domains instead of repeating the same noisy search pattern.\n"
    "- Search iteratively until you reach a high-confidence answer or exhaust the search budget.\n"
    "- Prefer 2-4 searches for difficult factual questions when the first result set lacks cross-validation.\n"
    "- If search results look off-topic, explicitly retry with a narrower query instead of using them.\n"
    "- Ignore results whose title/snippet obviously do not match the user's subject.\n"
    "- Do not cite or summarize irrelevant snippets just because the tool returned them.\n\n"
    "Tool usage rules:\n"
    "- Treat `web_search` output as structured JSON. Read `confidence`, `insufficiencies`, `recommended_next_steps`, `summary`, `results`, `candidate_results`, and `need_webfetch` before deciding the next step.\n"
    "- For multi-entity questions, do not stop after researching only one entity; cover every requested entity before answering.\n"
    "- For people/company identity facts, do not answer decisively until at least two independent sources support the key fact, or one official source plus one independent source.\n"
    "- If `web_search.results` is empty but `candidate_results` is non-empty, fetch the best candidate pages instead of saying nothing was found.\n"
    "- If `web_search.confidence.level` is low or `insufficient_cross_validation` is present, run another narrower search instead of answering.\n"
    "- If `web_search` returns relevant links but evidence is snippet-only, call `web_fetch` on the best 1-3 URLs and compare the facts.\n"
    "- Your final answer must be a synthesized summary, not a link dump. Include confidence level and mention source agreement or disagreement.\n"
    "- Do not loop on near-duplicate searches once confidence is already high.\n"
    "- When the user asks to create or modify a local file, use `write_file` or `append_file` instead of only describing the content.\n"
    "- When the user asks to remove a local file, use `delete_file`.\n"
    "- Do not use file tools for general web knowledge questions.\n"
    "- Do not use emojis, just answer technically.\n"
    "- Use file tools only when the user explicitly asks about local files, code, or the current workspace.\n\n"
    "Use tools only when needed. If a question can be answered directly, answer without tools.\n"
    "Be concise, accurate, and include concrete details (dates, filenames, numbers)."
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
        if not payload:
            return "I could not find enough reliable search results. Please narrow the topic or add more specific keywords."
        summary = str(payload.get("summary") or "").strip()
        confidence = payload.get("confidence") or {}
        confidence_level = str(confidence.get("level") or "").strip()
        confidence_reason = str(confidence.get("reason") or "").strip()
        insufficiencies = payload.get("insufficiencies") or []
        results = payload.get("results") or []
        candidate_results = payload.get("candidate_results") or []
        if not results and not candidate_results:
            note = payload.get("note")
            return str(note or summary or "I could not find enough reliable search results. Please narrow the topic or add more specific keywords.")
        lines: list[str] = []
        if summary:
            lines.append(summary)
        else:
            lines.append("I found relevant results and stopped repeated searching.")
        if confidence_level:
            detail = f"Confidence: {confidence_level}"
            if confidence_reason:
                detail += f" ({confidence_reason})"
            lines.append(detail)
        if insufficiencies:
            lines.append("Gaps: " + ", ".join(str(item) for item in insufficiencies[:3]))
        lines.append("Sources:")
        source_items = results[:3] if results else candidate_results[:3]
        for item in source_items:
            title = item.get("title", "Untitled")
            url = item.get("url", "")
            lines.append(f"- [{title}]({url})")
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
