from __future__ import annotations

import json
import re
import shutil
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
MAX_STORED_TASK_OUTPUT_CHARS = 12_000
MAX_TASK_OUTPUT_EVENTS = 40
MAX_TOOL_RESULT_CACHE_ENTRIES = 200
TOOL_EVENT_SCHEMA_VERSION = 1
RECENT_TOOL_HISTORY_TURNS = 3
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


def _coerce_optional_int(value: Any) -> Any:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str):
        text = value.strip()
        if re.fullmatch(r"[+-]?\d+", text):
            return int(text)
    return value


def _compact_message(message: dict[str, Any]) -> dict[str, Any]:
    item = dict(message)
    item["content"] = _compact_text(item.get("content"), MAX_STORED_MESSAGE_CHARS)
    usage = item.get("usage")
    item["usage"] = usage if isinstance(usage, dict) else {}
    tools = item.get("tools", [])
    if isinstance(tools, list):
        # Tool transcripts are the lossless source for native history rehydration.
        # Compact operational caches separately, but never compact this transcript.
        item["tools"] = [dict(tool) if isinstance(tool, dict) else tool for tool in tools]
    else:
        item["tools"] = []
    return item


def _event_type(tool: dict[str, Any]) -> str:
    value = str(tool.get("type") or tool.get("event_type") or "").strip()
    if value in {"tool_call", "tool_result"}:
        return value
    return ""


def _legacy_tool_call_id(turn_index: int, tool_name: str, ordinal: int) -> str:
    safe_name = re.sub(r"[^a-zA-Z0-9_.-]+", "-", tool_name).strip("-") or "tool"
    return f"legacy-{turn_index}-{safe_name}-{ordinal}"


def normalize_tool_events(tools: Any, turn_index: int) -> list[dict[str, Any]]:
    """Return ordered, non-mutating tool events suitable for prompt rehydration."""
    if not isinstance(tools, list):
        return []

    pending_by_name: dict[str, list[str]] = {}
    available_call_ids: set[str] = set()
    legacy_call_counts: dict[str, int] = {}
    legacy_result_counts: dict[str, int] = {}
    normalized: list[dict[str, Any]] = []

    indexed_tools = [
        (index, tool)
        for index, tool in enumerate(tools)
        if isinstance(tool, dict) and _event_type(tool)
    ]
    indexed_tools.sort(
        key=lambda pair: (
            pair[1].get("sequence") if isinstance(pair[1].get("sequence"), int) else pair[0],
            pair[0],
        )
    )

    for fallback_sequence, (_, source) in enumerate(indexed_tools):
        event_type = _event_type(source)
        name = str(source.get("name") or source.get("tool_name") or "tool").strip() or "tool"
        sequence = source.get("sequence") if isinstance(source.get("sequence"), int) else fallback_sequence
        raw_id = source.get("tool_call_id") or source.get("id")
        tool_call_id = str(raw_id).strip() if raw_id is not None else ""
        is_legacy = not tool_call_id

        if event_type == "tool_call":
            if not tool_call_id:
                legacy_call_counts[name] = legacy_call_counts.get(name, 0) + 1
                tool_call_id = _legacy_tool_call_id(turn_index, name, legacy_call_counts[name])
            pending_by_name.setdefault(name, []).append(tool_call_id)
            available_call_ids.add(tool_call_id)
            normalized.append(
                {
                    "schema_version": TOOL_EVENT_SCHEMA_VERSION,
                    "type": "tool_call",
                    "sequence": sequence,
                    "tool_call_id": tool_call_id,
                    "name": name,
                    "arguments": source.get("arguments"),
                    "legacy": is_legacy,
                }
            )
            continue

        pending = pending_by_name.setdefault(name, [])
        matched_call = False
        if not tool_call_id and pending:
            tool_call_id = pending.pop(0)
            matched_call = True
        elif tool_call_id and tool_call_id in pending:
            pending.remove(tool_call_id)
            matched_call = True

        legacy_unmatched = False
        malformed = False
        if not tool_call_id:
            legacy_result_counts[name] = legacy_result_counts.get(name, 0) + 1
            tool_call_id = _legacy_tool_call_id(turn_index, f"{name}-unmatched", legacy_result_counts[name])
            legacy_unmatched = True
        elif not matched_call and tool_call_id not in available_call_ids:
            malformed = True
        if matched_call:
            available_call_ids.discard(tool_call_id)

        normalized.append(
            {
                "schema_version": TOOL_EVENT_SCHEMA_VERSION,
                "type": "tool_result",
                "sequence": sequence,
                "tool_call_id": tool_call_id,
                "name": name,
                "content": source.get("content"),
                "legacy": is_legacy,
                "legacy_unmatched": legacy_unmatched,
                "malformed": malformed,
            }
        )
    return normalized


def _stable_history_projection_value(value: Any) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )
    except Exception:
        return str(value)


def deduplicate_tool_history_events(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Collapse identical matched call/result pairs for prompt projection only."""
    calls_by_id: dict[str, dict[str, Any]] = {}
    duplicate_call_ids: set[str] = set()
    seen_pairs: set[tuple[str, str, str]] = set()

    for event in events:
        tool_call_id = str(event.get("tool_call_id") or "")
        if not tool_call_id:
            continue
        if event.get("type") == "tool_call":
            calls_by_id.setdefault(tool_call_id, event)
            continue
        if event.get("type") != "tool_result" or event.get("legacy_unmatched") or event.get("malformed"):
            continue
        call = calls_by_id.get(tool_call_id)
        if not call:
            continue
        key = (
            str(call.get("name") or event.get("name") or "tool"),
            _stable_history_projection_value(call.get("arguments")),
            _stable_history_projection_value(event.get("content")),
        )
        if key in seen_pairs:
            duplicate_call_ids.add(tool_call_id)
        else:
            seen_pairs.add(key)

    if not duplicate_call_ids:
        return events

    deduped: list[dict[str, Any]] = []
    for event in events:
        tool_call_id = str(event.get("tool_call_id") or "")
        if tool_call_id not in duplicate_call_ids:
            deduped.append(event)
            continue
        if event.get("type") == "tool_call":
            continue
        if event.get("type") == "tool_result" and not event.get("legacy_unmatched") and not event.get("malformed"):
            continue
        deduped.append(event)
    return deduped


def _append_tool_history_entries(history: list[dict[str, Any]], events: list[dict[str, Any]]) -> None:
    index = 0
    while index < len(events):
        event = events[index]
        if event["type"] == "tool_call":
            batch: list[dict[str, Any]] = []
            first_sequence = event["sequence"]
            while index < len(events) and events[index]["type"] == "tool_call":
                call = events[index]
                batch.append(
                    {
                        "name": call["name"],
                        "args": call.get("arguments"),
                        "id": call["tool_call_id"],
                        "type": "tool_call",
                    }
                )
                index += 1
            history.append(
                {
                    "role": "assistant_tool_calls",
                    "sequence": first_sequence,
                    "tool_calls": batch,
                    "protected_tool_history": True,
                }
            )
            continue

        if event.get("legacy_unmatched"):
            history.append(
                {
                    "role": "legacy_tool_result",
                    "sequence": event["sequence"],
                    "name": event["name"],
                    "tool_call_id": event["tool_call_id"],
                    "content": event.get("content"),
                    "legacy_unmatched": True,
                    "protected_tool_history": True,
                }
            )
        elif event.get("malformed"):
            history.append(
                {
                    "role": "malformed_tool_result",
                    "sequence": event["sequence"],
                    "name": event["name"],
                    "tool_call_id": event["tool_call_id"],
                    "content": event.get("content"),
                    "protected_tool_history": True,
                }
            )
        else:
            history.append(
                {
                    "role": "tool",
                    "sequence": event["sequence"],
                    "name": event["name"],
                    "tool_call_id": event["tool_call_id"],
                    "content": event.get("content"),
                    "protected_tool_history": True,
                }
            )
        index += 1


class SessionStore:
    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        session_dir = str(get_runtime_value("paths", "session_dir", "sessionss") or "sessionss")
        self.sessions_dir = self.root / session_dir
        self.sessions_dir.mkdir(parents=True, exist_ok=True)

    def session_path(self, session_id: str) -> Path:
        return self.session_dir_path(session_id) / "session.json"

    def legacy_session_path(self, session_id: str) -> Path:
        return self.sessions_dir / f"{_slugify_session_id(session_id)}.json"

    def session_dir_path(self, session_id: str) -> Path:
        return self.sessions_dir / _slugify_session_id(session_id)

    def task_journal_path(self, session_id: str) -> Path:
        return self.session_dir_path(session_id) / "task_plan.jsonl"

    def task_plan_path(self, session_id: str) -> Path:
        return self.session_dir_path(session_id) / "task_plan.json"

    def legacy_task_journal_path(self, session_id: str) -> Path:
        return self.sessions_dir / f"{_slugify_session_id(session_id)}.task_plan.jsonl"

    def tool_events_path(self, session_id: str) -> Path:
        return self.session_dir_path(session_id) / "tool_events.jsonl"

    def legacy_tool_events_path(self, session_id: str) -> Path:
        return self.sessions_dir / f"{_slugify_session_id(session_id)}.tool_events.jsonl"

    def tool_result_cache_path(self, session_id: str) -> Path:
        return self.session_dir_path(session_id) / "tool_result_cache.json"

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

    def _migrate_legacy_sidecars(self, session_id: str) -> None:
        for legacy_path, folder_path in (
            (self.legacy_task_journal_path(session_id), self.task_journal_path(session_id)),
            (self.legacy_tool_events_path(session_id), self.tool_events_path(session_id)),
        ):
            if not legacy_path.exists() or folder_path.exists():
                continue
            try:
                folder_path.parent.mkdir(parents=True, exist_ok=True)
                folder_path.write_text(legacy_path.read_text(encoding="utf-8"), encoding="utf-8")
            except Exception:
                continue

    def list_sessions(self) -> list[dict[str, Any]]:
        sessions: list[dict[str, Any]] = []
        seen_session_ids: set[str] = set()
        candidate_paths = [
            path / "session.json"
            for path in self.sessions_dir.iterdir()
            if path.is_dir() and (path / "session.json").exists()
        ]
        candidate_paths.extend(self.sessions_dir.glob("*.json"))
        for path in sorted(candidate_paths, key=lambda item: item.stat().st_mtime, reverse=True):
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
        loaded_from_legacy = False
        if not path.exists():
            legacy_path = self.legacy_session_path(session_id)
            if legacy_path.exists():
                path = legacy_path
                loaded_from_legacy = True
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
        if loaded_from_legacy:
            self.save_session(data)
            self._migrate_legacy_sidecars(data.get("session_id") or session_id)
        return data

    def default_progress(self) -> dict[str, Any]:
        return {
            "status": "idle",
            "active_tool": None,
            "message": "",
            "elapsed_seconds": 0,
            "last_debug_stage": "",
            "task_plan_ref": {"path": "task_plan.json", "updated_at": ""},
            "task_outputs": {},
            "script_stages": [],
            "task_items": [],
            "pitfalls": [],
            "primary_result": None,
            "stage_results": {},
            "artifacts": [],
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
            path.parent.mkdir(parents=True, exist_ok=True)
            for stale_tmp in path.parent.glob("session.tmp-*.json"):
                try:
                    stale_tmp.unlink()
                except Exception:
                    pass
            payload = dict(session)
            progress = payload.get("task_progress")
            if isinstance(progress, dict):
                progress = dict(progress)
                progress.pop("task_plan", None)
                progress.setdefault("task_plan_ref", {"path": "task_plan.json", "updated_at": ""})
                payload["task_progress"] = progress
            messages = payload.get("messages", [])
            if isinstance(messages, list):
                payload["messages"] = [
                    _compact_message(message) if isinstance(message, dict) else message
                    for message in messages
                ]
            # Direct write avoids accumulating locked session.tmp-* files on Windows.
            path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            for stale_tmp in path.parent.glob("session.tmp-*.json"):
                try:
                    stale_tmp.unlink()
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
        deleted = False
        folder = self.session_dir_path(session_id)
        if folder.exists() and folder.is_dir():
            shutil.rmtree(folder)
            deleted = True
        for path in (
            self.legacy_session_path(session_id),
            self.legacy_task_journal_path(session_id),
            self.legacy_tool_events_path(session_id),
        ):
            if path.exists():
                path.unlink()
                deleted = True
        return deleted

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
        if "task_plan" not in progress or not isinstance(progress.get("task_plan"), dict):
            progress["task_plan"] = {"todos": [], "updated_at": _now_iso()}
        if "task_outputs" not in progress or not isinstance(progress.get("task_outputs"), dict):
            progress["task_outputs"] = {}
        if "script_stages" not in progress or not isinstance(progress.get("script_stages"), list):
            progress["script_stages"] = []
        if "task_items" not in progress or not isinstance(progress.get("task_items"), list):
            progress["task_items"] = []
        if "pitfalls" not in progress or not isinstance(progress.get("pitfalls"), list):
            progress["pitfalls"] = []
        if "stage_results" not in progress or not isinstance(progress.get("stage_results"), dict):
            progress["stage_results"] = {}
        if "artifacts" not in progress or not isinstance(progress.get("artifacts"), list):
            progress["artifacts"] = []
        progress["updated_at"] = _now_iso()
        self.save_session(session)
        return session

    def record_primary_result(self, session_id: str, result: dict[str, Any]) -> dict[str, Any]:
        session = self.create_or_get_session(session_id)
        progress = session.setdefault("task_progress", self.default_progress())
        normalized = {
            "type": str(result.get("type") or "artifact"),
            "title": str(result.get("title") or result.get("summary") or "Result"),
            "status": str(result.get("status") or "created"),
            "url": str(result.get("url") or ""),
            "path": str(result.get("path") or ""),
            "token": str(result.get("token") or result.get("base_token") or ""),
            "table_id": str(result.get("table_id") or ""),
            "record_count": _coerce_optional_int(result.get("record_count")),
            "verified": bool(result.get("verified", False)),
            "summary": str(result.get("summary") or ""),
            "source_tool": str(result.get("source_tool") or ""),
            "updated_at": _now_iso(),
        }
        progress["primary_result"] = normalized
        self.register_artifact(
            session_id,
            {
                **normalized,
                "role": "primary",
            },
            save=False,
            session=session,
        )
        progress["updated_at"] = _now_iso()
        self.save_session(session)
        return session

    def record_stage_result(self, session_id: str, stage_name: str, result: dict[str, Any]) -> dict[str, Any]:
        session = self.create_or_get_session(session_id)
        progress = session.setdefault("task_progress", self.default_progress())
        stage_results = progress.setdefault("stage_results", {})
        key = str(stage_name or result.get("stage_name") or "stage").strip() or "stage"
        stage_results[key] = {
            "stage_name": key,
            "status": str(result.get("status") or "completed"),
            "result_ref": str(result.get("result_ref") or result.get("path") or ""),
            "summary": str(result.get("summary") or ""),
            "item_count": result.get("item_count"),
            "verified": bool(result.get("verified", False)),
            "source_tool": str(result.get("source_tool") or ""),
            "updated_at": _now_iso(),
        }
        progress["stage_results"] = stage_results
        progress["updated_at"] = _now_iso()
        self.save_session(session)
        return session

    def register_artifact(
        self,
        session_id: str,
        artifact: dict[str, Any],
        *,
        save: bool = True,
        session: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        session = session or self.create_or_get_session(session_id)
        progress = session.setdefault("task_progress", self.default_progress())
        artifacts = progress.setdefault("artifacts", [])
        role = str(artifact.get("role") or "intermediate")
        normalized = {
            "artifact_id": str(artifact.get("artifact_id") or f"artifact-{uuid4().hex[:8]}"),
            "role": role,
            "type": str(artifact.get("type") or "artifact"),
            "title": str(artifact.get("title") or artifact.get("summary") or ""),
            "url": str(artifact.get("url") or ""),
            "path": str(artifact.get("path") or ""),
            "token": str(artifact.get("token") or artifact.get("base_token") or ""),
            "table_id": str(artifact.get("table_id") or ""),
            "record_count": _coerce_optional_int(artifact.get("record_count")),
            "status": str(artifact.get("status") or ""),
            "verified": bool(artifact.get("verified", False)),
            "summary": str(artifact.get("summary") or ""),
            "created_at": artifact.get("created_at") or _now_iso(),
            "updated_at": _now_iso(),
        }
        identity = (
            normalized["role"],
            normalized["type"],
            normalized["url"],
            normalized["path"],
            normalized["token"],
            normalized["table_id"],
        )
        deduped: list[dict[str, Any]] = []
        replaced = False
        for item in artifacts if isinstance(artifacts, list) else []:
            item_identity = (
                item.get("role", ""),
                item.get("type", ""),
                item.get("url", ""),
                item.get("path", ""),
                item.get("token", ""),
                item.get("table_id", ""),
            )
            if item_identity == identity:
                merged = {**item, **normalized}
                if normalized.get("record_count") is None and item.get("record_count") is not None:
                    merged["record_count"] = item.get("record_count")
                deduped.append(merged)
                replaced = True
            else:
                deduped.append(item)
        if not replaced:
            deduped.append(normalized)
        progress["artifacts"] = deduped[-100:]
        progress["updated_at"] = _now_iso()
        if save:
            self.save_session(session)
        return session

    def get_resume_context(self, session_id: str) -> dict[str, Any]:
        session = self.ensure_task_plan_recovered(session_id) or self.load_session(session_id)
        if session is None:
            return {}
        progress = session.get("task_progress", {})
        plan = self.load_task_plan(session_id)
        return {
            "primary_result": progress.get("primary_result"),
            "stage_results": progress.get("stage_results") if isinstance(progress.get("stage_results"), dict) else {},
            "artifacts": progress.get("artifacts") if isinstance(progress.get("artifacts"), list) else [],
            "unfinished_todos": [
                todo for todo in (plan.get("todos") or [])
                if isinstance(todo, dict) and str(todo.get("status") or "pending") != "completed"
            ],
            "completed_todos": [
                todo for todo in (plan.get("todos") or [])
                if isinstance(todo, dict) and str(todo.get("status") or "") == "completed"
            ],
        }

    def set_task_plan(
        self,
        session_id: str,
        todos: list[dict[str, Any]],
        source: str = "auto",
        reason: str = "",
    ) -> dict[str, Any]:
        """Save a task plan snapshot.

        First call: the model's plan is accepted as the authoritative baseline.
        Subsequent calls: task IDs, order, content, and activeForm are LOCKED.
        Only status, details, and result_ref may be updated. Completed todos
        are never downgraded. New/removed/renamed task IDs are rejected with
        a warning and the original IDs are preserved.
        """
        session = self.create_or_get_session(session_id)
        progress = session.setdefault("task_progress", self.default_progress())
        now = _now_iso()

        # Normalize incoming todos
        incoming: list[dict[str, Any]] = []
        for index, todo in enumerate(todos, 1):
            task_id = str(todo.get("task_id") or f"step-{index}").strip()
            incoming.append(
                {
                    "task_id": task_id,
                    "content": str(todo.get("content") or task_id).strip(),
                    "activeForm": str(todo.get("activeForm") or todo.get("content") or task_id).strip(),
                    "status": str(todo.get("status") or "pending").strip(),
                    "details": str(todo.get("details") or "").strip(),
                    "result_ref": str(todo.get("result_ref") or "").strip(),
                    "created_at": str(todo.get("created_at") or now),
                    "updated_at": now,
                }
            )

        existing_plan = progress.get("task_plan") if isinstance(progress.get("task_plan"), dict) else {}
        existing_todos: list[dict[str, Any]] = existing_plan.get("todos", []) if isinstance(existing_plan, dict) else []
        existing_todos = [item for item in existing_todos if isinstance(item, dict)]
        warnings: list[str] = []

        # --- First plan: accept as-is (authoritative baseline) ---
        if not existing_todos:
            progress["task_plan"] = {
                "todos": incoming,
                "source": source,
                "reason": reason,
                "warnings": [],
                "created_at": now,
                "updated_at": now,
            }
            progress["updated_at"] = now
            self.save_session(session)
            self.append_task_journal(
                session_id,
                {
                    "event": "task_plan_snapshot",
                    "source": source,
                    "reason": reason,
                    "warnings": [],
                    "todos": incoming,
                },
            )
            return session

        # --- Subsequent calls: task IDs and content are LOCKED ---
        existing_by_id: dict[str, dict[str, Any]] = {
            str(item.get("task_id") or ""): item for item in existing_todos
        }
        incoming_by_id: dict[str, dict[str, Any]] = {
            str(item.get("task_id") or ""): item for item in incoming
        }
        existing_ids = list(existing_by_id.keys())
        incoming_ids = list(incoming_by_id.keys())

        # Detect structural changes
        new_ids = [tid for tid in incoming_ids if tid not in existing_by_id]
        removed_ids = [tid for tid in existing_ids if tid not in incoming_by_id]
        reordered = incoming_ids != existing_ids

        if new_ids:
            warnings.append(
                f"Rejected new task IDs {new_ids} — plan is locked after first snapshot. "
                f"Existing IDs: {existing_ids}"
            )
        if removed_ids:
            warnings.append(
                f"Preserved removed task IDs {removed_ids} — plan is locked after first snapshot."
            )
        if reordered and not new_ids and not removed_ids:
            warnings.append(
                "Ignored reordering — plan order is locked after first snapshot."
            )

        for existing in existing_todos:
            task_id = str(existing.get("task_id") or "")
            incoming_item = incoming_by_id.get(task_id)
            if incoming_item is None:
                continue
            for field in ("content", "activeForm", "status", "details", "result_ref"):
                incoming_value = str(incoming_item.get(field) or "").strip()
                existing_value = str(existing.get(field) or "").strip()
                if incoming_value and incoming_value != existing_value:
                    warnings.append(
                        f"Ignored `{field}` change for `{task_id}` because todo snapshot is locked after first call."
                    )

        progress["task_plan"] = {
            "todos": existing_todos,
            "source": existing_plan.get("source") if isinstance(existing_plan, dict) else source,
            "reason": existing_plan.get("reason") if isinstance(existing_plan, dict) else reason,
            "warnings": warnings,
            "created_at": existing_plan.get("created_at") or now if isinstance(existing_plan, dict) else now,
            "updated_at": existing_plan.get("updated_at") or now if isinstance(existing_plan, dict) else now,
        }
        self.save_session(session)
        self.append_task_journal(
            session_id,
            {
                "event": "task_plan_snapshot_ignored",
                "source": source,
                "reason": reason,
                "warnings": warnings,
                "incoming_todos": incoming,
                "todos": existing_todos,
            },
        )
        return session

        # Build merged todos: preserve original IDs/order/content, only apply status updates
        final_todos: list[dict[str, Any]] = []
        for existing in existing_todos:
            task_id = str(existing.get("task_id") or "")
            incoming_item = incoming_by_id.get(task_id)
            merged = dict(existing)
            merged["updated_at"] = now

            if incoming_item is None:
                # Task ID removed by model — keep it
                final_todos.append(merged)
                continue

            # --- Content / activeForm are LOCKED ---
            incoming_content = str(incoming_item.get("content") or "").strip()
            existing_content = str(existing.get("content") or "").strip()
            if incoming_content and incoming_content != existing_content:
                warnings.append(
                    f"Ignored content change for `{task_id}` — content is locked after first snapshot."
                )

            incoming_active = str(incoming_item.get("activeForm") or "").strip()
            existing_active = str(existing.get("activeForm") or "").strip()
            if incoming_active and incoming_active != existing_active:
                warnings.append(
                    f"Ignored activeForm change for `{task_id}` — activeForm is locked after first snapshot."
                )

            # --- Status: only update forward (pending → in_progress → completed) ---
            incoming_status = str(incoming_item.get("status") or existing.get("status") or "pending").strip()
            existing_status = str(existing.get("status") or "pending").strip()

            if existing_status == "completed" and incoming_status != "completed":
                warnings.append(
                    f"Preserved completed status for `{task_id}` (model sent `{incoming_status}`)."
                )
                # Keep completed
            elif incoming_status != existing_status:
                merged["status"] = incoming_status

            # --- Details / result_ref: allow updates ---
            incoming_details = str(incoming_item.get("details") or "").strip()
            if incoming_details:
                merged["details"] = incoming_details[:1200]
            incoming_ref = str(incoming_item.get("result_ref") or "").strip()
            if incoming_ref:
                merged["result_ref"] = incoming_ref[:500]

            final_todos.append(merged)

        # Also preserve any removed todos at their original position
        # (they were already kept above via existing_todos iteration)

        progress["task_plan"] = {
            "todos": final_todos,
            "source": source,
            "reason": reason,
            "warnings": warnings,
            "created_at": existing_plan.get("created_at") or now if isinstance(existing_plan, dict) else now,
            "updated_at": now,
        }
        progress["updated_at"] = now
        self.save_session(session)
        self.append_task_journal(
            session_id,
            {
                "event": "task_plan_snapshot",
                "source": source,
                "reason": reason,
                "warnings": warnings,
                "todos": final_todos,
            },
        )
        return session

    def update_task_plan_todo(
        self,
        session_id: str,
        task_id: str,
        *,
        status: str | None = None,
        details: str | None = None,
        result_ref: str | None = None,
    ) -> dict[str, Any]:
        session = self.create_or_get_session(session_id)
        progress = session.setdefault("task_progress", self.default_progress())
        plan = progress.setdefault("task_plan", {"todos": [], "updated_at": _now_iso()})
        todos = plan.setdefault("todos", [])
        now = _now_iso()
        normalized_id = str(task_id or "").strip()
        matched = False
        for todo in todos:
            if not isinstance(todo, dict) or todo.get("task_id") != normalized_id:
                continue
            matched = True
            if status:
                next_status = str(status).strip()
                if todo.get("status") == "completed" and next_status != "completed":
                    todo["details"] = (
                        str(todo.get("details") or "")
                        + f"\nIgnored downgrade attempt to `{next_status}`."
                    ).strip()[:1200]
                else:
                    todo["status"] = next_status
            if details is not None:
                todo["details"] = str(details).strip()[:1200]
            if result_ref is not None:
                todo["result_ref"] = str(result_ref).strip()[:500]
            todo["updated_at"] = now
            break
        if not matched:
            self.append_task_journal(
                session_id,
                {
                    "event": "task_plan_todo_update_ignored",
                    "task_id": normalized_id,
                    "status": status,
                    "details": details,
                    "result_ref": result_ref,
                    "reason": "task_id not found in fixed task_plan",
                },
            )
            return session
        plan["updated_at"] = now
        progress["updated_at"] = now
        self.save_session(session)
        self.append_task_journal(
            session_id,
            {
                "event": "task_plan_todo_update",
                "task_id": normalized_id,
                "status": status,
                "details": details,
                "result_ref": result_ref,
                "todos": todos,
            },
        )
        return session

    def recover_task_plan_from_journal(self, session_id: str) -> dict[str, Any] | None:
        path = self.task_journal_path(session_id)
        if not path.exists():
            legacy_path = self.legacy_task_journal_path(session_id)
            if legacy_path.exists():
                path = legacy_path
        if not path.exists():
            return None
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except Exception:
            return None
        for line in reversed(lines):
            try:
                payload = json.loads(line)
            except Exception:
                continue
            if not isinstance(payload, dict) or payload.get("event") != "task_plan_snapshot":
                continue
            todos = payload.get("todos")
            if not isinstance(todos, list) or not any(isinstance(item, dict) for item in todos):
                continue
            now = _now_iso()
            return {
                "todos": [item for item in todos if isinstance(item, dict)],
                "source": payload.get("source") or "journal_recovery",
                "reason": payload.get("reason") or "recovered_from_task_plan_journal",
                "warnings": payload.get("warnings") if isinstance(payload.get("warnings"), list) else [],
                "created_at": payload.get("created_at") or now,
                "updated_at": now,
                "recovered_from_journal": True,
            }
        return None

    def ensure_task_plan_recovered(self, session_id: str) -> dict[str, Any] | None:
        session = self.load_session(session_id)
        if session is None:
            return None
        progress = session.setdefault("task_progress", self.default_progress())
        plan = progress.get("task_plan") if isinstance(progress, dict) else None
        todos = plan.get("todos") if isinstance(plan, dict) else None
        if isinstance(todos, list) and todos:
            return session
        recovered = self.recover_task_plan_from_journal(session_id)
        if recovered is None:
            return session
        progress["task_plan"] = recovered
        progress["updated_at"] = _now_iso()
        self.save_session(session)
        self.append_task_journal(
            session_id,
            {
                "event": "task_plan_recovered",
                "reason": "session task_plan was empty; restored last non-empty snapshot",
                "todos": recovered.get("todos", []),
            },
        )
        return session

    def append_task_journal(self, session_id: str, event: dict[str, Any]) -> None:
        path = self.task_journal_path(session_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "created_at": _now_iso(),
            **event,
        }
        with _SESSION_FILE_LOCK:
            with path.open("a", encoding="utf-8", newline="\n") as handle:
                handle.write(json.dumps(payload, ensure_ascii=False, default=str) + "\n")

    def read_task_journal_tail(self, session_id: str, limit: int = 8) -> list[dict[str, Any]]:
        path = self.task_journal_path(session_id)
        if not path.exists():
            legacy_path = self.legacy_task_journal_path(session_id)
            if legacy_path.exists():
                path = legacy_path
        if not path.exists():
            return []
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except Exception:
            return []
        events: list[dict[str, Any]] = []
        for line in lines[-max(1, limit):]:
            try:
                payload = json.loads(line)
            except Exception:
                continue
            if isinstance(payload, dict):
                events.append(payload)
        return events

    def _current_task_id_from_progress(self, progress: dict[str, Any]) -> str:
        plan = progress.get("task_plan") if isinstance(progress, dict) else None
        todos = plan.get("todos", []) if isinstance(plan, dict) else []
        if isinstance(todos, list):
            for todo in todos:
                if not isinstance(todo, dict):
                    continue
                if str(todo.get("status") or "pending") != "completed":
                    return str(todo.get("task_id") or "").strip()
        return ""

    def append_tool_event(
        self,
        session_id: str,
        *,
        event_type: str,
        tool_name: str,
        tool_call_id: str | None = None,
        arguments: Any | None = None,
        content: Any | None = None,
    ) -> dict[str, Any]:
        session = self.ensure_task_plan_recovered(session_id) or self.create_or_get_session(session_id)
        progress = session.setdefault("task_progress", self.default_progress())
        if "task_outputs" not in progress or not isinstance(progress.get("task_outputs"), dict):
            progress["task_outputs"] = {}
        now = _now_iso()
        task_id = self._current_task_id_from_progress(progress) or "unassigned"
        payload = {
            "created_at": now,
            "event": str(event_type or "").strip() or "tool_event",
            "task_id": task_id,
            "tool_name": str(tool_name or "tool").strip() or "tool",
            "tool_call_id": str(tool_call_id or "").strip(),
            "arguments": _compact_jsonable(arguments, MAX_STORED_TOOL_ARGUMENT_CHARS),
            "content": _compact_jsonable(content, MAX_STORED_TASK_OUTPUT_CHARS),
        }

        outputs = progress.setdefault("task_outputs", {})
        task_output = outputs.setdefault(
            task_id,
            {
                "task_id": task_id,
                "events": [],
                "updated_at": now,
            },
        )
        events = task_output.setdefault("events", [])
        if isinstance(events, list):
            events.append(payload)
            task_output["events"] = events[-MAX_TASK_OUTPUT_EVENTS:]
        else:
            task_output["events"] = [payload]
        task_output["updated_at"] = now
        outputs[task_id] = task_output
        progress["updated_at"] = now
        self.save_session(session)

        path = self.tool_events_path(session_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        with _SESSION_FILE_LOCK:
            with path.open("a", encoding="utf-8", newline="\n") as handle:
                handle.write(json.dumps(payload, ensure_ascii=False, default=str) + "\n")
        return session

    def read_tool_events_tail(self, session_id: str, limit: int = 12) -> list[dict[str, Any]]:
        path = self.tool_events_path(session_id)
        if not path.exists():
            legacy_path = self.legacy_tool_events_path(session_id)
            if legacy_path.exists():
                path = legacy_path
        if not path.exists():
            return []
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except Exception:
            return []
        events: list[dict[str, Any]] = []
        for line in lines[-max(1, limit):]:
            try:
                payload = json.loads(line)
            except Exception:
                continue
            if isinstance(payload, dict):
                events.append(payload)
        return events

    def load_tool_result_cache(self, session_id: str) -> dict[str, dict[str, Any]]:
        path = self.tool_result_cache_path(session_id)
        if not path.exists():
            return {}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return {}
        if not isinstance(data, dict):
            return {}
        entries = data.get("entries", data)
        if not isinstance(entries, dict):
            return {}
        return {
            str(key): value
            for key, value in entries.items()
            if isinstance(value, dict)
        }

    def save_tool_result_cache(self, session_id: str, entries: dict[str, dict[str, Any]]) -> None:
        path = self.tool_result_cache_path(session_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        now = _now_iso()
        clean_entries = {
            str(key): _compact_jsonable(value, MAX_STORED_TOOL_CONTENT_CHARS)
            for key, value in entries.items()
            if isinstance(value, dict)
        }
        ordered = sorted(
            clean_entries.items(),
            key=lambda item: float(item[1].get("created_at_ms") or 0),
            reverse=True,
        )[:MAX_TOOL_RESULT_CACHE_ENTRIES]
        payload = {
            "updated_at": now,
            "entries": dict(ordered),
        }
        with _SESSION_FILE_LOCK:
            path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")

    def get_tool_result_cache_entry(self, session_id: str, call_key: str) -> dict[str, Any] | None:
        if not session_id or not call_key:
            return None
        return self.load_tool_result_cache(session_id).get(call_key)

    def set_tool_result_cache_entry(
        self,
        session_id: str,
        call_key: str,
        entry: dict[str, Any],
    ) -> None:
        if not session_id or not call_key or not isinstance(entry, dict):
            return
        entries = self.load_tool_result_cache(session_id)
        entries[call_key] = entry
        self.save_tool_result_cache(session_id, entries)

    def get_unfinished_task_plan_todos(self, session_id: str) -> list[dict[str, Any]]:
        session = self.ensure_task_plan_recovered(session_id)
        if session is None:
            return []
        progress = session.get("task_progress", {})
        plan = progress.get("task_plan") if isinstance(progress, dict) else None
        todos = plan.get("todos", []) if isinstance(plan, dict) else []
        if not isinstance(todos, list):
            return []
        return [
            todo for todo in todos
            if isinstance(todo, dict) and str(todo.get("status") or "pending") != "completed"
        ]

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

    def get_history(self, session_id: str) -> list[dict[str, Any]]:
        session = self.ensure_task_plan_recovered(session_id)
        if session is None:
            return []
        history: list[dict[str, Any]] = []
        messages = session.get("messages", [])
        protected_assistant_indexes: set[int] = set()
        pending_user = False
        if isinstance(messages, list):
            completed_turns: list[int] = []
            for index, item in enumerate(messages):
                if not isinstance(item, dict):
                    continue
                role = item.get("role", "")
                if role == "user":
                    pending_user = True
                elif role == "assistant" and pending_user:
                    completed_turns.append(index)
                    pending_user = False
            protected_assistant_indexes = set(completed_turns[-RECENT_TOOL_HISTORY_TURNS:])

        for index, item in enumerate(messages if isinstance(messages, list) else []):
            if not isinstance(item, dict):
                continue
            role = item.get("role", "")
            content = item.get("content", "")
            if role in {"user", "assistant"} and isinstance(content, str):
                if role == "assistant" and index in protected_assistant_indexes:
                    tool_events = normalize_tool_events(item.get("tools"), index)
                    _append_tool_history_entries(history, deduplicate_tool_history_events(tool_events))
                history.append({"role": role, "content": content})
        progress = session.get("task_progress", {})
        plan = progress.get("task_plan") if isinstance(progress, dict) else None
        if isinstance(plan, dict) and isinstance(plan.get("todos"), list) and plan.get("todos"):
            todo_lines = []
            for todo in plan.get("todos", []):
                if not isinstance(todo, dict):
                    continue
                todo_lines.append(
                    "- {task_id}: {status} | {content} | details={details} | result={result_ref}".format(
                        task_id=todo.get("task_id", ""),
                        status=todo.get("status", "pending"),
                        content=todo.get("content", ""),
                        details=str(todo.get("details") or "")[:300],
                        result_ref=str(todo.get("result_ref") or "")[:180],
                    )
                )
            if todo_lines:
                journal_events = self.read_task_journal_tail(session_id, limit=5)
                journal_lines = []
                for event in journal_events:
                    journal_lines.append(
                        "- {created_at} {event}: {summary}".format(
                            created_at=event.get("created_at", ""),
                            event=event.get("event", ""),
                            summary=str(event.get("task_id") or event.get("reason") or event.get("warnings") or "")[:240],
                        )
                    )
                content = (
                    "Previous task_plan state from session memory (advisory). "
                    "Use this for context but the model owns the current plan.\n"
                    + "\n".join(todo_lines)
                )
                if journal_lines:
                    content += "\nRecent task_plan journal:\n" + "\n".join(journal_lines)
                history.append({"role": "assistant", "content": content})
        task_outputs = progress.get("task_outputs") if isinstance(progress, dict) else None
        if isinstance(task_outputs, dict) and task_outputs:
            output_lines = [
                "Persisted task outputs from previous execution. Use these results directly; do not rerun completed tool work unless the user asks or the saved output is insufficient."
            ]
            current_task_id = self._current_task_id_from_progress(progress)
            preferred_ids = [current_task_id] if current_task_id else []
            preferred_ids.extend(task_id for task_id in task_outputs if task_id not in preferred_ids)
            for task_id in preferred_ids[:4]:
                output = task_outputs.get(task_id)
                if not isinstance(output, dict):
                    continue
                events = output.get("events") if isinstance(output.get("events"), list) else []
                if not events:
                    continue
                output_lines.append(f"Task `{task_id}` recent tool outputs:")
                for event in events[-6:]:
                    if not isinstance(event, dict):
                        continue
                    preview = event.get("content")
                    if not isinstance(preview, str):
                        try:
                            preview = json.dumps(preview, ensure_ascii=False, default=str)
                        except Exception:
                            preview = str(preview)
                    preview = (preview or "").strip()
                    if len(preview) > 900:
                        preview = preview[:900] + "\n[... truncated persisted tool output ...]"
                    output_lines.append(
                        "- {created_at} {event} `{tool}`: {preview}".format(
                            created_at=event.get("created_at", ""),
                            event=event.get("event", ""),
                            tool=event.get("tool_name", "tool"),
                            preview=preview,
                        )
                    )
            if len(output_lines) > 1:
                history.append({"role": "assistant", "content": "\n".join(output_lines)})
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

    # ------------------------------------------------------------------
    # task_plan.json is the single source of truth for task progress.
    # task_plan.jsonl is append-only audit history; session.json stores
    # only task_plan_ref and never stores the full todo list.
    # ------------------------------------------------------------------

    def _task_plan_ref(self, updated_at: str = "") -> dict[str, Any]:
        return {"path": "task_plan.json", "updated_at": updated_at}

    def _empty_task_plan(self) -> dict[str, Any]:
        return {
            "todos": [],
            "source": "",
            "reason": "",
            "warnings": [],
            "created_at": "",
            "updated_at": "",
        }

    def _normalize_plan_todos(self, todos: list[dict[str, Any]], now: str) -> list[dict[str, Any]]:
        normalized: list[dict[str, Any]] = []
        for index, todo in enumerate(todos, 1):
            if not isinstance(todo, dict):
                continue
            task_id = str(todo.get("task_id") or f"step-{index}").strip()
            content = str(todo.get("content") or task_id).strip()
            normalized.append(
                {
                    "task_id": task_id,
                    "content": content,
                    "activeForm": str(todo.get("activeForm") or todo.get("active_form") or content).strip(),
                    "status": str(todo.get("status") or "pending").strip(),
                    "details": str(todo.get("details") or "").strip(),
                    "result_ref": str(todo.get("result_ref") or "").strip(),
                    "created_at": str(todo.get("created_at") or now),
                    "updated_at": str(todo.get("updated_at") or now),
                }
            )
        return normalized

    def _sync_task_plan_ref_to_session(self, session_id: str, updated_at: str) -> dict[str, Any]:
        session = self.create_or_get_session(session_id)
        progress = session.setdefault("task_progress", self.default_progress())
        progress.pop("task_plan", None)
        progress["task_plan_ref"] = self._task_plan_ref(updated_at)
        progress["updated_at"] = _now_iso()
        self.save_session(session)
        return session

    def load_task_plan(self, session_id: str) -> dict[str, Any]:
        path = self.task_plan_path(session_id)
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                data = self._empty_task_plan()
                data["warnings"] = ["task_plan.json is unreadable"]
            if not isinstance(data, dict):
                data = self._empty_task_plan()
                data["warnings"] = ["task_plan.json is invalid"]
            todos = data.get("todos")
            data["todos"] = [item for item in todos if isinstance(item, dict)] if isinstance(todos, list) else []
            return data

        # One-time migration from legacy session.json task_progress.task_plan.
        session = self.load_session(session_id)
        progress = session.get("task_progress", {}) if isinstance(session, dict) else {}
        legacy_plan = progress.get("task_plan") if isinstance(progress, dict) else None
        if isinstance(legacy_plan, dict) and isinstance(legacy_plan.get("todos"), list) and legacy_plan.get("todos"):
            now = _now_iso()
            migrated = {
                "todos": self._normalize_plan_todos(legacy_plan.get("todos", []), now),
                "source": legacy_plan.get("source") or "legacy_session_migration",
                "reason": legacy_plan.get("reason") or "migrated_from_session_json",
                "warnings": legacy_plan.get("warnings") if isinstance(legacy_plan.get("warnings"), list) else [],
                "created_at": legacy_plan.get("created_at") or now,
                "updated_at": legacy_plan.get("updated_at") or now,
            }
            self.save_task_plan(session_id, migrated, audit_event="task_plan_migrated_from_session")
            return migrated

        recovered = self.recover_task_plan_from_journal(session_id)
        if recovered is not None:
            self.save_task_plan(session_id, recovered, audit_event="task_plan_recovered")
            return recovered
        return self._empty_task_plan()

    def save_task_plan(
        self,
        session_id: str,
        plan: dict[str, Any],
        *,
        audit_event: str = "task_plan_saved",
        incoming_todos: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        now = _now_iso()
        payload = dict(plan)
        payload["todos"] = self._normalize_plan_todos(payload.get("todos", []), now)
        payload.setdefault("source", "")
        payload.setdefault("reason", "")
        payload.setdefault("warnings", [])
        payload.setdefault("created_at", now)
        payload["updated_at"] = str(payload.get("updated_at") or now)

        path = self.task_plan_path(session_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        with _SESSION_FILE_LOCK:
            path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")

        session = self._sync_task_plan_ref_to_session(session_id, payload["updated_at"])
        event_payload: dict[str, Any] = {
            "event": audit_event,
            "task_plan_path": "task_plan.json",
            "source": payload.get("source"),
            "reason": payload.get("reason"),
            "warnings": payload.get("warnings", []),
            "todos": payload.get("todos", []),
        }
        if incoming_todos is not None:
            event_payload["incoming_todos"] = incoming_todos
        self.append_task_journal(session_id, event_payload)
        return session

    def set_task_plan(
        self,
        session_id: str,
        todos: list[dict[str, Any]],
        source: str = "auto",
        reason: str = "",
    ) -> dict[str, Any]:
        now = _now_iso()
        incoming = self._normalize_plan_todos(todos, now)
        existing_plan = self.load_task_plan(session_id)
        existing_todos = self._normalize_plan_todos(existing_plan.get("todos", []), now)

        if not existing_todos:
            plan = {
                "todos": incoming,
                "source": source,
                "reason": reason,
                "warnings": [],
                "created_at": now,
                "updated_at": now,
            }
            return self.save_task_plan(session_id, plan, audit_event="task_plan_snapshot")

        warnings: list[str] = []
        existing_by_id = {str(item.get("task_id") or ""): item for item in existing_todos}
        incoming_by_id = {str(item.get("task_id") or ""): item for item in incoming}
        existing_ids = [str(item.get("task_id") or "") for item in existing_todos]
        incoming_ids = [str(item.get("task_id") or "") for item in incoming]
        new_ids = [task_id for task_id in incoming_ids if task_id not in existing_by_id]
        removed_ids = [task_id for task_id in existing_ids if task_id not in incoming_by_id]

        final_todos: list[dict[str, Any]] = []
        for existing in existing_todos:
            task_id = str(existing.get("task_id") or "")
            incoming_item = incoming_by_id.get(task_id)
            if incoming_item is None:
                if reason == "plan_changed" and str(existing.get("status") or "pending") == "pending":
                    continue
                warnings.append(f"Preserved task ID `{task_id}` because only pending plan_changed tasks may be removed.")
                final_todos.append(existing)
                continue

            merged = dict(existing)
            for field in ("content", "activeForm"):
                incoming_value = str(incoming_item.get(field) or "").strip()
                existing_value = str(existing.get(field) or "").strip()
                if incoming_value and incoming_value != existing_value:
                    warnings.append(f"Ignored `{field}` change for `{task_id}` because task identity is locked.")
            for field in ("status", "details", "result_ref"):
                incoming_value = str(incoming_item.get(field) or "").strip()
                if incoming_value != str(existing.get(field) or "").strip():
                    merged[field] = incoming_value
            merged["updated_at"] = now
            final_todos.append(merged)

        for task_id in new_ids:
            incoming_item = incoming_by_id.get(task_id, {})
            if reason == "plan_changed" and str(incoming_item.get("status") or "pending") == "pending":
                next_item = dict(incoming_item)
                next_item["created_at"] = now
                next_item["updated_at"] = now
                final_todos.append(next_item)
            else:
                warnings.append(f"Rejected new task ID `{task_id}` because only pending plan_changed tasks may be added.")

        if incoming_ids != [str(item.get("task_id") or "") for item in final_todos]:
            if not new_ids and not removed_ids:
                warnings.append("Ignored task reordering because task order is locked.")

        plan = {
            "todos": final_todos,
            "source": existing_plan.get("source") or source,
            "reason": reason or existing_plan.get("reason") or "",
            "warnings": warnings,
            "created_at": existing_plan.get("created_at") or now,
            "updated_at": now,
        }
        return self.save_task_plan(session_id, plan, audit_event="task_plan_update", incoming_todos=incoming)

    def update_task_plan_todo(
        self,
        session_id: str,
        task_id: str,
        *,
        status: str | None = None,
        details: str | None = None,
        result_ref: str | None = None,
    ) -> dict[str, Any]:
        plan = self.load_task_plan(session_id)
        todos = self._normalize_plan_todos(plan.get("todos", []), _now_iso())
        normalized_id = str(task_id or "").strip()
        found = False
        now = _now_iso()
        for todo in todos:
            if str(todo.get("task_id") or "") != normalized_id:
                continue
            found = True
            if status is not None:
                todo["status"] = str(status).strip()
            if details is not None:
                todo["details"] = str(details).strip()[:1200]
            if result_ref is not None:
                todo["result_ref"] = str(result_ref).strip()[:500]
            todo["updated_at"] = now
            break
        if not found:
            self.append_task_journal(
                session_id,
                {
                    "event": "task_plan_todo_update_ignored",
                    "task_id": normalized_id,
                    "status": status,
                    "details": details,
                    "result_ref": result_ref,
                    "reason": "task_id not found in task_plan.json",
                },
            )
            return self.create_or_get_session(session_id)
        plan["todos"] = todos
        plan["updated_at"] = now
        return self.save_task_plan(
            session_id,
            plan,
            audit_event="task_plan_todo_update",
            incoming_todos=todos,
        )

    def ensure_task_plan_recovered(self, session_id: str) -> dict[str, Any] | None:
        session = self.create_or_get_session(session_id)
        plan = self.load_task_plan(session_id)
        progress = session.setdefault("task_progress", self.default_progress())
        progress.pop("task_plan", None)
        progress["task_plan_ref"] = self._task_plan_ref(str(plan.get("updated_at") or ""))
        self.save_session(session)
        return session

    def get_unfinished_task_plan_todos(self, session_id: str) -> list[dict[str, Any]]:
        plan = self.load_task_plan(session_id)
        todos = plan.get("todos", [])
        if not isinstance(todos, list):
            return []
        return [
            todo for todo in todos
            if isinstance(todo, dict) and str(todo.get("status") or "pending") != "completed"
        ]

    def _current_task_id_from_progress(self, progress: dict[str, Any]) -> str:
        # progress no longer owns todos; derive current task from task_outputs
        # caller context if possible, otherwise leave events unassigned.
        return ""

    def get_history(self, session_id: str) -> list[dict[str, Any]]:
        session = self.ensure_task_plan_recovered(session_id)
        if session is None:
            return []
        history: list[dict[str, Any]] = []
        messages = session.get("messages", [])
        protected_assistant_indexes: set[int] = set()
        pending_user = False
        if isinstance(messages, list):
            completed_turns: list[int] = []
            for index, item in enumerate(messages):
                if not isinstance(item, dict):
                    continue
                role = item.get("role", "")
                if role == "user":
                    pending_user = True
                elif role == "assistant" and pending_user:
                    completed_turns.append(index)
                    pending_user = False
            protected_assistant_indexes = set(completed_turns[-RECENT_TOOL_HISTORY_TURNS:])

        for index, item in enumerate(messages if isinstance(messages, list) else []):
            if not isinstance(item, dict):
                continue
            role = item.get("role", "")
            content = item.get("content", "")
            if role in {"user", "assistant"} and isinstance(content, str):
                if role == "assistant" and index in protected_assistant_indexes:
                    tool_events = normalize_tool_events(item.get("tools"), index)
                    _append_tool_history_entries(history, deduplicate_tool_history_events(tool_events))
                history.append({"role": role, "content": content})

        plan = self.load_task_plan(session_id)
        todos = plan.get("todos", [])
        if isinstance(todos, list) and todos:
            todo_lines = []
            for todo in todos:
                if not isinstance(todo, dict):
                    continue
                todo_lines.append(
                    "- {task_id}: {status} | {content} | details={details} | result={result_ref}".format(
                        task_id=todo.get("task_id", ""),
                        status=todo.get("status", "pending"),
                        content=todo.get("content", ""),
                        details=str(todo.get("details") or "")[:300],
                        result_ref=str(todo.get("result_ref") or "")[:180],
                    )
                )
            if todo_lines:
                history.append(
                    {
                        "role": "assistant",
                        "content": (
                            "Current task_plan state from task_plan.json (authoritative progress anchor). "
                            "Do not reconstruct progress from session.json.\n"
                            + "\n".join(todo_lines)
                        ),
                    }
                )

        progress = session.get("task_progress", {})
        task_outputs = progress.get("task_outputs") if isinstance(progress, dict) else None
        if isinstance(task_outputs, dict) and task_outputs:
            output_lines = [
                "Persisted task outputs from previous execution. Use these results directly; do not rerun completed tool work unless the user asks or the saved output is insufficient."
            ]
            for task_id, output in list(task_outputs.items())[:4]:
                if not isinstance(output, dict):
                    continue
                events = output.get("events") if isinstance(output.get("events"), list) else []
                if not events:
                    continue
                output_lines.append(f"Task `{task_id}` recent tool outputs:")
                for event in events[-6:]:
                    preview = event.get("content") if isinstance(event, dict) else ""
                    if not isinstance(preview, str):
                        preview = json.dumps(preview, ensure_ascii=False, default=str)
                    if len(preview) > 900:
                        preview = preview[:900] + "\n[... truncated persisted tool output ...]"
                    output_lines.append(f"- {event.get('created_at', '')} {event.get('event', '')} `{event.get('tool_name', 'tool')}`: {preview}")
            if len(output_lines) > 1:
                history.append({"role": "assistant", "content": "\n".join(output_lines)})

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
