from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

SESSION_DIR_NAME = "sessionss"
MAX_SESSION_TITLE_LENGTH = 40


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


class SessionStore:
    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        self.sessions_dir = self.root / SESSION_DIR_NAME
        self.sessions_dir.mkdir(parents=True, exist_ok=True)

    def session_path(self, session_id: str) -> Path:
        return self.sessions_dir / f"{_slugify_session_id(session_id)}.json"

    def list_sessions(self) -> list[dict[str, Any]]:
        sessions: list[dict[str, Any]] = []
        for path in sorted(
            self.sessions_dir.glob("*.json"),
            key=lambda item: item.stat().st_mtime,
            reverse=True,
        ):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                continue
            sessions.append(
                {
                    "session_id": data.get("session_id", path.stem),
                    "title": data.get("title", path.stem),
                    "created_at": data.get("created_at"),
                    "updated_at": data.get("updated_at"),
                    "message_count": len(data.get("messages", [])),
                    "task_progress": data.get("task_progress", {}),
                }
            )
        return sessions

    def load_session(self, session_id: str) -> dict[str, Any] | None:
        path = self.session_path(session_id)
        if not path.exists():
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
        if "task_progress" not in data:
            data["task_progress"] = self.default_progress()
        if "messages" not in data:
            data["messages"] = []
        return data

    def default_progress(self) -> dict[str, Any]:
        return {
            "status": "idle",
            "active_tool": None,
            "message": "",
            "elapsed_seconds": 0,
            "last_debug_stage": "",
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
        }
        self.save_session(session)
        return session

    def save_session(self, session: dict[str, Any]) -> None:
        session["updated_at"] = _now_iso()
        path = self.session_path(session["session_id"])
        path.write_text(json.dumps(session, ensure_ascii=False, indent=2), encoding="utf-8")

    def append_message(
        self,
        session_id: str,
        role: str,
        content: str,
        tools: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        session = self.create_or_get_session(session_id, first_message=content if role == "user" else "")
        if role == "user" and not session.get("messages"):
            session["title"] = _make_title(content)
        session.setdefault("messages", []).append(
            {
                "role": role,
                "content": content,
                "tools": tools or [],
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
    ) -> dict[str, Any]:
        session = self.create_or_get_session(session_id)
        messages = session.setdefault("messages", [])
        if messages and messages[-1].get("role") == "assistant":
            messages[-1]["content"] = content
            messages[-1]["tools"] = tools or []
            messages[-1]["updated_at"] = _now_iso()
        else:
            messages.append(
                {
                    "role": "assistant",
                    "content": content,
                    "tools": tools or [],
                    "created_at": _now_iso(),
                }
            )
        self.save_session(session)
        return session

    def update_progress(self, session_id: str, **updates: Any) -> dict[str, Any]:
        session = self.create_or_get_session(session_id)
        progress = session.setdefault("task_progress", self.default_progress())
        progress.update(updates)
        progress["updated_at"] = _now_iso()
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
        return history
