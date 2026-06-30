from __future__ import annotations

import json
import os
import asyncio
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
from .session_events import get_session_event_hub
from .subagent_runtime import get_subagent_manager as get_runtime_subagent_manager
from .subagents import built_in_subagents, read_subagent_task_state
from .tools import _CURRENT_SESSION_ID_ENV
from .skills import (
    list_installed_skills,
    get_installed_skill,
    install_skill_from_registry,
    uninstall_skill,
    get_available_skills,
    execute_skill,
)

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


def get_subagent_manager() -> Any:
    return get_runtime_subagent_manager()


def _workspace() -> Path:
    return Path.cwd().resolve()


async def get_agent() -> Any:
    global _agent
    if _agent is not None:
        return _agent
    lock = _get_lock()
    async with lock:
        if _agent is not None:
            return _agent
        workspace = _workspace()
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
        "subagent_tasks": session.get("subagent_tasks", []),
        "subagent_notifications": session.get("subagent_notifications", []),
    }


def _refresh_session_subagent_tasks(session: dict[str, Any]) -> dict[str, Any]:
    workspace = _workspace()
    tasks = []
    for item in session.get("subagent_tasks", []):
        agent_id = item.get("agent_id", "")
        if not agent_id:
            continue
        latest = get_subagent_manager().get_task(agent_id, workspace_dir=workspace)
        tasks.append(latest or item)
    session["subagent_tasks"] = tasks
    get_session_store().save_session(session)
    return session


# ---------------------------------------------------------------------------
# REST endpoints
# ---------------------------------------------------------------------------


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


# ---------------------------------------------------------------------------
# Skill endpoints
# ---------------------------------------------------------------------------


@app.get("/skills")
async def list_skills() -> JSONResponse:
    """List all installed skills."""
    ws = _workspace()
    skills = list_installed_skills(ws)
    return JSONResponse({"skills": [s.to_dict() for s in skills]})


@app.get("/skills/available")
async def list_available_skills() -> JSONResponse:
    """List skills from the built-in registry that are not yet installed."""
    ws = _workspace()
    skills = get_available_skills(ws)
    return JSONResponse({"skills": [s.to_dict() for s in skills]})


@app.get("/skills/{name:str}")
async def get_skill_detail(name: str) -> JSONResponse:
    """Get a single installed skill's detail including params_schema."""
    ws = _workspace()
    skill = get_installed_skill(ws, name)
    if skill is None:
        return JSONResponse({"error": f"Skill '{name}' is not installed."}, status_code=404)
    return JSONResponse({"skill": skill.to_dict()})


@app.post("/skills/install")
async def install_skill_api(request: Request) -> JSONResponse:
    """Install a skill from the built-in registry."""
    body = await request.json()
    name = str(body.get("name", "")).strip()
    if not name:
        return JSONResponse({"error": "Skill name is required."}, status_code=400)
    ws = _workspace()
    skill = install_skill_from_registry(ws, name)
    if skill is None:
        return JSONResponse(
            {"error": f"Skill '{name}' not found in the registry. Available skills: architecture-diagram-generator"},
            status_code=404,
        )
    return JSONResponse({"skill": skill.to_dict(), "message": f"Skill '{name}' installed successfully."})


@app.delete("/skills/{name:str}")
async def uninstall_skill_api(name: str) -> JSONResponse:
    """Uninstall a skill."""
    ws = _workspace()
    deleted = uninstall_skill(ws, name)
    if not deleted:
        return JSONResponse({"error": f"Skill '{name}' is not installed."}, status_code=404)
    return JSONResponse({"message": f"Skill '{name}' uninstalled successfully."})


@app.post("/skills/{name:str}/execute")
async def execute_skill_api(name: str, request: Request) -> JSONResponse:
    """Execute a skill with user-provided parameters.

    Body: { "params": { "system_description": "...", "diagram_type": "architecture" } }
    """
    body = await request.json()
    params = body.get("params", {})
    if not isinstance(params, dict) or not params:
        return JSONResponse({"error": "Parameters object is required (e.g. {\"system_description\": \"...\"})."}, status_code=400)

    ws = _workspace()
    skill = get_installed_skill(ws, name)
    if skill is None:
        return JSONResponse({"error": f"Skill '{name}' is not installed."}, status_code=404)

    # Validate required params
    schema = skill.params_schema or {}
    required = schema.get("required", [])
    missing = [r for r in required if r not in params or not str(params.get(r, "")).strip()]
    if missing:
        return JSONResponse(
            {"error": f"Missing required parameter(s): {', '.join(missing)}"},
            status_code=400,
        )

    try:
        result = await execute_skill(ws, name, params)
        return JSONResponse({"result": result, "skill": name})
    except Exception as exc:
        return JSONResponse({"error": f"Skill execution failed: {exc}", "skill": name}, status_code=500)


# ---------------------------------------------------------------------------
# Session & Subagent endpoints
# ---------------------------------------------------------------------------


@app.get("/sessions")
async def list_sessions() -> JSONResponse:
    return JSONResponse({"sessions": get_session_store().list_sessions()})


@app.get("/subagents")
async def list_subagents() -> JSONResponse:
    subagent_dir = _workspace() / "sessionss" / "subagents"
    items: list[dict[str, Any]] = []
    if subagent_dir.exists():
        for path in sorted(subagent_dir.glob("*.task.json"), key=lambda item: item.stat().st_mtime, reverse=True):
            try:
                items.append(json.loads(path.read_text(encoding="utf-8")))
            except Exception:
                continue
    return JSONResponse({"subagents": items, "types": list(built_in_subagents().keys())})


@app.get("/subagents/{agent_id}")
async def get_subagent(agent_id: str) -> JSONResponse:
    task = get_subagent_manager().get_task(agent_id, workspace_dir=_workspace())
    if task is None:
        task = read_subagent_task_state(_workspace(), agent_id)
    if task is None:
        return JSONResponse({"error": "Subagent not found"}, status_code=404)
    return JSONResponse(task)


@app.post("/subagents/{agent_id}/messages")
async def send_message_to_subagent(agent_id: str, request: Request) -> JSONResponse:
    body = await request.json()
    message = str(body.get("message") or "").strip()
    if not message:
        return JSONResponse({"error": "Message cannot be empty"}, status_code=400)
    ok = get_subagent_manager().send_message(
        agent_id,
        message,
        workspace_dir=_workspace(),
    )
    if not ok:
        return JSONResponse({"error": "Subagent not found"}, status_code=404)
    task = get_subagent_manager().get_task(agent_id, workspace_dir=_workspace())
    return JSONResponse(task or {"ok": True, "agent_id": agent_id})


@app.get("/sessions/{session_id}")
async def get_session(session_id: str) -> JSONResponse:
    session = get_session_store().load_session(session_id)
    if session is None:
        return JSONResponse({"error": "Session not found"}, status_code=404)
    session = _refresh_session_subagent_tasks(session)
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


@app.patch("/sessions/{session_id}")
async def rename_session(session_id: str, request: Request) -> JSONResponse:
    body = await request.json()
    title = str(body.get("title") or "").strip()
    if not title:
        return JSONResponse({"error": "Title cannot be empty"}, status_code=400)
    session = get_session_store().rename_session(session_id, title)
    if session is None:
        return JSONResponse({"error": "Session not found"}, status_code=404)
    return JSONResponse(_session_payload(session))


@app.delete("/sessions/{session_id}")
async def delete_session(session_id: str) -> JSONResponse:
    deleted = get_session_store().delete_session(session_id)
    if not deleted:
        return JSONResponse({"error": "Session not found"}, status_code=404)
    return JSONResponse({"ok": True, "session_id": session_id})


# ---------------------------------------------------------------------------
# Chat endpoints
# ---------------------------------------------------------------------------


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
        previous_session_env = os.environ.get(_CURRENT_SESSION_ID_ENV)
        os.environ[_CURRENT_SESSION_ID_ENV] = session["session_id"]
        event_hub = get_session_event_hub()
        event_queue = event_hub.subscribe(session["session_id"])
        try:
            agent_iter = stream_agent_events(agent, message, session["session_id"], merged_history).__aiter__()
            pending_agent = asyncio.create_task(agent_iter.__anext__())
            pending_subagent = asyncio.create_task(event_queue.get())

            while True:
                done, _ = await asyncio.wait(
                    {pending_agent, pending_subagent},
                    return_when=asyncio.FIRST_COMPLETED,
                )

                if pending_subagent in done:
                    subagent_event = pending_subagent.result()
                    pending_subagent = asyncio.create_task(event_queue.get())
                    yield {
                        "event": "debug",
                        "data": json.dumps(
                            {
                                "stage": "subagent_notification",
                                "message": subagent_event.get("message", "Subagent notification"),
                                "elapsed_seconds": None,
                                **subagent_event,
                            },
                            ensure_ascii=False,
                        ),
                    }

                if pending_agent in done:
                    try:
                        event = pending_agent.result()
                    except StopAsyncIteration:
                        pending_agent = None
                        break
                    pending_agent = asyncio.create_task(agent_iter.__anext__())

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
        finally:
            event_hub.unsubscribe(session["session_id"], event_queue)
            if previous_session_env is None:
                os.environ.pop(_CURRENT_SESSION_ID_ENV, None)
            else:
                os.environ[_CURRENT_SESSION_ID_ENV] = previous_session_env

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
    previous_session_env = os.environ.get(_CURRENT_SESSION_ID_ENV)
    os.environ[_CURRENT_SESSION_ID_ENV] = session["session_id"]
    try:
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
    finally:
        if previous_session_env is None:
            os.environ.pop(_CURRENT_SESSION_ID_ENV, None)
        else:
            os.environ[_CURRENT_SESSION_ID_ENV] = previous_session_env

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
