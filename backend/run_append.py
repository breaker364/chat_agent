from __future__ import annotations

import time
from dataclasses import asdict, dataclass
from threading import RLock
from typing import Any
from uuid import uuid4


@dataclass
class AppendCommand:
    append_id: str
    session_id: str
    run_id: str
    content: str
    created_at: float
    sequence: int
    status: str = "queued"
    updated_at: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class AppendCommandQueue:
    def __init__(self) -> None:
        self._lock = RLock()
        self._commands: dict[tuple[str, str], list[AppendCommand]] = {}
        self._sequences: dict[tuple[str, str], int] = {}

    def enqueue(self, session_id: str, run_id: str, content: str) -> AppendCommand:
        normalized_session_id = str(session_id or "default").strip() or "default"
        normalized_run_id = str(run_id or "").strip()
        normalized_content = str(content or "").strip()
        if not normalized_run_id:
            raise ValueError("run_id is required")
        if not normalized_content:
            raise ValueError("content is required")
        key = (normalized_session_id, normalized_run_id)
        now = time.time()
        with self._lock:
            sequence = self._sequences.get(key, 0) + 1
            self._sequences[key] = sequence
            command = AppendCommand(
                append_id=f"append-{uuid4().hex}",
                session_id=normalized_session_id,
                run_id=normalized_run_id,
                content=normalized_content,
                created_at=now,
                sequence=sequence,
                status="queued",
                updated_at=now,
            )
            self._commands.setdefault(key, []).append(command)
            return command

    def consume_pending(self, session_id: str, run_id: str) -> list[AppendCommand]:
        key = (str(session_id or "default").strip() or "default", str(run_id or "").strip())
        now = time.time()
        with self._lock:
            queued = [command for command in self._commands.get(key, []) if command.status == "queued"]
            for command in queued:
                command.status = "injected"
                command.updated_at = now
            return list(queued)

    def finalize_pending(self, session_id: str, run_id: str, status: str = "not_applied") -> list[AppendCommand]:
        key = (str(session_id or "default").strip() or "default", str(run_id or "").strip())
        now = time.time()
        with self._lock:
            finalized = []
            for command in self._commands.get(key, []):
                if command.status == "queued":
                    command.status = status
                    command.updated_at = now
                    finalized.append(command)
            return finalized

    def clear_run(self, session_id: str, run_id: str) -> None:
        key = (str(session_id or "default").strip() or "default", str(run_id or "").strip())
        with self._lock:
            self._commands.pop(key, None)
            self._sequences.pop(key, None)


_APPEND_COMMAND_QUEUE = AppendCommandQueue()


def enqueue_append_command(session_id: str, run_id: str, content: str) -> AppendCommand:
    return _APPEND_COMMAND_QUEUE.enqueue(session_id, run_id, content)


def consume_pending_append_commands(session_id: str, run_id: str) -> list[AppendCommand]:
    return _APPEND_COMMAND_QUEUE.consume_pending(session_id, run_id)


def finalize_pending_append_commands(
    session_id: str,
    run_id: str,
    status: str = "not_applied",
) -> list[AppendCommand]:
    return _APPEND_COMMAND_QUEUE.finalize_pending(session_id, run_id, status)


def clear_run_append_commands(session_id: str, run_id: str) -> None:
    _APPEND_COMMAND_QUEUE.clear_run(session_id, run_id)
