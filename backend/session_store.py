from __future__ import annotations

import json
import re
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from .config import get_runtime_value

MAX_SESSION_TITLE_LENGTH = 40
MAX_STORED_MESSAGE_CHARS = 80_000
MAX_STORED_TOOL_CONTENT_CHARS = 16_000
MAX_STORED_TOOL_ARGUMENT_CHARS = 8_000
_SESSION_FILE_LOCK = threading.RLock()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _slugify_session_id(value: str) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9._-]+", "-", (value or "").strip())
    cleaned = cleaned.strip(".-")
    return cleaned or f"session-{uuid4().hex[:12]}"


def _make_title(message: str) -> str:
    title = re.sub(r"\s+", " ", (message or "").strip())
    if not title:
        return "New Session"
    if len(title) <= MAX_SESSION_TITLE_LENGTH:
        return title
    return f"{title[:MAX_SESSION_TITLE_LENGTH - 3]}..."


def _compact_text(value: Any, limit: int) -> Any:
    if not isinstance(value, str) or len(value) <= limit:
        return value
    head = value[: max(0, limit - 220)]
    return f"{head}\n\n[... truncated {len(value) - len(head)} chars for session storage ...]"


def _compact_jsonable(value: Any, limit: int) -> Any:
    if isinstance(value, str):
        return _compact_text(value, limit)
    if isinstance(value, dict):
        return {str(key): _compact_jsonable(item, limit) for key, item in value.items()}
    if isinstance(value, list):
        return [_compact_jsonable(item, limit) for item in value]
    return value


def _compact_tool(tool: dict[str, Any]) -> dict[str, Any]:
    item = dict(tool)
    if "content" in item:
        item["content"] = _compact_text(item.get("content"), MAX_STORED_TOOL_CONTENT_CHARS)
    if "arguments" in item:
        item["arguments"] = _compact_jsonable(item.get("arguments"), MAX_STORED_TOOL_ARGUMENT_CHARS)
    return item


def _compact_message(message: dict[str, Any]) -> dict[str, Any]:
    item = dict(message)
    item["content"] = _compact_text(item.get("content"), MAX_STORED_MESSAGE_CHARS)
    usage = item.get("usage")
    item["usage"] = usage if isinstance(usage, dict) else {}
    tools = item.get("tools", [])
    if isinstance(tools, list):
        item["tools"] = [_compact_tool(tool) if isinstance(tool, dict) else tool for tool in tools]
    else:
        item["tools"] = []
    return item


class SessionStore:
    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        session_dir = str(get_runtime_value("paths", "session_dir", "sessionss") or "sessionss")
        self.sessions_dir = self.root / session_dir
        self.sessions_dir.mkdir(parents=True, exist_ok=True)

    def session_path(self, session_id: str) -> Path:
        return self.sessions_dir / f"{_slugify_session_id(session_id)}.json"

    def _is_reserved_session_file(self, path: Path) -> bool:
        feishu_session_file = str(
            get_runtime_value("paths", "feishu_session_file", "feishu_web_session.json")
            or "feishu_web_session.json"
        )
        return path.name == feishu_session_file

    def _is_archived_session_file(self, path: Path) -> bool:
        return bool(re.search(r"\.corrupt-[0-9a-f]+\.json$", path.name, re.IGNORECASE))

    def _looks_like_chat_session(self, data: dict[str, Any]) -> bool:
        return (
            isinstance(data, dict)
            and isinstance(data.get("session_id"), str)
            and isinstance(data.get("messages", []), list)
        )

    def list_sessions(self) -> list[dict[str, Any]]:
        sessions: list[dict[str, Any]] = []
        seen_session_ids: set[str] = set()
        for path in sorted(
            self.sessions_dir.glob("*.json"),
            key=lambda item: item.stat().st_mtime,
            reverse=True,
        ):
            if self._is_reserved_session_file(path) or self._is_archived_session_file(path):
                continue
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                continue
            if not self._looks_like_chat_session(data):
                continue
            session_id = data.get("session_id", path.stem)
            if session_id in seen_session_ids:
                continue
            seen_session_ids.add(session_id)
            sessions.append(
                {
                    "session_id": session_id,
                    "title": data.get("title", path.stem),
                    "created_at": data.get("created_at"),
                    "updated_at": data.get("updated_at"),
                    "message_count": len(data.get("messages", [])),
                    "task_progress": data.get("task_progress", {}),
                    "subagent_tasks": data.get("subagent_tasks", []),
                    "subagent_notifications": data.get("subagent_notifications", []),
                }
            )
        return sessions

    def load_session(self, session_id: str) -> dict[str, Any] | None:
        path = self.session_path(session_id)
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            corrupt_path = path.with_suffix(f".corrupt-{uuid4().hex[:8]}.json")
            try:
                path.replace(corrupt_path)
            except Exception:
                pass
            return None
        if not self._looks_like_chat_session(data):
            return None
        if "task_progress" not in data:
            data["task_progress"] = self.default_progress()
        if "messages" not in data:
            data["messages"] = []
        if "subagent_tasks" not in data:
            data["subagent_tasks"] = []
        if "subagent_notifications" not in data:
            data["subagent_notifications"] = []
        return data

    def default_progress(self) -> dict[str, Any]:
        return {
            "status": "idle",
            "active_tool": None,
            "message": "",
            "elapsed_seconds": 0,
            "last_debug_stage": "",
            "script_stages": [],
            "task_items": [],
            "pitfalls": [],
            "updated_at": _now_iso(),
        }

    def create_or_get_session(self, session_id: str, first_message: str = "") -> dict[str, Any]:
        existing = self.load_session(session_id)
        if existing is not None:
            return existing
        session = {
            "session_id": _slugify_session_id(session_id),
            "title": _make_title(first_message),
            "created_at": _now_iso(),
            "updated_at": _now_iso(),
            "messages": [],
            "task_progress": self.default_progress(),
            "subagent_tasks": [],
            "subagent_notifications": [],
        }
        self.save_session(session)
        return session

    def save_session(self, session: dict[str, Any]) -> None:
        with _SESSION_FILE_LOCK:
            session["updated_at"] = _now_iso()
            path = self.session_path(session["session_id"])
            payload = dict(session)
            messages = payload.get("messages", [])
            if isinstance(messages, list):
                payload["messages"] = [
                    _compact_message(message) if isinstance(message, dict) else message
                    for message in messages
                ]
            tmp_path = path.with_suffix(f".tmp-{uuid4().hex}.json")
            tmp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            try:
                tmp_path.replace(path)
            except PermissionError:
                # Windows can transiently lock the target during rapid session polling.
                # Fall back to remove-then-replace, then direct write as a last resort.
                try:
                    if path.exists():
                        path.unlink()
                    tmp_path.replace(path)
                except PermissionError:
                    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
                    try:
                        tmp_path.unlink()
                    except Exception:
                        pass

    def record_execution_summary(self, session_id: str, summary: dict[str, Any]) -> dict[str, Any]:
        session = self.create_or_get_session(session_id)
        progress = session.setdefault("task_progress", self.default_progress())
        progress["execution_summary"] = {
            **summary,
            "updated_at": _now_iso(),
        }
        stages = progress.setdefault("script_stages", [])
        stages.append(
            {
                "stage_name": "execution_summary",
                "status": str(summary.get("status") or "completed"),
                "summary": str(summary.get("headline") or summary.get("message") or "Execution summary saved."),
                "artifact_path": ", ".join(str(path) for path in summary.get("saved_or_downloaded", [])[:5])
                if isinstance(summary.get("saved_or_downloaded"), list)
                else "",
                "created_at": _now_iso(),
            }
        )
        progress["script_stages"] = stages[-100:]
        tasks = progress.setdefault("task_items", [])
        tasks = [item for item in tasks if item.get("task_id") != "latest-execution-summary"]
        tasks.append(
            {
                "task_id": "latest-execution-summary",
                "title": str(summary.get("headline") or "Latest execution summary"),
                "status": str(summary.get("status") or "completed"),
                "details": str(summary.get("final_response_preview") or ""),
                "artifact_path": ", ".join(str(path) for path in summary.get("saved_or_downloaded", [])[:5])
                if isinstance(summary.get("saved_or_downloaded"), list)
                else "",
                "created_at": _now_iso(),
                "updated_at": _now_iso(),
            }
        )
        progress["task_items"] = tasks[-200:]
        self.save_session(session)
        return session

    def rename_session(self, session_id: str, title: str) -> dict[str, Any] | None:
        session = self.load_session(session_id)
        if session is None:
            return None
        normalized_title = re.sub(r"\s+", " ", (title or "").strip())
        if normalized_title:
            session["title"] = normalized_title[:MAX_SESSION_TITLE_LENGTH]
            self.save_session(session)
        return session

    def delete_session(self, session_id: str) -> bool:
        path = self.session_path(session_id)
        if not path.exists():
            return False
        path.unlink()
        return True

    def append_message(
        self,
        session_id: str,
        role: str,
        content: str,
        tools: list[dict[str, Any]] | None = None,
        usage: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        session = self.create_or_get_session(session_id, first_message=content if role == "user" else "")
        if role == "user" and not session.get("messages"):
            session["title"] = _make_title(content)
        session.setdefault("messages", []).append(
            {
                "role": role,
                "content": content,
                "tools": tools or [],
                "usage": usage or {},
                "created_at": _now_iso(),
            }
        )
        self.save_session(session)
        return session

    def replace_last_assistant_message(
        self,
        session_id: str,
        content: str,
        tools: list[dict[str, Any]] | None = None,
        usage: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        session = self.create_or_get_session(session_id)
        messages = session.setdefault("messages", [])
        if messages and messages[-1].get("role") == "assistant":
            messages[-1]["content"] = content
            messages[-1]["tools"] = tools or []
            messages[-1]["usage"] = usage or {}
            messages[-1]["updated_at"] = _now_iso()
        else:
            messages.append(
                {
                    "role": "assistant",
                    "content": content,
                    "tools": tools or [],
                    "usage": usage or {},
                    "created_at": _now_iso(),
                }
            )
        self.save_session(session)
        return session

    def update_progress(self, session_id: str, **updates: Any) -> dict[str, Any]:
        session = self.create_or_get_session(session_id)
        progress = session.setdefault("task_progress", self.default_progress())
        progress.update(updates)
        if "script_stages" not in progress or not isinstance(progress.get("script_stages"), list):
            progress["script_stages"] = []
        if "task_items" not in progress or not isinstance(progress.get("task_items"), list):
            progress["task_items"] = []
        if "pitfalls" not in progress or not isinstance(progress.get("pitfalls"), list):
            progress["pitfalls"] = []
        progress["updated_at"] = _now_iso()
        self.save_session(session)
        return session

    def add_task_item(self, session_id: str, task: dict[str, Any]) -> dict[str, Any]:
        session = self.create_or_get_session(session_id)
        progress = session.setdefault("task_progress", self.default_progress())
        tasks = progress.setdefault("task_items", [])
        task_id = str(task.get("task_id") or "").strip() or f"task-{uuid4().hex[:8]}"
        normalized = {
            "task_id": task_id,
            "title": str(task.get("title") or "").strip(),
            "status": str(task.get("status") or "pending").strip(),
            "details": str(task.get("details") or "").strip(),
            "artifact_path": str(task.get("artifact_path") or "").strip(),
            "created_at": _now_iso(),
            "updated_at": _now_iso(),
        }
        tasks = [item for item in tasks if item.get("task_id") != task_id]
        tasks.append(normalized)
        progress["task_items"] = tasks[-200:]
        progress["updated_at"] = _now_iso()
        self.save_session(session)
        return session

    def update_task_item(self, session_id: str, task_id: str, **updates: Any) -> dict[str, Any]:
        session = self.create_or_get_session(session_id)
        progress = session.setdefault("task_progress", self.default_progress())
        tasks = progress.setdefault("task_items", [])
        normalized_id = str(task_id or "").strip()
        updated = False
        for index, item in enumerate(tasks):
            if item.get("task_id") != normalized_id:
                continue
            next_item = dict(item)
            for key in ("title", "status", "details", "artifact_path"):
                if key in updates and updates[key] is not None:
                    next_item[key] = str(updates[key]).strip()
            next_item["updated_at"] = _now_iso()
            tasks[index] = next_item
            updated = True
            break
        if not updated:
            tasks.append(
                {
                    "task_id": normalized_id or f"task-{uuid4().hex[:8]}",
                    "title": str(updates.get("title") or "").strip(),
                    "status": str(updates.get("status") or "pending").strip(),
                    "details": str(updates.get("details") or "").strip(),
                    "artifact_path": str(updates.get("artifact_path") or "").strip(),
                    "created_at": _now_iso(),
                    "updated_at": _now_iso(),
                }
            )
        progress["task_items"] = tasks[-200:]
        progress["updated_at"] = _now_iso()
        self.save_session(session)
        return session

    def add_pitfall(self, session_id: str, pitfall: dict[str, Any]) -> dict[str, Any]:
        session = self.create_or_get_session(session_id)
        progress = session.setdefault("task_progress", self.default_progress())
        pitfalls = progress.setdefault("pitfalls", [])
        pitfalls.append(
            {
                "summary": str(pitfall.get("summary") or "").strip(),
                "impact": str(pitfall.get("impact") or "").strip(),
                "resolution": str(pitfall.get("resolution") or "").strip(),
                "artifact_path": str(pitfall.get("artifact_path") or "").strip(),
                "created_at": _now_iso(),
            }
        )
        progress["pitfalls"] = pitfalls[-200:]
        progress["updated_at"] = _now_iso()
        self.save_session(session)
        return session

    def add_script_stage(self, session_id: str, stage: dict[str, Any]) -> dict[str, Any]:
        session = self.create_or_get_session(session_id)
        progress = session.setdefault("task_progress", self.default_progress())
        stages = progress.setdefault("script_stages", [])
        stages.append({**stage, "created_at": _now_iso()})
        progress["script_stages"] = stages[-100:]
        progress["updated_at"] = _now_iso()
        self.save_session(session)
        return session

    def add_subagent_task(self, session_id: str, task: dict[str, Any]) -> dict[str, Any]:
        session = self.create_or_get_session(session_id)
        tasks = session.setdefault("subagent_tasks", [])
        tasks = [item for item in tasks if item.get("agent_id") != task.get("agent_id")]
        tasks.insert(0, task)
        session["subagent_tasks"] = tasks[:50]
        self.save_session(session)
        return session

    def update_subagent_task(self, session_id: str, task: dict[str, Any]) -> dict[str, Any]:
        session = self.create_or_get_session(session_id)
        tasks = session.setdefault("subagent_tasks", [])
        updated = False
        for index, item in enumerate(tasks):
            if item.get("agent_id") == task.get("agent_id"):
                tasks[index] = {**item, **task}
                updated = True
                break
        if not updated:
            tasks.insert(0, task)
        session["subagent_tasks"] = tasks[:50]
        self.save_session(session)
        return session

    def add_subagent_notification(self, session_id: str, notification: dict[str, Any]) -> dict[str, Any]:
        session = self.create_or_get_session(session_id)
        notifications = session.setdefault("subagent_notifications", [])
        notifications.insert(0, notification)
        session["subagent_notifications"] = notifications[:200]
        self.save_session(session)
        return session

    def get_history(self, session_id: str) -> list[dict[str, str]]:
        session = self.load_session(session_id)
        if session is None:
            return []
        history: list[dict[str, str]] = []
        for item in session.get("messages", []):
            role = item.get("role", "")
            content = item.get("content", "")
            if role in {"user", "assistant"} and isinstance(content, str):
                history.append({"role": role, "content": content})
        progress = session.get("task_progress", {})
        summary = progress.get("execution_summary") if isinstance(progress, dict) else None
        if isinstance(summary, dict):
            memory_lines = [
                "Session memory from previous execution:",
                f"- Status: {summary.get('status', '')}",
                f"- Request: {summary.get('user_request', '')}",
                f"- Recent changes: {'; '.join(summary.get('modifications_or_adjustments') or [])}",
                f"- Saved/downloaded: {'; '.join(summary.get('saved_or_downloaded') or [])}",
                f"- Successful methods: {'; '.join(summary.get('successful_methods') or [])}",
            ]
            if summary.get("failure_reason"):
                memory_lines.append(f"- Previous failure reason: {summary.get('failure_reason')}")
            history.append({"role": "assistant", "content": "\n".join(memory_lines)})
        return history
