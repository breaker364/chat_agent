import asyncio
import json
import shutil
import unittest
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage, HumanMessage

from backend import main as main_module
from backend.agent import build_agent, stream_agent_events
from backend.main import app
from backend.runtime_context import bind_runtime_context, reset_runtime_context
from backend.session_store import SessionStore
from backend import tools as runtime_tools


class _EventIterator:
    def __init__(self, events):
        self.events = list(events)
        self.index = 0

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self.index >= len(self.events):
            raise StopAsyncIteration
        event = self.events[self.index]
        self.index += 1
        return event


class CapturingAgent:
    def __init__(self, events=None):
        self.events = list(events or [])
        self.calls = []

    def astream_events(self, values, *, config, version):
        self.calls.append(list(values["messages"]))
        return _EventIterator(self.events)


class JsonRequest:
    def __init__(self, body):
        self.body = body

    async def json(self):
        return self.body


class CompleteAuditor:
    def invoke(self, _messages):
        return AIMessage(content=json.dumps({"complete": True, "reason": "done", "status": "completed"}))


class RecordingChatModel:
    def __init__(self):
        self.calls = []
        self.bound_tools = []

    def bind_tools(self, tools):
        self.bound_tools.append(list(tools))
        return self

    def invoke(self, input, *args, **kwargs):
        self.calls.append(("invoke", input))
        return AIMessage(content="ok")

    def stream(self, input, *args, **kwargs):
        self.calls.append(("stream", input))
        yield AIMessage(content="ok")


class RunAppendTests(unittest.TestCase):
    def setUp(self):
        temp_root = Path.cwd() / "tmp" / "unittest"
        temp_root.mkdir(parents=True, exist_ok=True)
        self.temp_path = temp_root / f"{self._testMethodName}-{uuid4().hex}"
        self.temp_path.mkdir(parents=True, exist_ok=False)
        self.store = SessionStore(self.temp_path)
        with main_module._ACTIVE_RUNS_LOCK:
            main_module._ACTIVE_RUNS.clear()

    def tearDown(self):
        with main_module._ACTIVE_RUNS_LOCK:
            main_module._ACTIVE_RUNS.clear()
        shutil.rmtree(self.temp_path, ignore_errors=True)

    def _client(self):
        return TestClient(app)

    def _acquire_run(self, session_id):
        run, active = main_module._acquire_session_run(session_id, "test")
        self.assertIsNone(active)
        self.assertIsNotNone(run)
        return run

    def test_append_endpoint_accepts_command_for_active_run(self):
        run = self._acquire_run("append-active")
        with patch("backend.main.get_session_store", return_value=self.store):
            response = self._client().post(
                "/sessions/append-active/runs/current/append",
                json={"content": "请补充验证步骤", "run_id": run["run_id"]},
            )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["status"], "queued")
        self.assertEqual(payload["session_id"], "append-active")
        self.assertEqual(payload["run_id"], run["run_id"])
        self.assertTrue(payload["append_id"])
        progress = self.store.load_session("append-active")["task_progress"]
        self.assertEqual(progress["append_commands"][0]["status"], "queued")
        self.assertEqual(progress["append_commands"][0]["content"], "请补充验证步骤")

    def test_append_endpoint_rejects_missing_stale_terminal_and_empty_requests(self):
        self.store.create_or_get_session("reject-session")
        with patch("backend.main.get_session_store", return_value=self.store):
            no_active = self._client().post(
                "/sessions/idle-session/runs/current/append",
                json={"content": "补充内容"},
            )
        self.assertEqual(no_active.status_code, 409)
        self.assertEqual(no_active.json()["error"], "no_active_run")

        run = self._acquire_run("reject-session")
        with patch("backend.main.get_session_store", return_value=self.store):
            stale = self._client().post(
                "/sessions/reject-session/runs/current/append",
                json={"content": "补充内容", "run_id": "old-run"},
            )
            empty = self._client().post(
                "/sessions/reject-session/runs/current/append",
                json={"content": "   ", "run_id": run["run_id"]},
            )
        self.assertEqual(stale.status_code, 409)
        self.assertEqual(stale.json()["error"], "stale_run")
        self.assertEqual(empty.status_code, 400)
        self.assertEqual(empty.json()["error"], "empty_append")

        main_module._release_session_run("reject-session", run["run_id"])
        with patch("backend.main.get_session_store", return_value=self.store):
            terminal = self._client().post(
                "/sessions/reject-session/runs/current/append",
                json={"content": "补充内容", "run_id": run["run_id"]},
            )
        self.assertEqual(terminal.status_code, 409)
        self.assertEqual(terminal.json()["error"], "no_active_run")
        messages = self.store.load_session("reject-session")["messages"]
        self.assertEqual(messages, [])

    def test_append_commands_are_injected_before_next_model_call_with_marker_once(self):
        session_id = "inject-session"
        run = self._acquire_run(session_id)
        with patch("backend.main.get_session_store", return_value=self.store):
            response = self._client().post(
                f"/sessions/{session_id}/runs/current/append",
                json={"content": "改为输出三条结论", "run_id": run["run_id"]},
            )
        self.assertEqual(response.status_code, 200)

        agent = CapturingAgent(
            [
                {"event": "on_chat_model_stream", "name": "model", "data": {"chunk": AIMessage(content="final answer")}},
                {"event": "on_chain_end", "name": "LangGraph", "data": {}},
            ]
        )

        async def collect_once():
            context_tokens = bind_runtime_context(session_id, run["run_id"])
            try:
                with patch("backend.agent.SessionStore", return_value=self.store), patch(
                    "backend.agent.load_llm_config", return_value={}
                ), patch("backend.agent.create_chat_deepseek", return_value=CompleteAuditor()):
                    return [event async for event in stream_agent_events(agent, "原始任务", session_id)]
            finally:
                reset_runtime_context(context_tokens)

        events = asyncio.run(collect_once())

        first_call_human_messages = [
            message.content for message in agent.calls[0] if isinstance(message, HumanMessage)
        ]
        joined = "\n".join(first_call_human_messages)
        self.assertIn("原始任务", joined)
        self.assertIn("追加指令", joined)
        self.assertIn("改为输出三条结论", joined)
        self.assertEqual(joined.count("改为输出三条结论"), 1)
        debug_stages = [json.loads(event["data"]).get("stage") for event in events if event["event"] == "debug"]
        self.assertIn("append_command_injected", debug_stages)
        progress = self.store.load_session(session_id)["task_progress"]
        self.assertEqual(progress["append_commands"][0]["status"], "injected")

    def test_build_agent_model_injects_pending_appends_on_invoke_and_stream(self):
        session_id = "model-call-inject-session"
        run = self._acquire_run(session_id)
        llm = RecordingChatModel()

        def fake_create_react_agent(*, model, tools, state_schema):
            return model.bind_tools(tools)

        async def fake_get_all_tools(*args, **kwargs):
            return []

        with patch("backend.main.get_session_store", return_value=self.store):
            first = self._client().post(
                f"/sessions/{session_id}/runs/current/append",
                json={"content": "invoke 前追加", "run_id": run["run_id"]},
            )
        self.assertEqual(first.status_code, 200)

        context_tokens = bind_runtime_context(session_id, run["run_id"])
        try:
            with patch("backend.agent.SessionStore", return_value=self.store), patch(
                "backend.agent.load_llm_config", return_value={}
            ), patch("backend.agent.create_chat_deepseek", return_value=llm), patch(
                "backend.agent.get_all_tools", fake_get_all_tools
            ), patch("backend.agent.create_react_agent", fake_create_react_agent):
                model = asyncio.run(build_agent())
                model.invoke({"messages": [HumanMessage(content="原始任务")]})

            invoke_messages = llm.calls[0][1]["messages"]
            invoke_joined = "\n".join(
                message.content for message in invoke_messages if isinstance(message, HumanMessage)
            )
            self.assertIn("原始任务", invoke_joined)
            self.assertIn("用户追加指令", invoke_joined)
            self.assertIn("invoke 前追加", invoke_joined)
            self.assertEqual(invoke_joined.count("invoke 前追加"), 1)

            with patch("backend.main.get_session_store", return_value=self.store):
                second = self._client().post(
                    f"/sessions/{session_id}/runs/current/append",
                    json={"content": "stream 前追加", "run_id": run["run_id"]},
                )
            self.assertEqual(second.status_code, 200)

            with patch("backend.agent.SessionStore", return_value=self.store):
                list(model.stream({"messages": [HumanMessage(content="第二轮任务")]}))

            stream_messages = llm.calls[1][1]["messages"]
            stream_joined = "\n".join(
                message.content for message in stream_messages if isinstance(message, HumanMessage)
            )
            self.assertIn("第二轮任务", stream_joined)
            self.assertIn("用户追加指令", stream_joined)
            self.assertIn("stream 前追加", stream_joined)
            self.assertNotIn("invoke 前追加", stream_joined)

            progress = self.store.load_session(session_id)["task_progress"]
            statuses = [command["status"] for command in progress["append_commands"]]
            self.assertEqual(statuses, ["injected", "injected"])
        finally:
            reset_runtime_context(context_tokens)

    def test_multiple_append_commands_preserve_fifo_order_and_session_scope(self):
        run_a = self._acquire_run("append-a")
        run_b = self._acquire_run("append-b")
        with patch("backend.main.get_session_store", return_value=self.store):
            first = self._client().post(
                "/sessions/append-a/runs/current/append",
                json={"content": "第一条追加", "run_id": run_a["run_id"]},
            )
            second = self._client().post(
                "/sessions/append-a/runs/current/append",
                json={"content": "第二条追加", "run_id": run_a["run_id"]},
            )
        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)

        agent_b = CapturingAgent(
            [
                {"event": "on_chat_model_stream", "name": "model", "data": {"chunk": AIMessage(content="b answer")}},
                {"event": "on_chain_end", "name": "LangGraph", "data": {}},
            ]
        )
        agent_a = CapturingAgent(
            [
                {"event": "on_chat_model_stream", "name": "model", "data": {"chunk": AIMessage(content="a answer")}},
                {"event": "on_chain_end", "name": "LangGraph", "data": {}},
            ]
        )

        async def run_stream(agent, session_id, run_id):
            context_tokens = bind_runtime_context(session_id, run_id)
            try:
                with patch("backend.agent.SessionStore", return_value=self.store), patch(
                    "backend.agent.load_llm_config", return_value={}
                ), patch("backend.agent.create_chat_deepseek", return_value=CompleteAuditor()):
                    return [event async for event in stream_agent_events(agent, "任务", session_id)]
            finally:
                reset_runtime_context(context_tokens)

        asyncio.run(run_stream(agent_b, "append-b", run_b["run_id"]))
        b_joined = "\n".join(
            message.content for message in agent_b.calls[0] if isinstance(message, HumanMessage)
        )
        self.assertNotIn("第一条追加", b_joined)
        self.assertNotIn("第二条追加", b_joined)

        asyncio.run(run_stream(agent_a, "append-a", run_a["run_id"]))
        a_joined = "\n".join(
            message.content for message in agent_a.calls[0] if isinstance(message, HumanMessage)
        )
        self.assertLess(a_joined.index("第一条追加"), a_joined.index("第二条追加"))
        self.assertEqual(a_joined.count("追加指令"), 2)

    def test_queued_append_is_marked_not_applied_when_run_finishes_before_next_model_call(self):
        session_id = "not-applied-session"
        started = asyncio.Event()
        appended = asyncio.Event()
        run_id_holder = {}

        async def fake_stream_agent_events(_agent, _message, _session_id, _history):
            run_id_holder["run_id"] = runtime_tools._current_run_id()
            started.set()
            await asyncio.wait_for(appended.wait(), timeout=1)
            yield {"event": "done", "data": json.dumps("done", ensure_ascii=False)}

        async def fake_get_agent():
            return object()

        async def run_request():
            with patch("backend.main.get_session_store", return_value=self.store), patch(
                "backend.main.get_agent", fake_get_agent
            ), patch("backend.main.stream_agent_events", fake_stream_agent_events):
                task = asyncio.create_task(
                    main_module.chat_sync(JsonRequest({"message": "任务", "session_id": session_id}))
                )
                await asyncio.wait_for(started.wait(), timeout=1)
                append_response = self._client().post(
                    f"/sessions/{session_id}/runs/current/append",
                    json={"content": "最后补充", "run_id": run_id_holder["run_id"]},
                )
                appended.set()
                response = await task
                return append_response, response

        append_response, response = asyncio.run(run_request())

        self.assertEqual(append_response.status_code, 200)
        self.assertEqual(response.status_code, 200)
        progress = self.store.load_session(session_id)["task_progress"]
        self.assertEqual(progress["append_commands"][0]["status"], "not_applied")
