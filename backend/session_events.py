from __future__ import annotations

import asyncio
from collections import defaultdict
from typing import Any


class SessionEventHub:
    def __init__(self) -> None:
        self._queues: dict[str, list[asyncio.Queue[dict[str, Any]]]] = defaultdict(list)

    def subscribe(self, session_id: str) -> asyncio.Queue[dict[str, Any]]:
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._queues[session_id].append(queue)
        return queue

    def unsubscribe(self, session_id: str, queue: asyncio.Queue[dict[str, Any]]) -> None:
        queues = self._queues.get(session_id)
        if not queues:
            return
        self._queues[session_id] = [item for item in queues if item is not queue]
        if not self._queues[session_id]:
            self._queues.pop(session_id, None)

    def publish(self, session_id: str, event: dict[str, Any]) -> None:
        for queue in list(self._queues.get(session_id, [])):
            try:
                queue.put_nowait(event)
            except Exception:
                continue


_SESSION_EVENT_HUB = SessionEventHub()


def get_session_event_hub() -> SessionEventHub:
    return _SESSION_EVENT_HUB
