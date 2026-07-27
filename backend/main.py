from __future__ import annotations

import json
import asyncio
import re
import time
from pathlib import Path
from threading import RLock
from typing import Any
from uuid import uuid4

import uvicorn
from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sse_starlette.sse import EventSourceResponse

from .agent import build_agent, stream_agent_events
from .config import get_runtime_value
from .session_store import SessionStore, TOOL_EVENT_SCHEMA_VERSION
from .session_events import get_session_event_hub
from .subagent_runtime import get_subagent_manager as get_runtime_subagent_manager
from .subagents import built_in_subagents, read_subagent_task_state
from .token_counter import count_message_tokens, count_text_tokens, normalize_usage
from .runtime_context import bind_runtime_context, reset_runtime_context
from .tools import clear_tool_dedupe_cache
from .feishu_web_login import (
    FeishuWebSessionStore,
    bootstrap_feishu_session,
    feishu_session_status_payload,
    init_feishu_qr_login,
    poll_feishu_qr_login,
)
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


@app.on_event("startup")
async def bootstrap_feishu_auth_on_startup() -> None:
    status = bootstrap_feishu_session(get_feishu_session_store())
    print(
        "[feishu] startup session status: "
        f"logged_in={status.get('logged_in')} reason={status.get('reason')} "
        f"source={status.get('source')} path={status.get('path')}"
    )

# ---------------------------------------------------------------------------
# Agent singleton (lazy init on first request)
# ---------------------------------------------------------------------------
_agent: Any = None
_agent_lock: Any = None
_session_store: SessionStore | None = None
_ACTIVE_RUNS: dict[str, dict[str, Any]] = {}
_ACTIVE_RUNS_LOCK = RLock()


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


def _acquire_session_run(session_id: str, endpoint: str) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    normalized_session_id = str(session_id or "default").strip() or "default"
    with _ACTIVE_RUNS_LOCK:
        active = _ACTIVE_RUNS.get(normalized_session_id)
        if active:
            return None, dict(active)
        run = {
            "session_id": normalized_session_id,
            "run_id": f"{normalized_session_id}:{uuid4().hex}",
            "endpoint": endpoint,
            "status": "running",
            "started_at": time.time(),
        }
        _ACTIVE_RUNS[normalized_session_id] = run
        return dict(run), None


def _release_session_run(session_id: str, run_id: str) -> None:
    normalized_session_id = str(session_id or "default").strip() or "default"
    with _ACTIVE_RUNS_LOCK:
        active = _ACTIVE_RUNS.get(normalized_session_id)
        if active and active.get("run_id") == run_id:
            _ACTIVE_RUNS.pop(normalized_session_id, None)


def _active_run_error_payload(session_id: str, active_run: dict[str, Any] | None) -> dict[str, Any]:
    return {
        "error": "session_run_active",
        "message": "A foreground run is already active for this session.",
        "session_id": session_id,
        "run_id": str((active_run or {}).get("run_id") or ""),
        "status": "running",
    }


def _with_run_attribution(event: dict[str, Any], parsed: Any, session_id: str, run_id: str) -> dict[str, Any]:
    if not isinstance(parsed, dict):
        return event
    payload = {
        **parsed,
        "session_id": session_id,
        "run_id": run_id,
    }
    return {
        "event": str(event.get("event") or ""),
        "data": json.dumps(payload, ensure_ascii=False),
    }


def _workspace() -> Path:
    return Path.cwd().resolve()


MAX_UPLOAD_FILES = 10
MAX_UPLOAD_BYTES = 25 * 1024 * 1024
_PRIVATE_TOOL_ARGUMENT_KEYS = {"runtime", "config", "callbacks", "store", "context", "state"}


def _safe_upload_component(value: str, fallback: str) -> str:
    name = Path(value or "").name
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip("._")
    return cleaned or fallback


@app.post("/uploads")
async def upload_files(
    session_id: str = Form("default"),
    files: list[UploadFile] = File(...),
) -> JSONResponse:
    if not files:
        return JSONResponse({"error": "At least one file is required."}, status_code=400)
    if len(files) > MAX_UPLOAD_FILES:
        return JSONResponse(
            {"error": f"At most {MAX_UPLOAD_FILES} files can be uploaded at once."},
            status_code=400,
        )

    workspace = _workspace()
    safe_session = _safe_upload_component(session_id, "default")
    upload_dir = workspace / "tmp" / "uploads" / safe_session
    upload_dir.mkdir(parents=True, exist_ok=True)
    uploaded: list[dict[str, Any]] = []

    for upload in files:
        original_name = Path(upload.filename or "attachment").name
        safe_name = _safe_upload_component(original_name, "attachment")
        target = upload_dir / f"{uuid4().hex[:10]}_{safe_name}"
        size = 0
        try:
            with target.open("wb") as handle:
                while chunk := await upload.read(1024 * 1024):
                    size += len(chunk)
                    if size > MAX_UPLOAD_BYTES:
                        handle.close()
                        target.unlink(missing_ok=True)
                        return JSONResponse(
                            {
                                "error": (
                                    f"File '{original_name}' exceeds the "
                                    f"{MAX_UPLOAD_BYTES // (1024 * 1024)} MB limit."
                                )
                            },
                            status_code=413,
                        )
                    handle.write(chunk)
        finally:
            await upload.close()

        uploaded.append(
            {
                "name": original_name,
                "path": target.relative_to(workspace).as_posix(),
                "size": size,
                "content_type": upload.content_type or "application/octet-stream",
            }
        )

    return JSONResponse({"files": uploaded})


def get_feishu_session_store() -> FeishuWebSessionStore:
    return FeishuWebSessionStore(_workspace())


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
    stored_history: list[dict[str, Any]],
    incoming_history: list[dict[str, Any]] | None,
) -> list[dict[str, Any]]:
    if not incoming_history:
        return stored_history
    if incoming_history == stored_history:
        return list(stored_history)
    if incoming_history[: len(stored_history)] == stored_history:
        return incoming_history
    # Prefer canonical persisted history; only append truly new trailing items.
    merged = list(stored_history)
    overlap = min(len(stored_history), len(incoming_history))
    if overlap and stored_history[-overlap:] == incoming_history[:overlap]:
        merged.extend(incoming_history[overlap:])
        return merged
    return list(stored_history)


def _assistant_tool_transcript_event(
    event_type: str,
    payload: Any,
    sequence: int,
    pending_ids: dict[str, list[str]],
) -> dict[str, Any]:
    data = payload if isinstance(payload, dict) else {}
    name = str(data.get("name") or "tool").strip() or "tool"
    tool_call_id = str(data.get("tool_call_id") or "").strip()
    if event_type == "tool_call":
        tool_call_id = tool_call_id or f"fallback-{sequence}"
        pending_ids.setdefault(name, []).append(tool_call_id)
        return {
            "schema_version": TOOL_EVENT_SCHEMA_VERSION,
            "type": "tool_call",
            "sequence": sequence,
            "tool_call_id": tool_call_id,
            "name": name,
            "arguments": data.get("arguments"),
        }

    pending = pending_ids.get(name, [])
    if not tool_call_id and pending:
        tool_call_id = pending.pop(0)
    elif tool_call_id and tool_call_id in pending:
        pending.remove(tool_call_id)
    if not pending:
        pending_ids.pop(name, None)
    tool_call_id = tool_call_id or f"fallback-{sequence}"
    return {
        "schema_version": TOOL_EVENT_SCHEMA_VERSION,
        "type": "tool_result",
        "sequence": sequence,
        "tool_call_id": tool_call_id,
        "name": name,
        "content": data.get("content"),
    }


def _public_session_value(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            str(key): _public_session_value(item)
            for key, item in value.items()
            if str(key).lower() not in _PRIVATE_TOOL_ARGUMENT_KEYS
        }
    if isinstance(value, list):
        return [_public_session_value(item) for item in value]
    return value


def _session_payload(session: dict[str, Any]) -> dict[str, Any]:
    messages = session.get("messages", [])
    history_messages = [
        item for item in messages
        if item.get("role") in {"user", "assistant"} and isinstance(item.get("content"), str)
    ]
    history_chars = sum(len(item.get("content", "")) for item in history_messages)
    history_token_estimate = count_message_tokens(
        [{"role": item.get("role", "user"), "content": item.get("content", "")} for item in history_messages]
    )
    return {
        "session_id": session.get("session_id"),
        "title": session.get("title"),
        "created_at": session.get("created_at"),
        "updated_at": session.get("updated_at"),
        "messages": _public_session_value(messages),
        "task_progress": _public_session_value(session.get("task_progress", {})),
        "subagent_tasks": _public_session_value(session.get("subagent_tasks", [])),
        "subagent_notifications": _public_session_value(session.get("subagent_notifications", [])),
        "context_stats": {
            "history_messages": len(history_messages),
            "history_chars": history_chars,
            "history_token_estimate": history_token_estimate,
            "usage": session.get("task_progress", {}).get("usage", {}),
        },
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


def _compact_summary_text(value: Any, limit: int = 500) -> str:
    text = str(value or "").strip()
    if len(text) <= limit:
        return text
    return f"{text[:limit - 80]}... [truncated {len(text) - limit + 80} chars]"


def _tool_counts(tools: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for item in tools:
        if item.get("type") != "tool_call":
            continue
        name = str(item.get("name") or "tool")
        counts[name] = counts.get(name, 0) + 1
    return counts


def _extract_artifact_paths(tools: list[dict[str, Any]]) -> list[str]:
    paths: list[str] = []
    path_pattern = re.compile(
        r"(?:(?:[A-Za-z]:[\\/][^\s\"'<>|]+)|(?:tmp[\\/][^\s\"'<>|]+)|(?:sessionss[\\/][^\s\"'<>|]+)|(?:docs[\\/][^\s\"'<>|]+)|(?:frontend[\\/][^\s\"'<>|]+)|(?:backend[\\/][^\s\"'<>|]+))"
    )
    for item in tools:
        args = item.get("arguments")
        if isinstance(args, dict):
            for key in ("path", "output_path", "artifact_path", "file_path"):
                raw = args.get(key)
                if isinstance(raw, str) and raw.strip():
                    paths.append(raw.strip())
        content = item.get("content")
        if isinstance(content, str):
            paths.extend(match.group(0).rstrip(".,);]") for match in path_pattern.finditer(content))
    unique: list[str] = []
    seen = set()
    for path in paths:
        if path not in seen:
            seen.add(path)
            unique.append(path)
    return unique[:20]


def _infer_modifications(tools: list[dict[str, Any]]) -> list[str]:
    modifications: list[str] = []
    for item in tools:
        if item.get("type") != "tool_call":
            continue
        name = str(item.get("name") or "")
        args = item.get("arguments") if isinstance(item.get("arguments"), dict) else {}
        target = args.get("path") or args.get("artifact_path") or args.get("output_path") or ""
        if name in {"write_file", "append_file", "delete_file"}:
            modifications.append(f"{name}: {target}".strip())
        elif name == "run_python_file":
            modifications.append(f"verification/script run: {target or args.get('path', '')}".strip())
        elif name in {"record_task_item", "update_task_item", "record_script_stage", "record_pitfall"}:
            modifications.append(f"progress memory updated via {name}")
    return modifications[:20]


def _build_execution_summary(
    *,
    user_message: str,
    final_text: str,
    tools: list[dict[str, Any]],
    status: str,
    failure_reason: str = "",
) -> dict[str, Any]:
    counts = _tool_counts(tools)
    artifact_paths = _extract_artifact_paths(tools)
    modifications = _infer_modifications(tools)
    verification_tools = [
        name for name in counts
        if name in {"run_python_file", "Agent", "get_subagent_task"} or "verification" in name.lower()
    ]
    return {
        "status": status,
        "headline": "Task completed." if status == "completed" else "Task did not complete cleanly.",
        "user_request": _compact_summary_text(user_message, 800),
        "final_response_preview": _compact_summary_text(final_text, 1200),
        "tasks_completed": [
            "Processed the user request through the agent event pipeline.",
            "Captured tool calls and results for auditability.",
            "Persisted an execution summary into the current session.",
        ],
        "modifications_or_adjustments": modifications,
        "saved_or_downloaded": artifact_paths,
        "tool_counts": counts,
        "verification": verification_tools or ["not detected"],
        "successful_methods": [
            "Use the current session's task_progress.execution_summary to recover the latest progress.",
            "Check task_progress.script_stages for recent execution_summary entries.",
            "Inspect saved_or_downloaded paths before rerunning expensive or destructive steps.",
        ],
        "failure_reason": failure_reason,
    }


def _append_persisted_summary(final_text: str, summary: dict[str, Any]) -> str:
    body = (final_text or "").strip()
    if "Persistent Progress Summary" in body:
        return body
    lines = [
        "**Persistent Progress Summary**",
        f"- Status: {summary.get('status')}",
        f"- Request: {summary.get('user_request')}",
        f"- Tools: {', '.join(f'{name} x{count}' if count > 1 else name for name, count in summary.get('tool_counts', {}).items()) or 'none'}",
    ]
    modifications = summary.get("modifications_or_adjustments") or []
    if modifications:
        lines.append(f"- Modified/adjusted: {'; '.join(modifications[:5])}")
    artifacts = summary.get("saved_or_downloaded") or []
    if artifacts:
        lines.append(f"- Saved/downloaded: {'; '.join(artifacts[:5])}")
    verification = summary.get("verification") or []
    lines.append(f"- Verification: {', '.join(verification)}")
    if summary.get("failure_reason"):
        lines.append(f"- Failure reason: {summary.get('failure_reason')}")
    lines.append("- Memory: saved to this session under task_progress.execution_summary and task_progress.script_stages.")
    return f"{body}\n\n" + "\n".join(lines) if body else "\n".join(lines)


def _append_result_locations(final_text: str, result_context: dict[str, Any]) -> str:
    body = (final_text or "").strip()
    if "**Result Location**" in body or "**结果位置**" in body:
        return body
    lines: list[str] = []
    primary = result_context.get("primary_result")
    if isinstance(primary, dict):
        target = primary.get("url") or primary.get("path")
        if target:
            lines.append("**Result Location**")
            lines.append(f"- Primary: {target}")
            if primary.get("summary"):
                lines.append(f"- Summary: {primary.get('summary')}")
            if primary.get("record_count") is not None:
                lines.append(f"- Records: {primary.get('record_count')}")
    artifacts = result_context.get("artifacts")
    if isinstance(artifacts, list):
        primary_target = ""
        if isinstance(primary, dict):
            primary_target = str(primary.get("url") or primary.get("path") or "")
        primary_artifacts = [
            item for item in artifacts
            if isinstance(item, dict)
            and item.get("role") == "primary"
            and (item.get("url") or item.get("path"))
            and (item.get("status") in {"written", "verified"} or item.get("record_count") is not None or item.get("verified"))
            and str(item.get("url") or item.get("path") or "") != primary_target
        ]
        if primary_artifacts and not lines:
            lines.append("**Result Location**")
        for item in primary_artifacts[:3]:
            target = item.get("url") or item.get("path")
            lines.append(f"- {item.get('title') or item.get('type') or 'Artifact'}: {target}")
    if not lines:
        return body
    return f"{body}\n\n" + "\n".join(lines) if body else "\n".join(lines)


def _looks_like_raw_failure_output(final_text: str) -> bool:
    body = (final_text or "").strip().lower()
    return (
        body.startswith("agent execution failed")
        or body.startswith("traceback (most recent call last)")
    )


def _extract_exception_summary(text: str) -> tuple[str, str]:
    lines = [line.strip() for line in (text or "").splitlines() if line.strip()]
    exception_line = ""
    for line in reversed(lines):
        if re.match(r"^[A-Za-z_][A-Za-z0-9_.]*(Error|Exception):", line):
            exception_line = line
            break
    if not exception_line:
        for line in reversed(lines):
            if ":" in line and not line.startswith("File "):
                exception_line = line
                break
    if not exception_line:
        return "Unknown execution error.", "The runtime returned an exception without a concise final error line."

    lowered = exception_line.lower()
    if "could not determine the requested operation" in lowered:
        diagnosis = "The skill runner could not map the request to a supported standardized command."
    elif "missing <token|url>" in lowered:
        diagnosis = "The command is missing a required target token or URL."
    elif "exceeded" in lowered:
        diagnosis = "A runtime budget or tool-call limit was exceeded."
    elif "permission" in lowered or "access denied" in lowered:
        diagnosis = "The operation hit a permission or sandbox boundary."
    else:
        diagnosis = "A tool or subprocess failed and the task did not complete."
    return exception_line, diagnosis


def _summarize_raw_failure_output(final_text: str, fallback_reason: str = "") -> str:
    exception_line, diagnosis = _extract_exception_summary(final_text or fallback_reason)
    return (
        "任务未完成。\n\n"
        "**错误分析**\n"
        f"- 异常摘要: `{exception_line}`\n"
        f"- 可能原因: {diagnosis}\n"
        "- 处理要求: 不应把原始 traceback 直接返回给用户，应基于异常摘要修正命令、参数或执行路径后继续。\n\n"
        "**建议下一步**\n"
        "- 根据上面的异常摘要调整工具调用；如果是命令格式问题，改用标准 CLI 命令后重试。"
    )


def _sanitize_failure_reason(reason: str) -> str:
    text = (reason or "").strip()
    if not text:
        return ""
    if "traceback (most recent call last)" in text.lower():
        exception_line, diagnosis = _extract_exception_summary(text)
        return f"{exception_line} ({diagnosis})"
    return text


def _extract_embedded_execution_status(final_text: str) -> tuple[str, str]:
    """Recover status emitted inside the agent's final execution summary."""
    status = ""
    failure_reason = ""
    for line in (final_text or "").splitlines():
        stripped = line.strip().lstrip("-").strip()
        lowered = stripped.lower()
        if lowered.startswith("status:") or stripped.startswith("状态:"):
            raw_status = stripped.split(":", 1)[1].strip().lower()
            if raw_status.startswith(("failed", "blocked", "completed")):
                status = raw_status.split()[0]
        elif lowered.startswith("failure reason:") or stripped.startswith("失败原因:"):
            failure_reason = stripped.split(":", 1)[1].strip()
    return status, failure_reason


def _finalize_agent_response(
    *,
    store: SessionStore,
    session_id: str,
    user_message: str,
    final_text: str,
    tools: list[dict[str, Any]],
    status: str = "completed",
    failure_reason: str = "",
) -> str:
    unfinished_todos = store.get_unfinished_task_plan_todos(session_id)
    if status == "completed" and _looks_like_raw_failure_output(final_text):
        status = "failed"
        failure_reason = failure_reason or "Final response contains raw execution failure output."
        final_text = _summarize_raw_failure_output(final_text, failure_reason)
    embedded_status, embedded_failure_reason = _extract_embedded_execution_status(final_text)
    if status == "completed" and embedded_status in {"failed", "blocked"}:
        status = embedded_status
        failure_reason = failure_reason or embedded_failure_reason or "Agent execution summary reported an incomplete run."
    failure_reason = _sanitize_failure_reason(failure_reason)
    if status == "completed" and unfinished_todos and embedded_status != "completed":
        status = "blocked"
        failure_reason = failure_reason or "Task plan has unfinished todo items."
    summary = _build_execution_summary(
        user_message=user_message,
        final_text=final_text,
        tools=tools,
        status=status,
        failure_reason=failure_reason,
    )
    final_with_summary = _append_persisted_summary(final_text, summary)
    store.record_execution_summary(session_id, summary)
    final_with_summary = _append_result_locations(final_with_summary, store.get_resume_context(session_id))
    store.append_tool_event(
        session_id,
        event_type="turn_result",
        tool_name="assistant_final",
        arguments={"status": status, "tool_counts": summary.get("tool_counts", {})},
        content=final_with_summary,
    )
    store.append_task_journal(
        session_id,
        {
            "event": "turn_end",
            "status": status,
            "failure_reason": failure_reason,
            "tool_counts": summary.get("tool_counts", {}),
            "final_response_preview": summary.get("final_response_preview", ""),
            "unfinished_todos": unfinished_todos,
        },
    )
    return final_with_summary


# ---------------------------------------------------------------------------
# REST endpoints
# ---------------------------------------------------------------------------


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/feishu/login/init")
@app.post("/feishu/login/init/")
@app.post("/feishu/init")
@app.post("/feishu/qr/init")
async def feishu_login_init() -> JSONResponse:
    try:
        state = init_feishu_qr_login()
    except Exception as exc:
        return JSONResponse({"error": f"Failed to initialize Feishu QR login: {exc}"}, status_code=500)
    if not state.flow_key or not state.token:
        return JSONResponse({"error": "Feishu QR login did not return a valid token/flow_key."}, status_code=502)
    return JSONResponse(
        {
            "token": state.token,
            "flow_key": state.flow_key,
            "qr_content": state.qr_content,
            "qr_png_base64": state.qr_png_base64,
            "created_at": state.created_at,
        }
    )


@app.post("/feishu/login/poll")
@app.post("/feishu/login/poll/")
@app.post("/feishu/poll")
@app.post("/feishu/qr/poll")
async def feishu_login_poll(request: Request) -> JSONResponse:
    body = await request.json()
    flow_key = str(body.get("flow_key") or "").strip()
    if not flow_key:
        return JSONResponse({"error": "flow_key is required"}, status_code=400)
    try:
        result = poll_feishu_qr_login(flow_key)
    except Exception as exc:
        return JSONResponse({"error": f"Failed to poll Feishu QR login: {exc}"}, status_code=500)

    session_value = result.get("session")
    if session_value:
        get_feishu_session_store().save(
            {
                "session": session_value,
                "issued_at": time.time(),
                "metadata": {
                    "source": "qr_login",
                    "status": result.get("status"),
                    "next_step": result.get("next_step"),
                },
            }
        )
    return JSONResponse(result)


@app.get("/feishu/session")
@app.get("/feishu/status")
async def feishu_session_status() -> JSONResponse:
    status = bootstrap_feishu_session(get_feishu_session_store())
    return JSONResponse(status)


@app.delete("/feishu/session")
@app.delete("/feishu/status")
async def feishu_session_clear() -> JSONResponse:
    get_feishu_session_store().clear()
    return JSONResponse({"ok": True})


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

    store = get_session_store()
    session = store.create_or_get_session(session_id, first_message=message)
    run, active_run = _acquire_session_run(session["session_id"], "chat_stream")
    if active_run:
        return EventSourceResponse(
            [
                {
                    "event": "error",
                    "data": json.dumps(_active_run_error_payload(session["session_id"], active_run), ensure_ascii=False),
                }
            ]
        )
    run_id = str(run["run_id"])

    try:
        agent = await get_agent()
        stored_history = store.get_history(session["session_id"])
        merged_history = _merge_history(stored_history, history)
    except Exception:
        _release_session_run(session["session_id"], run_id)
        clear_tool_dedupe_cache(run_id)
        raise

    store.append_message(
        session["session_id"],
        "user",
        message,
        tools=[],
        usage={"content_tokens": count_text_tokens(message)},
    )
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
        pending_tool_ids: dict[str, list[str]] = {}
        run_usage: dict[str, Any] = {}
        turn_finalized = False
        interrupted = False
        terminal_status = ""
        terminal_failure_reason = ""
        context_tokens = bind_runtime_context(session["session_id"], run_id)
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
                    subagent_debug_event = {
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
                    yield _with_run_attribution(
                        subagent_debug_event,
                        json.loads(subagent_debug_event["data"]),
                        session["session_id"],
                        run_id,
                    )

                if pending_agent in done:
                    try:
                        event = pending_agent.result()
                    except StopAsyncIteration:
                        pending_agent = None
                        break
                    pending_agent = asyncio.create_task(agent_iter.__anext__())

                    event_type = event.get("event", "")
                    private_transcript_event = event_type in {"tool_transcript_call", "tool_transcript_result"}
                    raw_data = event.get("data", "")
                    parsed: Any
                    try:
                        parsed = json.loads(raw_data)
                    except Exception:
                        parsed = raw_data

                    if event_type == "text":
                        assistant_text += str(parsed)
                    elif event_type == "tool_transcript_call":
                        transcript_event = _assistant_tool_transcript_event(
                            "tool_call", parsed, len(assistant_tools), pending_tool_ids
                        )
                        assistant_tools.append(transcript_event)
                        store.append_tool_event(
                            session["session_id"],
                            event_type="tool_call",
                            tool_name=transcript_event["name"],
                            tool_call_id=transcript_event["tool_call_id"],
                            arguments=transcript_event.get("arguments"),
                        )
                    elif event_type == "tool_call":
                        store.update_progress(
                            session["session_id"],
                            status="running",
                            active_tool=parsed.get("name"),
                            message=f"Calling tool `{parsed.get('name', 'tool')}`.",
                            last_debug_stage="tool_start",
                        )
                    elif event_type == "tool_transcript_result":
                        transcript_event = _assistant_tool_transcript_event(
                            "tool_result", parsed, len(assistant_tools), pending_tool_ids
                        )
                        assistant_tools.append(transcript_event)
                        store.append_tool_event(
                            session["session_id"],
                            event_type="tool_result",
                            tool_name=transcript_event["name"],
                            tool_call_id=transcript_event["tool_call_id"],
                            content=transcript_event.get("content"),
                        )
                    elif event_type == "tool_result":
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
                        if parsed.get("stage") == "agent_start" and isinstance(parsed.get("context_token_estimate"), int):
                            run_usage.setdefault("input_tokens", parsed.get("context_token_estimate", 0))
                        if parsed.get("stage") == "model_usage" and isinstance(parsed.get("usage"), dict):
                            run_usage = {
                                **run_usage,
                                **(parsed.get("usage") or {}),
                            }
                            store.update_progress(
                                session["session_id"],
                                usage=normalize_usage(run_usage, output_text=assistant_text),
                            )
                        store.update_progress(
                            session["session_id"],
                            status="running",
                            active_tool=parsed.get("active_tool"),
                            message=parsed.get("message", ""),
                            elapsed_seconds=parsed.get("elapsed_seconds", 0),
                            last_debug_stage=parsed.get("stage", ""),
                        )
                    elif event_type == "error" and isinstance(parsed, dict):
                        assistant_text = str(parsed.get("message") or assistant_text)
                        if parsed.get("code") == "protected_context_capacity_exceeded":
                            terminal_status = "failed"
                            terminal_failure_reason = assistant_text
                    elif event_type == "done":
                        assistant_text = str(parsed or assistant_text)
                        resolved_status = terminal_status or ("completed" if assistant_text.strip() else "failed")
                        resolved_failure_reason = terminal_failure_reason or (
                            "" if assistant_text.strip() else "Agent returned no final text."
                        )
                        assistant_text = _finalize_agent_response(
                            store=store,
                            session_id=session["session_id"],
                            user_message=message,
                            final_text=assistant_text,
                            tools=assistant_tools,
                            status=resolved_status,
                            failure_reason=resolved_failure_reason,
                        )
                        event = {"event": "done", "data": json.dumps(assistant_text, ensure_ascii=False)}
                        final_usage = normalize_usage(run_usage, output_text=assistant_text)
                        store.replace_last_assistant_message(
                            session["session_id"],
                            assistant_text,
                            tools=assistant_tools,
                            usage=final_usage,
                        )
                        store.update_progress(session["session_id"], usage=final_usage)
                        store.update_progress(
                            session["session_id"],
                            status="idle" if not terminal_status else terminal_status,
                            active_tool=None,
                            message="Ready to continue." if not terminal_status else terminal_failure_reason,
                            elapsed_seconds=0,
                            last_debug_stage="done" if not terminal_status else "protected_context_capacity_exceeded",
                        )
                        turn_finalized = True

                    if await request.is_disconnected():
                        interrupted = True
                        break
                    if not private_transcript_event:
                        yield _with_run_attribution(event, parsed, session["session_id"], run_id)
        finally:
            if not turn_finalized and (assistant_text.strip() or assistant_tools):
                partial_text = assistant_text.strip() or "(stopped before text reply)"
                failure_reason = (
                    "Client disconnected before the agent finished. "
                    "Continue from persisted task_plan and task_outputs instead of restarting."
                    if interrupted
                    else "Agent stream ended before a final done event. Continue from persisted context."
                )
                partial_text = _finalize_agent_response(
                    store=store,
                    session_id=session["session_id"],
                    user_message=message,
                    final_text=partial_text,
                    tools=assistant_tools,
                    status="blocked",
                    failure_reason=failure_reason,
                )
                partial_usage = normalize_usage(run_usage, output_text=partial_text)
                store.replace_last_assistant_message(
                    session["session_id"],
                    partial_text,
                    tools=assistant_tools,
                    usage=partial_usage,
                )
                store.update_progress(
                    session["session_id"],
                    status="blocked",
                    active_tool=None,
                    message=failure_reason,
                    elapsed_seconds=0,
                    last_debug_stage="interrupted",
                    usage=partial_usage,
                )
            event_hub.unsubscribe(session["session_id"], event_queue)
            reset_runtime_context(context_tokens)
            clear_tool_dedupe_cache(run_id)
            _release_session_run(session["session_id"], run_id)

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
    session = store.create_or_get_session(session_id, first_message=message)
    run, active_run = _acquire_session_run(session["session_id"], "chat_sync")
    if active_run:
        return JSONResponse(_active_run_error_payload(session["session_id"], active_run), status_code=409)

    run_id = str(run["run_id"])
    context_tokens = None
    try:
        stored_history = store.get_history(session["session_id"])
        merged_history = _merge_history(stored_history, history)

        store.append_message(
            session["session_id"],
            "user",
            message,
            tools=[],
            usage={"content_tokens": count_text_tokens(message)},
        )
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
        pending_tool_ids: dict[str, list[str]] = {}
        run_usage: dict[str, Any] = {}
        terminal_status = ""
        terminal_failure_reason = ""
        context_tokens = bind_runtime_context(session["session_id"], run_id)
        try:
            async for event in stream_agent_events(agent, message, session["session_id"], merged_history):
                event_type = event["event"]
                try:
                    parsed = json.loads(event["data"])
                except Exception:
                    parsed = event["data"]
                if event_type == "text":
                    final_text += str(parsed)
                elif event_type == "tool_transcript_call":
                    transcript_event = _assistant_tool_transcript_event(
                        "tool_call", parsed, len(tools), pending_tool_ids
                    )
                    tools.append(transcript_event)
                    store.append_tool_event(
                        session["session_id"],
                        event_type="tool_call",
                        tool_name=transcript_event["name"],
                        tool_call_id=transcript_event["tool_call_id"],
                        arguments=transcript_event.get("arguments"),
                    )
                elif event_type == "tool_call":
                    pass
                elif event_type == "tool_transcript_result":
                    transcript_event = _assistant_tool_transcript_event(
                        "tool_result", parsed, len(tools), pending_tool_ids
                    )
                    tools.append(transcript_event)
                    store.append_tool_event(
                        session["session_id"],
                        event_type="tool_result",
                        tool_name=transcript_event["name"],
                        tool_call_id=transcript_event["tool_call_id"],
                        content=transcript_event.get("content"),
                    )
                elif event_type == "tool_result":
                    pass
                elif event_type == "error" and isinstance(parsed, dict):
                    final_text = str(parsed.get("message") or final_text)
                    if parsed.get("code") == "protected_context_capacity_exceeded":
                        terminal_status = "failed"
                        terminal_failure_reason = final_text
                elif event_type == "debug" and parsed.get("stage") == "agent_start" and isinstance(parsed.get("context_token_estimate"), int):
                    run_usage.setdefault("input_tokens", parsed.get("context_token_estimate", 0))
                elif event_type == "debug" and parsed.get("stage") == "model_usage" and isinstance(parsed.get("usage"), dict):
                    run_usage = {
                        **run_usage,
                        **(parsed.get("usage") or {}),
                    }
                elif event_type == "done":
                    final_text = str(parsed or final_text)
        finally:
            reset_runtime_context(context_tokens)
            clear_tool_dedupe_cache(run_id)

        final_text = _finalize_agent_response(
            store=store,
            session_id=session["session_id"],
            user_message=message,
            final_text=final_text,
            tools=tools,
            status=terminal_status or ("completed" if final_text.strip() else "failed"),
            failure_reason=terminal_failure_reason or ("" if final_text.strip() else "Agent returned no final text."),
        )
        final_usage = normalize_usage(run_usage, output_text=final_text)
        store.replace_last_assistant_message(session["session_id"], final_text, tools=tools, usage=final_usage)
        store.update_progress(
            session["session_id"],
            status="idle" if not terminal_status else terminal_status,
            active_tool=None,
            message="Ready to continue." if not terminal_status else terminal_failure_reason,
            elapsed_seconds=0,
            last_debug_stage="done" if not terminal_status else "protected_context_capacity_exceeded",
            usage=final_usage,
        )
        return JSONResponse({"reply": final_text, "session_id": session["session_id"], "usage": final_usage})
    finally:
        _release_session_run(session["session_id"], run_id)


if __name__ == "__main__":
    uvicorn.run(
        "backend.main:app",
        host=str(get_runtime_value("app", "backend_bind_host", "0.0.0.0")),
        port=int(get_runtime_value("app", "backend_port", 8000)),
        reload=True,
    )
