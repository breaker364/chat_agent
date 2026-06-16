from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import uvicorn
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sse_starlette.sse import EventSourceResponse

from .agent import build_agent, stream_agent_events

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


def _get_lock():
    global _agent_lock
    if _agent_lock is None:
        import asyncio
        _agent_lock = asyncio.Lock()
    return _agent_lock


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
            config_path=None,  # uses DEFAULT_CONFIG_PATH
            workspace_dir=workspace,
        )
        print(f"[agent] Initialised with workspace: {workspace}")
        return _agent


# ---------------------------------------------------------------------------
# REST endpoints
# ---------------------------------------------------------------------------

@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/chat/stream")
async def chat_stream(request: Request) -> EventSourceResponse:
    """SSE streaming chat endpoint.

    Expects JSON body::

        {
          "message": "user's input text",
          "session_id": "optional-session-id",
          "history": [{"role": "user"/"assistant", "content": "..."}]
        }
    """
    body = await request.json()
    message = body.get("message", "")
    session_id = body.get("session_id", "default")
    history = body.get("history", None)

    if not message.strip():
        return EventSourceResponse(
            [{"event": "error", "data": "Message cannot be empty"}]
        )

    agent = await get_agent()

    async def event_generator():
        async for event in stream_agent_events(agent, message, session_id, history):
            if await request.is_disconnected():
                break
            yield event

    return EventSourceResponse(event_generator())


@app.post("/chat")
async def chat_sync(request: Request) -> JSONResponse:
    """Non-streaming chat endpoint (returns complete answer at once)."""
    body = await request.json()
    message = body.get("message", "")
    session_id = body.get("session_id", "default")
    history = body.get("history", None)

    if not message.strip():
        return JSONResponse({"error": "Message cannot be empty"}, status_code=400)

    agent = await get_agent()
    final_text = ""
    async for event in stream_agent_events(agent, message, session_id, history):
        if event["event"] == "done":
            final_text = event["data"]

    return JSONResponse({"reply": final_text})


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    uvicorn.run("backend.main:app", host="0.0.0.0", port=8000, reload=True)
