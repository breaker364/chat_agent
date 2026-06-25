from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from uuid import uuid4

import uvicorn
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sse_starlette.sse import EventSourceResponse

from .agent import build_agent, stream_agent_events
from .session_store import SessionStore

app = FastAPI(title="Chat Agent", docs_url="/docs")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------------------------------------------------------------------------
# Agent singleton (lazy init on first request)
# ---------------------------------------------------------------------------
_agent: Any = None
_agent_lock: Any = None
_session_store: SessionStore | None = None


def _get_lock():
    global _agent_lock
    if _agent_lock is None:
        import asyncio

        _agent_lock = asyncio.Lock()
    return _agent_lock


def get_session_store() -> SessionStore:
    global _session_store
    if _session_store is None:
        _session_store = SessionStore(Path.cwd().resolve())
    return _session_store


async def get_agent() -> Any:
    global _agent
    if _agent is not None:
        return _agent
    lock = _get_lock()
    async with lock:
        if _agent is not None:
            return _agent
        workspace = Path.cwd().resolve()
        _agent = await build_agent(
            config_path=None,
            workspace_dir=workspace,
        )
        print(f"[agent] Initialised with workspace: {workspace}")
        return _agent


def _merge_history(
    stored_history: list[dict[str, str]],
    incoming_history: list[dict[str, str]] | None,
) -> list[dict[str, str]]:
    if not incoming_history:
        return stored_history
    merged = list(stored_history)
    if incoming_history == stored_history:
        return merged
    if incoming_history[: len(stored_history)] == stored_history:
        return incoming_history
    return incoming_history


def _session_payload(session: dict[str, Any]) -> dict[str, Any]:
    return {
        "session_id": session.get("session_id"),
        "title": session.get("title"),
        "created_at": session.get("created_at"),
        "updated_at": session.get("updated_at"),
        "messages": session.get("messages", []),
        "task_progress": session.get("task_progress", {}),
    }


# ---------------------------------------------------------------------------
# REST endpoints
# ---------------------------------------------------------------------------


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/sessions")
async def list_sessions() -> JSONResponse:
    return JSONResponse({"sessions": get_session_store().list_sessions()})


@app.get("/sessions/{session_id}")
async def get_session(session_id: str) -> JSONResponse:
    session = get_session_store().load_session(session_id)
    if session is None:
        return JSONResponse({"error": "Session not found"}, status_code=404)
    return JSONResponse(_session_payload(session))


@app.post("/sessions")
async def create_session(request: Request) -> JSONResponse:
    body = await request.json() if request.headers.get("content-type", "").startswith("application/json") else {}
    session_id = str(body.get("session_id") or "").strip() or f"session-{uuid4().hex[:12]}"
    title = str(body.get("title") or "").strip()
    store = get_session_store()
    session = store.create_or_get_session(session_id)
    if title:
        session["title"] = title
        store.save_session(session)
    return JSONResponse(_session_payload(session))


@app.post("/chat/stream")
async def chat_stream(request: Request) -> EventSourceResponse:
    body = await request.json()
    message = body.get("message", "")
    session_id = str(body.get("session_id", "default")).strip() or "default"
    history = body.get("history", None)

    if not message.strip():
        return EventSourceResponse([{"event": "error", "data": "Message cannot be empty"}])

    agent = await get_agent()
    store = get_session_store()
    stored_history = store.get_history(session_id)
    merged_history = _merge_history(stored_history, history)

    session = store.create_or_get_session(session_id, first_message=message)
    store.append_message(session["session_id"], "user", message, tools=[])
    store.update_progress(
        session["session_id"],
        status="running",
        active_tool=None,
        message="Agent run started.",
        elapsed_seconds=0,
        last_debug_stage="agent_start",
    )

    async def event_generator():
        assistant_text = ""
        assistant_tools: list[dict[str, Any]] = []
        async for event in stream_agent_events(agent, message, session["session_id"], merged_history):
            event_type = event.get("event", "")
            raw_data = event.get("data", "")
            parsed: Any
            try:
                parsed = json.loads(raw_data)
            except Exception:
                parsed = raw_data

            if event_type == "text":
                assistant_text += str(parsed)
            elif event_type == "tool_call":
                assistant_tools.append(
                    {
                        "type": "tool_call",
                        "name": parsed.get("name", "tool"),
                        "arguments": parsed.get("arguments"),
                    }
                )
                store.update_progress(
                    session["session_id"],
                    status="running",
                    active_tool=parsed.get("name"),
                    message=f"Calling tool `{parsed.get('name', 'tool')}`.",
                    last_debug_stage="tool_start",
                )
            elif event_type == "tool_result":
                assistant_tools.append(
                    {
                        "type": "tool_result",
                        "name": parsed.get("name", "tool"),
                        "content": parsed.get("content"),
                    }
                )
                store.update_progress(
                    session["session_id"],
                    status="running",
                    active_tool=None,
                    message=f"Tool `{parsed.get('name', 'tool')}` returned.",
                    last_debug_stage="tool_end",
                )
            elif event_type == "progress":
                store.update_progress(
                    session["session_id"],
                    status="running",
                    active_tool=parsed.get("active_tool"),
                    message=parsed.get("message", ""),
                    elapsed_seconds=parsed.get("elapsed_seconds", 0),
                    last_debug_stage="heartbeat",
                )
            elif event_type == "debug":
                store.update_progress(
                    session["session_id"],
                    status="running",
                    active_tool=parsed.get("active_tool"),
                    message=parsed.get("message", ""),
                    elapsed_seconds=parsed.get("elapsed_seconds", 0),
                    last_debug_stage=parsed.get("stage", ""),
                )
            elif event_type == "done":
                assistant_text = str(parsed or assistant_text)
                store.replace_last_assistant_message(session["session_id"], assistant_text, tools=assistant_tools)
                store.update_progress(
                    session["session_id"],
                    status="idle",
                    active_tool=None,
                    message="Ready to continue.",
                    elapsed_seconds=0,
                    last_debug_stage="done",
                )

            if await request.is_disconnected():
                break
            yield event

    return EventSourceResponse(event_generator())


@app.post("/chat")
async def chat_sync(request: Request) -> JSONResponse:
    body = await request.json()
    message = body.get("message", "")
    session_id = str(body.get("session_id", "default")).strip() or "default"
    history = body.get("history", None)

    if not message.strip():
        return JSONResponse({"error": "Message cannot be empty"}, status_code=400)

    store = get_session_store()
    stored_history = store.get_history(session_id)
    merged_history = _merge_history(stored_history, history)
    session = store.create_or_get_session(session_id, first_message=message)
    store.append_message(session["session_id"], "user", message, tools=[])
    store.update_progress(
        session["session_id"],
        status="running",
        active_tool=None,
        message="Agent run started.",
        elapsed_seconds=0,
        last_debug_stage="agent_start",
    )

    agent = await get_agent()
    final_text = ""
    tools: list[dict[str, Any]] = []
    async for event in stream_agent_events(agent, message, session["session_id"], merged_history):
        event_type = event["event"]
        try:
            parsed = json.loads(event["data"])
        except Exception:
            parsed = event["data"]
        if event_type == "text":
            final_text += str(parsed)
        elif event_type == "tool_call":
            tools.append({"type": "tool_call", "name": parsed.get("name"), "arguments": parsed.get("arguments")})
        elif event_type == "tool_result":
            tools.append({"type": "tool_result", "name": parsed.get("name"), "content": parsed.get("content")})
        elif event_type == "done":
            final_text = str(parsed or final_text)

    store.replace_last_assistant_message(session["session_id"], final_text, tools=tools)
    store.update_progress(
        session["session_id"],
        status="idle",
        active_tool=None,
        message="Ready to continue.",
        elapsed_seconds=0,
        last_debug_stage="done",
    )
    return JSONResponse({"reply": final_text, "session_id": session["session_id"]})


if __name__ == "__main__":
    uvicorn.run("backend.main:app", host="0.0.0.0", port=8000, reload=True)
