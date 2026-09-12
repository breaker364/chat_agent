"""Low-cardinality runtime metrics for workflow operations."""

from __future__ import annotations

from collections import Counter
from threading import RLock
from typing import Any, Mapping


class WorkflowMetrics:
    """Aggregate operational counts without retaining request or tool payloads."""

    def __init__(self) -> None:
        self._lock = RLock()
        self._nodes: Counter[str] = Counter()
        self._tasks: Counter[str] = Counter()
        self._terminal: Counter[str] = Counter()

    def record_event(self, event: Mapping[str, Any]) -> None:
        node = str(event.get("node") or "").strip()
        status = str(event.get("status") or "").strip()
        with self._lock:
            if node and status:
                self._nodes[f"{node}:{status}"] += 1
            if event.get("task_id") and status:
                self._tasks[status] += 1

    def record_terminal(self, status: str) -> None:
        normalized = str(status or "blocked").strip() or "blocked"
        with self._lock:
            self._terminal[normalized] += 1

    def snapshot(self) -> dict[str, dict[str, int]]:
        with self._lock:
            return {
                "nodes": dict(self._nodes),
                "tasks": dict(self._tasks),
                "terminal": dict(self._terminal),
            }


WORKFLOW_METRICS = WorkflowMetrics()
