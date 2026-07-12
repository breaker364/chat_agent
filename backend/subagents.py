from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from threading import Lock, Thread
from typing import Any
from uuid import uuid4

from .config import create_chat_deepseek, load_llm_config
from .session_events import get_session_event_hub

logger = logging.getLogger(__name__)
SUBAGENT_MAX_RUNTIME_SECONDS = 180


@dataclass
class AgentDefinition:
    agent_type: str
    when_to_use: str
    system_prompt: str
    allowed_tool_names: set[str] | None = None
    disallowed_tool_names: set[str] | None = None
    max_turns: int = 20
    background: bool = False


def _subagent_dir(root: Path) -> Path:
    path = root / "sessionss" / "subagents"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _subagent_transcript_path(root: Path, agent_id: str) -> Path:
    return _subagent_dir(root) / f"{agent_id}.json"


def _subagent_task_path(root: Path, agent_id: str) -> Path:
    return _subagent_dir(root) / f"{agent_id}.task.json"


def built_in_subagents() -> dict[str, AgentDefinition]:
    return {
        "general-purpose": AgentDefinition(
            agent_type="general-purpose",
            when_to_use="General delegated tasks that need independent execution.",
            system_prompt=(
                "You are a delegated subagent. Execute the assigned task directly and return a concise result. "
                "Stay within scope. Do not ask follow-up questions unless absolutely necessary."
            ),
        ),
        "Explore": AgentDefinition(
            agent_type="Explore",
            when_to_use="Read-only codebase exploration, file search, architecture understanding.",
            system_prompt=(
                "You are a read-only exploration subagent. Inspect files, summarize structure, and report findings. "
                "Do not modify files or delete anything."
            ),
            disallowed_tool_names={"write_file", "append_file", "delete_file", "run_python_file", "Agent", "SendMessage"},
        ),
        "Plan": AgentDefinition(
            agent_type="Plan",
            when_to_use="Break a task into steps, tradeoffs, and implementation guidance.",
            system_prompt=(
                "You are a planning subagent. Produce a concrete execution plan with assumptions, risks, and next steps. "
                "Do not modify files."
            ),
            disallowed_tool_names={"write_file", "append_file", "delete_file", "run_python_file", "Agent", "SendMessage"},
        ),
        "verification": AgentDefinition(
            agent_type="verification",
            when_to_use="Independent verification of outputs, logic, or test execution results.",
            system_prompt=(
                "You are a verification subagent. Independently verify the assigned claim or implementation and report "
                "only the verification outcome, evidence, and any discrepancies."
            ),
            disallowed_tool_names={"write_file", "append_file", "delete_file", "Agent", "SendMessage"},
        ),
    }


def filter_tools_for_subagent(tools: list[Any], definition: AgentDefinition) -> list[Any]:
    filtered = list(tools)
    if definition.allowed_tool_names is not None:
        filtered = [tool for tool in filtered if getattr(tool, "name", "") in definition.allowed_tool_names]
    if definition.disallowed_tool_names:
        filtered = [tool for tool in filtered if getattr(tool, "name", "") not in definition.disallowed_tool_names]
    return filtered


def write_subagent_transcript(root: Path, agent_id: str, payload: dict[str, Any]) -> None:
    _subagent_transcript_path(root, agent_id).write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def write_subagent_task_state(root: Path, agent_id: str, payload: dict[str, Any]) -> None:
    _subagent_task_path(root, agent_id).write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def read_subagent_task_state(root: Path, agent_id: str) -> dict[str, Any] | None:
    path = _subagent_task_path(root, agent_id)
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


async def run_subagent(
    *,
    agent_id: str | None = None,
    prompt: str,
    description: str,
    subagent_type: str,
    workspace_dir: str | Path,
    config_path: str | Path | None = None,
    history: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
    from langgraph.prebuilt import create_react_agent

    workspace = Path(workspace_dir).resolve()
    definition = built_in_subagents().get(subagent_type) or built_in_subagents()["general-purpose"]
    cfg = load_llm_config(config_path)
    llm = create_chat_deepseek(cfg)
    from .tools import get_all_tools

    all_tools = await get_all_tools(workspace_dir=workspace)
    tools = filter_tools_for_subagent(all_tools, definition)
    agent = create_react_agent(model=llm, tools=tools, state_schema=None)

    agent_id = agent_id or f"subagent-{uuid4().hex[:10]}"
    started_at = time.monotonic()
    messages = [SystemMessage(content=definition.system_prompt)]
    if history:
        for item in history:
            role = item.get("role", "")
            content = item.get("content", "")
            if role == "user":
                messages.append(HumanMessage(content=content))
            elif role == "assistant":
                messages.append(AIMessage(content=content))
    messages.append(HumanMessage(content=prompt))

    result = await agent.ainvoke(
        {"messages": messages},
        config={"recursion_limit": definition.max_turns},
    )
    output_messages = result.get("messages", []) if isinstance(result, dict) else []
    final_text = ""
    if output_messages:
        last = output_messages[-1]
        final_text = getattr(last, "content", "") if not isinstance(last, dict) else str(last.get("content", ""))

    payload = {
        "agent_id": agent_id,
        "agent_type": definition.agent_type,
        "description": description,
        "prompt": prompt,
        "history": history or [],
        "result": final_text,
        "duration_seconds": round(time.monotonic() - started_at, 3),
        "created_at": time.time(),
    }
    write_subagent_transcript(workspace, agent_id, payload)
    return payload


class AsyncSubagentManager:
    def __init__(self) -> None:
        self._tasks: dict[str, dict[str, Any]] = {}
        self._mailboxes: dict[str, list[str]] = {}
        self._locks: dict[str, Lock] = {}

    def _persist(self, workspace_dir: str | Path, agent_id: str) -> None:
        state = self._tasks[agent_id]
        write_subagent_task_state(Path(workspace_dir).resolve(), agent_id, state)

    def launch(
        self,
        *,
        prompt: str,
        description: str,
        subagent_type: str,
        workspace_dir: str | Path,
        config_path: str | Path | None = None,
        history: list[dict[str, str]] | None = None,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        agent_id = f"subagent-{uuid4().hex[:10]}"
        state = {
            "agent_id": agent_id,
            "status": "running",
            "description": description,
            "subagent_type": subagent_type,
            "prompt": prompt,
            "history": history or [],
            "result": "",
            "error": "",
            "created_at": time.time(),
            "session_id": session_id or "",
            "message_queue_size": 0,
            "last_notification": "",
        }
        self._tasks[agent_id] = state
        self._mailboxes.setdefault(agent_id, [])
        self._locks[agent_id] = Lock()
        self._persist(workspace_dir, agent_id)

        def _thread_runner() -> None:
            try:
                from .session_store import SessionStore
                started_at = time.monotonic()

                payload = asyncio.run(
                    run_subagent(
                        agent_id=agent_id,
                        prompt=prompt,
                        description=description,
                        subagent_type=subagent_type,
                        workspace_dir=workspace_dir,
                        config_path=config_path,
                        history=history,
                    )
                )
                state["status"] = "idle"
                state["result"] = payload.get("result", "")
                state["transcript_file"] = str(_subagent_transcript_path(Path(workspace_dir).resolve(), payload["agent_id"]))
                state["last_notification"] = "Initial run completed."
                self._persist(workspace_dir, agent_id)
                if state.get("session_id"):
                    store = SessionStore(Path(workspace_dir).resolve())
                    store.update_subagent_task(state["session_id"], state)
                    store.add_subagent_notification(
                        state["session_id"],
                        {
                            "agent_id": agent_id,
                            "status": "completed",
                            "message": f"Subagent `{state['description']}` completed.",
                            "created_at": time.time(),
                        },
                    )
                    get_session_event_hub().publish(
                        state["session_id"],
                        {
                            "type": "subagent_notification",
                            "agent_id": agent_id,
                            "status": "completed",
                            "message": f"Subagent `{state['description']}` completed.",
                            "created_at": time.time(),
                        },
                    )
                if time.monotonic() - started_at > SUBAGENT_MAX_RUNTIME_SECONDS:
                    state["last_notification"] = "Subagent exceeded runtime budget."
            except Exception as exc:
                logger.exception("Subagent %s failed", agent_id)
                state["status"] = "failed"
                state["error"] = str(exc)
                self._persist(workspace_dir, agent_id)

        Thread(target=_thread_runner, daemon=True).start()
        return dict(state)

    def fail_task(
        self,
        agent_id: str,
        *,
        workspace_dir: str | Path,
        error: str,
    ) -> bool:
        state = self._tasks.get(agent_id)
        if state is None:
            return False
        state["status"] = "failed"
        state["error"] = error
        state["last_notification"] = error
        self._persist(workspace_dir, agent_id)
        return True

    def get_task(self, agent_id: str, workspace_dir: str | Path | None = None) -> dict[str, Any] | None:
        task = self._tasks.get(agent_id)
        if task is not None:
            return dict(task)
        if workspace_dir is None:
            return None
        return read_subagent_task_state(Path(workspace_dir).resolve(), agent_id)

    def send_message(
        self,
        agent_id: str,
        message: str,
        *,
        workspace_dir: str | Path,
        config_path: str | Path | None = None,
    ) -> bool:
        if agent_id not in self._tasks:
            return False
        mailbox = self._mailboxes.setdefault(agent_id, [])
        mailbox.append(message)
        self._tasks[agent_id]["message_queue_size"] = len(mailbox)
        self._tasks[agent_id]["last_notification"] = "Message queued."
        self._persist(workspace_dir, agent_id)
        self._resume_from_mailbox(agent_id, workspace_dir=workspace_dir, config_path=config_path)
        return True

    def _resume_from_mailbox(
        self,
        agent_id: str,
        *,
        workspace_dir: str | Path,
        config_path: str | Path | None = None,
    ) -> None:
        state = self._tasks.get(agent_id)
        if state is None:
            return
        lock = self._locks.setdefault(agent_id, Lock())
        if not lock.acquire(blocking=False):
            return

        def _thread_runner() -> None:
            try:
                from .session_store import SessionStore

                while True:
                    mailbox = self._mailboxes.setdefault(agent_id, [])
                    if not mailbox:
                        state["status"] = "idle"
                        state["message_queue_size"] = 0
                        self._persist(workspace_dir, agent_id)
                        if state.get("session_id"):
                            SessionStore(Path(workspace_dir).resolve()).update_subagent_task(state["session_id"], state)
                        break

                    message = mailbox.pop(0)
                    state["message_queue_size"] = len(mailbox)
                    state["status"] = "running"
                    state["last_notification"] = "Processing queued message."
                    self._persist(workspace_dir, agent_id)
                    if state.get("session_id"):
                        store = SessionStore(Path(workspace_dir).resolve())
                        store.update_subagent_task(state["session_id"], state)
                        store.add_subagent_notification(
                            state["session_id"],
                            {
                                "agent_id": agent_id,
                                "status": "message_received",
                                "message": f"Message delivered to subagent `{state['description']}`.",
                                "created_at": time.time(),
                            },
                        )
                        get_session_event_hub().publish(
                            state["session_id"],
                            {
                                "type": "subagent_notification",
                                "agent_id": agent_id,
                                "status": "message_received",
                                "message": f"Message delivered to subagent `{state['description']}`.",
                                "created_at": time.time(),
                            },
                        )

                    prompt = (
                        f"{state['prompt']}\n\n"
                        f"<cross-session-message>\n{message}\n</cross-session-message>\n"
                        "Continue the assigned task while incorporating the new message."
                    )
                    payload = asyncio.run(
                        run_subagent(
                            agent_id=agent_id,
                            prompt=prompt,
                            description=state["description"],
                            subagent_type=state["subagent_type"],
                            workspace_dir=workspace_dir,
                            config_path=config_path,
                            history=state.get("history", []),
                        )
                    )
                    state["result"] = payload.get("result", "")
                    state["last_notification"] = "Queued message processed."
                    self._persist(workspace_dir, agent_id)
                    if state.get("session_id"):
                        store = SessionStore(Path(workspace_dir).resolve())
                        store.update_subagent_task(state["session_id"], state)
                        store.add_subagent_notification(
                            state["session_id"],
                            {
                                "agent_id": agent_id,
                                "status": "completed",
                                "message": f"Queued message processed by subagent `{state['description']}`.",
                                "created_at": time.time(),
                            },
                        )
                        get_session_event_hub().publish(
                            state["session_id"],
                            {
                                "type": "subagent_notification",
                                "agent_id": agent_id,
                                "status": "completed",
                                "message": f"Queued message processed by subagent `{state['description']}`.",
                                "created_at": time.time(),
                            },
                        )
            except Exception as exc:
                logger.exception("Subagent %s failed while processing mailbox", agent_id)
                state["status"] = "failed"
                state["error"] = str(exc)
                self._persist(workspace_dir, agent_id)
            finally:
                lock.release()

        Thread(target=_thread_runner, daemon=True).start()
