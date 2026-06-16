from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any, AsyncGenerator

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage
from langgraph.checkpoint.memory import MemorySaver
from langgraph.prebuilt import create_react_agent

from .config import create_chat_deepseek, load_llm_config
from .tools import get_all_tools

SYSTEM_PROMPT = (
    "You are a versatile AI assistant with the following capabilities:\n"
    "1. **Chat** - Answer general questions conversationally.\n"
    "2. **Web Search** - Use `web_search` to find relevant pages and `fetch_webpage` to read a chosen page.\n"
    "3. **File Operations** - Use `list_directory`, `read_file`, `get_file_info` to inspect files.\n"
    "4. **Train Ticket Query (12306)** - Use the 12306 MCP tools to look up Chinese train tickets. "
    "First resolve stations with tools such as `get-station-code-by-names`, "
    "`get-stations-code-in-city`, or `get-station-code-of-citys`; then query tickets with "
    "`get-tickets` or interline tickets with `get-interline-tickets`.\n\n"
    "Search quality rules:\n"
    "- Keep search queries tightly aligned with the user's topic; do not switch to adjacent topics.\n"
    "- For sports/news/facts, prefer short literal queries using the user's original language first.\n"
    "- Search at most two times for one topic. If results are still off-topic, say so explicitly instead of looping.\n"
    "- If search results look off-topic, explicitly retry with a narrower query instead of using them.\n"
    "- Ignore results whose title/snippet obviously do not match the user's subject.\n"
    "- Do not cite or summarize irrelevant snippets just because the tool returned them.\n\n"
    "Use tools only when needed. If a question can be answered directly, answer without tools.\n"
    "Be concise, accurate, and include concrete details (dates, filenames, numbers)."
)

MAX_AGENT_STEPS = 25
MAX_WEB_SEARCH_CALLS = 1
MAX_FETCH_WEBPAGE_CALLS = 3


async def build_agent(
    config_path: str | Path | None = None,
    workspace_dir: str | Path | None = None,
) -> Any:
    """Build and return a compiled LangGraph react agent."""
    cfg = load_llm_config(config_path)
    llm = create_chat_deepseek(cfg)
    tools = await get_all_tools(workspace_dir=workspace_dir)
    memory = MemorySaver()
    agent = create_react_agent(
        model=llm,
        tools=tools,
        state_schema=None,
        checkpointer=memory,
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

    config = {
        "configurable": {"thread_id": session_id},
        "recursion_limit": MAX_AGENT_STEPS,
    }
    collected_text = ""
    active_tool: str | None = None
    progress_count = 0
    run_started_at = time.monotonic()
    web_search_calls = 0
    fetch_webpage_calls = 0

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
                    yield {"event": "done", "data": json.dumps(collected_text, ensure_ascii=False)}
                    return
            elif name == "fetch_webpage":
                fetch_webpage_calls += 1
                if fetch_webpage_calls > MAX_FETCH_WEBPAGE_CALLS:
                    yield {
                        "event": "debug",
                        "data": json.dumps(
                            {
                                "stage": "fetch_budget_exhausted",
                                "message": f"Blocked repeated fetch_webpage call. Max allowed per request: {MAX_FETCH_WEBPAGE_CALLS}.",
                                "elapsed_seconds": max(0, int(time.monotonic() - run_started_at)),
                                "tool": name,
                                "arguments": input_data,
                            },
                            ensure_ascii=False,
                        ),
                    }
                    yield {"event": "done", "data": json.dumps(collected_text, ensure_ascii=False)}
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
            yield {"event": "done", "data": json.dumps(collected_text, ensure_ascii=False)}
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
    yield {"event": "done", "data": json.dumps(collected_text, ensure_ascii=False)}


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
