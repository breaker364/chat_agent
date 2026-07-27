import asyncio
import json
import shutil
import unittest
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from langchain_core.messages import AIMessage, ToolMessage

from backend.agent import _append_native_history_messages, stream_agent_events
from backend import tools as runtime_tools
from backend.main import get_session, chat_stream, chat_sync
from backend.session_store import SessionStore


class CapturingAgent:
    def __init__(self, events=None):
        self.events = events or []
        self.calls = []

    async def astream_events(self, values, *, config, version):
        self.calls.append(values["messages"])
        for event in self.events:
            yield event


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


class SequentialAgent:
    def __init__(self, event_runs=None, before_call=None):
        self.event_runs = [list(run) for run in (event_runs or [])]
        self.before_call = before_call
        self.calls = []

    def astream_events(self, values, *, config, version):
        call_number = len(self.calls) + 1
        if self.before_call:
            self.before_call(call_number)
        self.calls.append(values["messages"])
        events = self.event_runs.pop(0) if self.event_runs else []
        return _EventIterator(events)


class JsonRequest:
    def __init__(self, body):
        self.body = body

    async def json(self):
        return self.body


class StreamRequest(JsonRequest):
    async def is_disconnected(self):
        return False


def _tool_call(sequence, tool_call_id, name, arguments):
    return {
        "schema_version": 1,
        "type": "tool_call",
        "sequence": sequence,
        "tool_call_id": tool_call_id,
        "name": name,
        "arguments": arguments,
    }


def _tool_result(sequence, tool_call_id, name, content):
    return {
        "schema_version": 1,
        "type": "tool_result",
        "sequence": sequence,
        "tool_call_id": tool_call_id,
        "name": name,
        "content": content,
    }


class ToolHistoryTests(unittest.TestCase):
    def setUp(self):
        temp_root = Path.cwd() / "tmp" / "unittest"
        temp_root.mkdir(parents=True, exist_ok=True)
        self.temp_path = temp_root / f"{self._testMethodName}-{uuid4().hex}"
        self.temp_path.mkdir(parents=True, exist_ok=False)
        self.store = SessionStore(self.temp_path)

    def tearDown(self):
        shutil.rmtree(self.temp_path, ignore_errors=True)

    def _complete_turn(self, session_id, user_text, assistant_text, tools):
        self.store.append_message(session_id, "user", user_text)
        self.store.replace_last_assistant_message(session_id, assistant_text, tools=tools)

    def _capture_prompt_messages(self, history):
        agent = CapturingAgent()

        async def consume():
            async for _ in stream_agent_events(agent, "next request", "history-test", history):
                pass

        asyncio.run(consume())
        self.assertEqual(len(agent.calls), 1)
        return agent.calls[0]

    def test_tool_events_keep_full_payload_and_sequence_in_session_history(self):
        arguments = {"nested": {"payload": "a" * 9000}}
        content = "b" * 18000
        tools = [
            _tool_call(0, "call-1", "lookup", arguments),
            _tool_result(1, "call-1", "lookup", content),
        ]

        self._complete_turn("lossless", "request", "final response", tools)

        stored = self.store.load_session("lossless")
        self.assertEqual(stored["messages"][-1]["tools"], tools)
        history = self.store.get_history("lossless")
        tool_entries = [entry for entry in history if entry.get("role") in {"assistant_tool_calls", "tool"}]
        self.assertEqual([entry["sequence"] for entry in tool_entries], [0, 1])
        self.assertEqual(tool_entries[0]["tool_calls"][0]["args"], arguments)
        self.assertEqual(tool_entries[1]["content"], content)

    def test_stream_events_include_same_run_tool_call_id_for_call_and_result(self):
        agent = CapturingAgent(
            [
                {
                    "event": "on_tool_start",
                    "name": "lookup",
                    "run_id": "run-42",
                    "data": {"input": {"query": "complete input"}},
                },
                {
                    "event": "on_tool_end",
                    "name": "lookup",
                    "run_id": "run-42",
                    "data": {"output": "complete result"},
                },
            ]
        )

        async def collect():
            return [event async for event in stream_agent_events(agent, "request", "stream-id-test")]

        events = asyncio.run(collect())
        payloads = {
            event["event"]: json.loads(event["data"])
            for event in events
            if event["event"] in {"tool_call", "tool_result"}
        }
        self.assertEqual(payloads["tool_call"]["tool_call_id"], "run-42")
        self.assertEqual(payloads["tool_result"]["tool_call_id"], "run-42")

    def test_chat_persists_complete_arguments_and_duplicate_runtime_calls(self):
        complete_arguments = {
            "query": "same call",
            "runtime": {"value": "retain"},
            "config": {"value": "retain"},
            "callbacks": ["retain"],
            "store": {"value": "retain"},
            "context": {"value": "retain"},
            "state": {"value": "retain"},
        }
        agent = CapturingAgent(
            [
                {"event": "on_tool_start", "name": "lookup", "run_id": "call-a", "data": {"input": complete_arguments}},
                {"event": "on_tool_end", "name": "lookup", "run_id": "call-a", "data": {"output": "first"}},
                {"event": "on_tool_start", "name": "lookup", "run_id": "call-b", "data": {"input": complete_arguments}},
                {"event": "on_tool_end", "name": "lookup", "run_id": "call-b", "data": {"output": "second"}},
            ]
        )

        async def fake_get_agent():
            return agent

        request = JsonRequest({"message": "request", "session_id": "duplicate-transcript"})
        with patch("backend.main.get_session_store", return_value=self.store), patch("backend.main.get_agent", fake_get_agent):
            asyncio.run(chat_sync(request))

        stored_tools = self.store.load_session("duplicate-transcript")["messages"][-1]["tools"]
        self.assertEqual([event["sequence"] for event in stored_tools], [0, 1, 2, 3])
        self.assertEqual([event["tool_call_id"] for event in stored_tools], ["call-a", "call-a", "call-b", "call-b"])
        self.assertEqual([event["arguments"] for event in stored_tools if event["type"] == "tool_call"], [complete_arguments, complete_arguments])
        self.assertEqual([event["content"] for event in stored_tools if event["type"] == "tool_result"], ["first", "second"])

    def test_concurrent_chat_runs_keep_runtime_context_isolated_by_session(self):
        arrivals = []
        release = asyncio.Event()

        async def fake_stream_agent_events(_agent, message, session_id, _history):
            arrivals.append(session_id)
            if len(arrivals) == 2:
                release.set()
            await asyncio.wait_for(release.wait(), timeout=1)
            observed_session_id = runtime_tools._current_session_id()
            observed_run_id = runtime_tools._current_run_id()
            yield {
                "event": "done",
                "data": json.dumps(
                    {
                        "message": f"{message}:{observed_session_id}",
                        "observed_session_id": observed_session_id,
                        "observed_run_id": observed_run_id,
                    },
                    ensure_ascii=False,
                ),
            }

        async def fake_get_agent():
            return object()

        async def run_concurrently():
            with patch("backend.main.get_session_store", return_value=self.store), patch("backend.main.get_agent", fake_get_agent), patch("backend.main.stream_agent_events", fake_stream_agent_events):
                first = asyncio.create_task(chat_sync(JsonRequest({"message": "first", "session_id": "parallel-a"})))
                second = asyncio.create_task(chat_sync(JsonRequest({"message": "second", "session_id": "parallel-b"})))
                return await asyncio.gather(first, second)

        responses = asyncio.run(run_concurrently())

        payloads = [json.loads(response.body) for response in responses]
        self.assertEqual({payload["session_id"] for payload in payloads}, {"parallel-a", "parallel-b"})
        self.assertIn("first:parallel-a", self.store.load_session("parallel-a")["messages"][-1]["content"])
        self.assertIn("second:parallel-b", self.store.load_session("parallel-b")["messages"][-1]["content"])
        self.assertEqual(runtime_tools._current_session_id(), "")
        self.assertEqual(runtime_tools._current_run_id(), "")

    def test_chat_rejects_second_foreground_run_for_same_session_without_appending_message(self):
        first_started = asyncio.Event()
        release_first = asyncio.Event()
        calls = []

        async def fake_stream_agent_events(_agent, message, session_id, _history):
            calls.append((session_id, message))
            if len(calls) == 1:
                first_started.set()
                await asyncio.wait_for(release_first.wait(), timeout=1)
            yield {"event": "done", "data": json.dumps(f"done:{message}", ensure_ascii=False)}

        async def fake_get_agent():
            return object()

        async def run_duplicate_requests():
            with patch("backend.main.get_session_store", return_value=self.store), patch("backend.main.get_agent", fake_get_agent), patch("backend.main.stream_agent_events", fake_stream_agent_events):
                first = asyncio.create_task(chat_sync(JsonRequest({"message": "first", "session_id": "same-session"})))
                await asyncio.wait_for(first_started.wait(), timeout=1)
                second_response = await chat_sync(JsonRequest({"message": "second", "session_id": "same-session"}))
                release_first.set()
                first_response = await first
                return first_response, second_response

        first_response, second_response = asyncio.run(run_duplicate_requests())

        self.assertEqual(first_response.status_code, 200)
        self.assertEqual(second_response.status_code, 409)
        second_payload = json.loads(second_response.body)
        self.assertEqual(second_payload["error"], "session_run_active")
        session = self.store.load_session("same-session")
        self.assertEqual([message["role"] for message in session["messages"]], ["user", "assistant"])
        self.assertEqual(session["messages"][0]["content"], "first")
        self.assertIn("done:first", session["messages"][1]["content"])

    def test_chat_allows_same_session_run_after_prior_run_finishes(self):
        calls = []

        async def fake_stream_agent_events(_agent, message, _session_id, _history):
            calls.append(message)
            yield {"event": "done", "data": json.dumps(f"done:{message}", ensure_ascii=False)}

        async def fake_get_agent():
            return object()

        async def run_requests():
            with patch("backend.main.get_session_store", return_value=self.store), patch("backend.main.get_agent", fake_get_agent), patch("backend.main.stream_agent_events", fake_stream_agent_events):
                first = await chat_sync(JsonRequest({"message": "first", "session_id": "repeat-session"}))
                second = await chat_sync(JsonRequest({"message": "second", "session_id": "repeat-session"}))
                return first, second

        first_response, second_response = asyncio.run(run_requests())

        self.assertEqual(first_response.status_code, 200)
        self.assertEqual(second_response.status_code, 200)
        self.assertEqual(calls, ["first", "second"])

    def test_chat_clears_per_run_dedupe_cache_after_sync_run(self):
        observed_run_ids = []

        async def fake_stream_agent_events(_agent, _message, _session_id, _history):
            run_id = runtime_tools._current_run_id()
            observed_run_ids.append(run_id)
            runtime_tools._TOOL_DEDUPE_CACHE[run_id] = {"call": "result"}
            yield {"event": "done", "data": json.dumps("done", ensure_ascii=False)}

        async def fake_get_agent():
            return object()

        request = JsonRequest({"message": "request", "session_id": "dedupe-cleanup"})
        with patch("backend.main.get_session_store", return_value=self.store), patch("backend.main.get_agent", fake_get_agent), patch("backend.main.stream_agent_events", fake_stream_agent_events):
            response = asyncio.run(chat_sync(request))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(observed_run_ids), 1)
        self.assertNotIn(observed_run_ids[0], runtime_tools._TOOL_DEDUPE_CACHE)

    def test_chat_stream_adds_session_and_run_attribution_to_structured_events(self):
        async def fake_stream_agent_events(_agent, _message, _session_id, _history):
            yield {
                "event": "progress",
                "data": json.dumps({"message": "working"}, ensure_ascii=False),
            }
            yield {"event": "done", "data": json.dumps("final", ensure_ascii=False)}

        async def fake_get_agent():
            return object()

        request = StreamRequest({"message": "request", "session_id": "attributed-stream"})
        with patch("backend.main.get_session_store", return_value=self.store), patch("backend.main.get_agent", fake_get_agent), patch("backend.main.stream_agent_events", fake_stream_agent_events), patch("backend.main.EventSourceResponse", lambda generator: generator):
            generator = asyncio.run(chat_stream(request))

            async def consume():
                return [event async for event in generator]

            events = asyncio.run(consume())

        progress_payload = json.loads(next(event["data"] for event in events if event["event"] == "progress"))
        self.assertEqual(progress_payload["session_id"], "attributed-stream")
        self.assertTrue(progress_payload["run_id"].startswith("attributed-stream:"))
        self.assertEqual(progress_payload["message"], "working")

    def test_chat_stream_hides_private_tool_transcript_events_from_clients(self):
        complete_arguments = {"query": "same call", "runtime": {"secret": "retain"}}
        agent = CapturingAgent(
            [
                {"event": "on_tool_start", "name": "lookup", "run_id": "call-a", "data": {"input": complete_arguments}},
                {"event": "on_tool_end", "name": "lookup", "run_id": "call-a", "data": {"output": "result"}},
                {"event": "on_tool_start", "name": "lookup", "run_id": "call-b", "data": {"input": complete_arguments}},
                {"event": "on_tool_end", "name": "lookup", "run_id": "call-b", "data": {"output": "result"}},
            ]
        )

        async def fake_get_agent():
            return agent

        request = StreamRequest({"message": "request", "session_id": "stream-private-events"})
        with patch("backend.main.get_session_store", return_value=self.store), patch("backend.main.get_agent", fake_get_agent), patch("backend.main.EventSourceResponse", lambda generator: generator):
            generator = asyncio.run(chat_stream(request))

            async def consume():
                return [event async for event in generator]

            events = asyncio.run(consume())

        event_types = [event["event"] for event in events]
        public_calls = [json.loads(event["data"]) for event in events if event["event"] == "tool_call"]
        self.assertNotIn("tool_transcript_call", event_types)
        self.assertNotIn("tool_transcript_result", event_types)
        self.assertEqual(len(public_calls), 1)
        self.assertNotIn("runtime", public_calls[0]["arguments"])
        self.assertNotIn("retain", json.dumps(events, ensure_ascii=False))

    def test_session_api_redacts_private_transcript_arguments_without_mutating_storage(self):
        private_arguments = {
            "query": "visible",
            "runtime": {"value": "private"},
            "config": {"value": "private"},
            "callbacks": ["private"],
            "store": {"value": "private"},
            "context": {"value": "private"},
            "state": {"value": "private"},
        }
        self._complete_turn(
            "projected-session",
            "question",
            "answer",
            [_tool_call(0, "call-1", "lookup", private_arguments), _tool_result(1, "call-1", "lookup", "result")],
        )

        with patch("backend.main.get_session_store", return_value=self.store):
            response = asyncio.run(get_session("projected-session"))

        payload = json.loads(response.body)
        public_arguments = payload["messages"][-1]["tools"][0]["arguments"]
        self.assertEqual(public_arguments["query"], "visible")
        self.assertFalse({"runtime", "config", "callbacks", "store", "context", "state"}.intersection(public_arguments))
        stored_arguments = self.store.load_session("projected-session")["messages"][-1]["tools"][0]["arguments"]
        self.assertEqual(stored_arguments, private_arguments)

    def test_malformed_tool_call_arguments_become_diagnostics_without_native_call_crash(self):
        for arguments in (None, "not-an-object", ["not-an-object"], object()):
            messages = []
            history = [
                {
                    "role": "assistant_tool_calls",
                    "tool_calls": [{"name": "lookup", "args": arguments, "id": "bad-call", "type": "tool_call"}],
                }
            ]
            try:
                _append_native_history_messages(messages, history)
            except Exception as exc:
                self.fail(f"Malformed arguments raised {type(exc).__name__}: {exc}")
            self.assertFalse(any(isinstance(message, AIMessage) and message.tool_calls for message in messages))
            self.assertIn("malformed_tool_call", messages[0].content)

    def test_capacity_overflow_marks_sync_turn_failed(self):
        tools = [_tool_call(0, "call-1", "lookup", {"query": "x" * 100}), _tool_result(1, "call-1", "lookup", "y" * 100)]
        self._complete_turn("overflow-sync", "question", "answer", tools)
        agent = CapturingAgent()

        async def fake_get_agent():
            return agent

        with patch("backend.main.get_session_store", return_value=self.store), patch("backend.main.get_agent", fake_get_agent), patch("backend.agent.MAX_HISTORY_TOTAL_CHARS", 50):
            response = asyncio.run(chat_sync(JsonRequest({"message": "next request", "session_id": "overflow-sync"})))

        payload = json.loads(response.body)
        session = self.store.load_session("overflow-sync")
        self.assertIn("Protected tool history exceeds", payload["reply"])
        self.assertEqual(agent.calls, [])
        self.assertEqual(session["task_progress"]["execution_summary"]["status"], "failed")
        self.assertEqual(session["task_progress"]["status"], "failed")

    def test_capacity_overflow_marks_stream_turn_failed(self):
        tools = [_tool_call(0, "call-1", "lookup", {"query": "x" * 100}), _tool_result(1, "call-1", "lookup", "y" * 100)]
        self._complete_turn("overflow-stream", "question", "answer", tools)
        agent = CapturingAgent()

        async def fake_get_agent():
            return agent

        with patch("backend.main.get_session_store", return_value=self.store), patch("backend.main.get_agent", fake_get_agent), patch("backend.main.EventSourceResponse", lambda generator: generator), patch("backend.agent.MAX_HISTORY_TOTAL_CHARS", 50):
            generator = asyncio.run(chat_stream(StreamRequest({"message": "next request", "session_id": "overflow-stream"})))

            async def consume():
                return [event async for event in generator]

            events = asyncio.run(consume())

        session = self.store.load_session("overflow-stream")
        self.assertTrue(any(event["event"] == "error" for event in events))
        self.assertEqual(agent.calls, [])
        self.assertEqual(session["task_progress"]["execution_summary"]["status"], "failed")
        self.assertEqual(session["task_progress"]["status"], "failed")

    def test_only_latest_three_turns_rehydrate_native_tool_messages_in_event_order(self):
        for turn in range(1, 5):
            tool_call_id = f"turn-{turn}"
            tools = [
                _tool_call(0, tool_call_id, "lookup", {"turn": turn}),
                _tool_result(1, tool_call_id, "lookup", f"result-{turn}"),
            ]
            if turn == 4:
                tools = [
                    _tool_call(0, "turn-4-first", "lookup", {"turn": 4, "order": 1}),
                    _tool_call(1, "turn-4-second", "lookup", {"turn": 4, "order": 2}),
                    _tool_result(2, "turn-4-second", "lookup", "result-4-second"),
                    _tool_result(3, "turn-4-first", "lookup", "result-4-first"),
                ]
            self._complete_turn(
                "window",
                f"question-{turn}",
                f"answer-{turn}",
                tools,
            )

        messages = self._capture_prompt_messages(self.store.get_history("window"))
        tool_messages = [message for message in messages if isinstance(message, ToolMessage)]
        call_messages = [
            message for message in messages
            if isinstance(message, AIMessage) and message.tool_calls
        ]
        assistant_text = [message.content for message in messages if isinstance(message, AIMessage) and not message.tool_calls]

        self.assertEqual(
            [message.tool_call_id for message in tool_messages],
            ["turn-2", "turn-3", "turn-4-second", "turn-4-first"],
        )
        self.assertEqual(
            [tool_call["id"] for message in call_messages for tool_call in message.tool_calls],
            ["turn-2", "turn-3", "turn-4-first", "turn-4-second"],
        )
        self.assertTrue(all(f"answer-{turn}" in assistant_text for turn in range(1, 5)))

    def test_legacy_same_name_events_use_fifo_ids_and_keep_unmatched_result_visible(self):
        legacy_tools = [
            {"type": "tool_call", "name": "lookup", "arguments": {"index": 1}},
            {"type": "tool_call", "name": "lookup", "arguments": {"index": 2}},
            {"type": "tool_result", "name": "lookup", "content": "first"},
            {"type": "tool_result", "name": "lookup", "content": "second"},
            {"type": "tool_result", "name": "lookup", "content": "unmatched"},
        ]
        self._complete_turn("legacy", "question", "answer", legacy_tools)

        history = self.store.get_history("legacy")
        calls = [entry for entry in history if entry.get("role") == "assistant_tool_calls"]
        results = [entry for entry in history if entry.get("role") == "tool"]
        unmatched = [entry for entry in history if entry.get("role") == "legacy_tool_result"]

        call_ids = [tool_call["id"] for call in calls for tool_call in call["tool_calls"]]
        self.assertEqual(len(call_ids), 2)
        self.assertEqual([entry["tool_call_id"] for entry in results], call_ids)
        self.assertEqual([entry["content"] for entry in results], ["first", "second"])
        self.assertEqual(unmatched[0]["content"], "unmatched")
        self.assertTrue(unmatched[0]["legacy_unmatched"])

    def test_protected_tool_context_overflow_stops_before_model_invocation(self):
        tools = [
            _tool_call(0, "call-1", "lookup", {"query": "x" * 100}),
            _tool_result(1, "call-1", "lookup", "y" * 100),
        ]
        self._complete_turn("overflow", "question", "answer", tools)
        agent = CapturingAgent()

        async def collect():
            with patch("backend.agent.MAX_HISTORY_TOTAL_CHARS", 50):
                return [event async for event in stream_agent_events(agent, "next request", "overflow", self.store.get_history("overflow"))]

        events = asyncio.run(collect())
        errors = [json.loads(event["data"]) for event in events if event["event"] == "error"]
        self.assertEqual(agent.calls, [])
        self.assertEqual(errors[0]["code"], "protected_context_capacity_exceeded")

    def test_get_history_deduplicates_identical_protected_tool_pairs_without_mutating_storage(self):
        arguments = {"query": "same"}
        tools = [
            _tool_call(0, "call-a", "lookup", arguments),
            _tool_result(1, "call-a", "lookup", "same result"),
            _tool_call(2, "call-b", "lookup", {"query": "same"}),
            _tool_result(3, "call-b", "lookup", "same result"),
        ]
        self._complete_turn("dedupe-history", "question", "answer", tools)

        history = self.store.get_history("dedupe-history")
        stored_tools = self.store.load_session("dedupe-history")["messages"][-1]["tools"]
        calls = [entry for entry in history if entry.get("role") == "assistant_tool_calls"]
        results = [entry for entry in history if entry.get("role") == "tool"]

        self.assertEqual(stored_tools, tools)
        self.assertEqual([call["id"] for entry in calls for call in entry["tool_calls"]], ["call-a"])
        self.assertEqual([entry["tool_call_id"] for entry in results], ["call-a"])
        self.assertEqual([entry["content"] for entry in results], ["same result"])

    def test_get_history_keeps_repeated_tool_pairs_when_results_differ(self):
        tools = [
            _tool_call(0, "call-a", "lookup", {"query": "same"}),
            _tool_result(1, "call-a", "lookup", "first result"),
            _tool_call(2, "call-b", "lookup", {"query": "same"}),
            _tool_result(3, "call-b", "lookup", "second result"),
        ]
        self._complete_turn("dedupe-different-results", "question", "answer", tools)

        history = self.store.get_history("dedupe-different-results")
        calls = [entry for entry in history if entry.get("role") == "assistant_tool_calls"]
        results = [entry for entry in history if entry.get("role") == "tool"]

        self.assertEqual([call["id"] for entry in calls for call in entry["tool_calls"]], ["call-a", "call-b"])
        self.assertEqual([entry["tool_call_id"] for entry in results], ["call-a", "call-b"])
        self.assertEqual([entry["content"] for entry in results], ["first result", "second result"])

    def test_unmatched_protected_tool_call_does_not_rehydrate_native_tool_call(self):
        tools = [_tool_call(0, "dangling-call", "lookup", {"query": "missing result"})]
        self._complete_turn("dangling-history", "question", "answer", tools)

        history = self.store.get_history("dangling-history")
        messages = self._capture_prompt_messages(history)
        native_tool_calls = [
            tool_call
            for message in messages
            if isinstance(message, AIMessage) and message.tool_calls
            for tool_call in message.tool_calls
        ]
        diagnostics = [
            message.content
            for message in messages
            if isinstance(message, AIMessage) and not message.tool_calls
        ]

        self.assertEqual(self.store.load_session("dangling-history")["messages"][-1]["tools"], tools)
        self.assertEqual(native_tool_calls, [])
        self.assertTrue(any("dangling_tool_call" in str(content) for content in diagnostics))

    def test_retry_repair_removes_dangling_tool_calls_before_reinvocation(self):
        class RaisingIterator:
            def __aiter__(self):
                return self

            async def __anext__(self):
                raise RuntimeError("invalid chat history")

        class MutatingRetryAgent:
            def __init__(self):
                self.calls = []

            def astream_events(self, values, *, config, version):
                self.calls.append(list(values["messages"]))
                if len(self.calls) == 1:
                    values["messages"].append(
                        AIMessage(
                            content="",
                            tool_calls=[
                                {
                                    "name": "lookup",
                                    "args": {"query": "dangling"},
                                    "id": "dangling-retry-call",
                                    "type": "tool_call",
                                }
                            ],
                        )
                    )
                    return RaisingIterator()
                return _EventIterator(
                    [
                        {"event": "on_chat_model_stream", "name": "model", "data": {"chunk": AIMessage(content="final answer")}},
                        {"event": "on_chain_end", "name": "LangGraph", "data": {}},
                    ]
                )

        def dangling_tool_call_ids(messages):
            remaining = []
            for message in messages:
                if isinstance(message, AIMessage) and message.tool_calls:
                    remaining.extend(tool_call["id"] for tool_call in message.tool_calls)
                elif isinstance(message, ToolMessage) and message.tool_call_id in remaining:
                    remaining.remove(message.tool_call_id)
            return remaining

        class CompleteAuditor:
            def invoke(self, messages):
                return AIMessage(content=json.dumps({"complete": True, "reason": "done", "status": "completed"}))

        agent = MutatingRetryAgent()

        async def collect():
            with patch("backend.agent.load_llm_config", return_value={}), patch("backend.agent.create_chat_deepseek", return_value=CompleteAuditor()):
                return [event async for event in stream_agent_events(agent, "request", "retry-repair")]

        asyncio.run(collect())

        self.assertGreaterEqual(len(agent.calls), 2)
        self.assertEqual(dangling_tool_call_ids(agent.calls[1]), [])

    def test_run_loop_continues_before_completion_audit_when_no_final_answer(self):
        audit_calls = {"count": 0}

        class CompleteAuditor:
            def invoke(self, messages):
                return AIMessage(content=json.dumps({
                    "complete": True,
                    "reason": "test auditor says complete",
                    "required_next_action": "none",
                    "status": "completed",
                }))

        def fake_create_chat_deepseek(*args, **kwargs):
            audit_calls["count"] += 1
            return CompleteAuditor()

        def before_call(call_number):
            if call_number == 2:
                self.assertEqual(audit_calls["count"], 0)

        agent = SequentialAgent(
            [
                [{"event": "on_chain_end", "name": "LangGraph", "data": {}}],
                [
                    {"event": "on_chat_model_stream", "name": "model", "data": {"chunk": AIMessage(content="final answer")}},
                    {"event": "on_chain_end", "name": "LangGraph", "data": {}},
                ],
            ],
            before_call=before_call,
        )

        async def collect():
            with patch("backend.agent.load_llm_config", return_value={}), patch("backend.agent.create_chat_deepseek", fake_create_chat_deepseek):
                return [event async for event in stream_agent_events(agent, "request", "continue-before-audit")]

        events = asyncio.run(collect())
        debug_stages = [json.loads(event["data"]).get("stage") for event in events if event["event"] == "debug"]

        self.assertEqual(len(agent.calls), 2)
        self.assertIn("agent_tool_use_continuation", debug_stages)
        self.assertGreaterEqual(audit_calls["count"], 1)

    def test_run_loop_forces_final_when_plan_complete_and_primary_result_registered(self):
        session_id = "force-final-primary-result"
        self.store.set_task_plan(
            session_id,
            [
                {
                    "task_id": "create_output",
                    "content": "Create output",
                    "activeForm": "Creating output",
                    "status": "completed",
                    "details": "Output created",
                    "result_ref": "tmp/result.md",
                }
            ],
            source="model",
            reason="test",
        )
        self.store.record_primary_result(
            session_id,
            {
                "type": "file",
                "title": "Result",
                "path": "tmp/result.md",
                "summary": "Primary result is ready.",
                "source_tool": "record_primary_result",
            },
        )

        agent = SequentialAgent(
            [
                [
                    {"event": "on_tool_start", "name": "record_primary_result", "run_id": "primary-result-call", "data": {"input": {"path": "tmp/result.md"}}},
                    {
                        "event": "on_tool_end",
                        "name": "record_primary_result",
                        "run_id": "primary-result-call",
                        "data": {"output": json.dumps({"success": True, "primary_result": {"path": "tmp/result.md"}}, ensure_ascii=False)},
                    },
                    {"event": "on_chain_end", "name": "LangGraph", "data": {}},
                ],
                [
                    {"event": "on_chat_model_stream", "name": "model", "data": {"chunk": AIMessage(content="should not continue")}},
                    {"event": "on_chain_end", "name": "LangGraph", "data": {}},
                ],
            ]
        )

        async def collect():
            with patch("backend.agent.SessionStore", return_value=self.store), patch(
                "backend.agent.create_chat_deepseek",
                side_effect=AssertionError("completion audit should not run after forced finalization"),
            ):
                return [event async for event in stream_agent_events(agent, "request", session_id)]

        events = asyncio.run(collect())
        done_text = json.loads(next(event["data"] for event in events if event["event"] == "done"))
        debug_stages = [json.loads(event["data"]).get("stage") for event in events if event["event"] == "debug"]

        self.assertEqual(len(agent.calls), 1)
        self.assertIn("forced_final_after_completion_gate", debug_stages)
        self.assertIn("tmp/result.md", done_text)
        self.assertIn("Primary result is ready.", done_text)

    def test_run_loop_does_not_continue_after_usable_final_answer(self):
        class CompleteAuditor:
            def invoke(self, messages):
                return AIMessage(content=json.dumps({
                    "complete": True,
                    "reason": "final answer exists",
                    "required_next_action": "none",
                    "status": "completed",
                }))

        agent = SequentialAgent(
            [
                [
                    {"event": "on_chat_model_stream", "name": "model", "data": {"chunk": AIMessage(content="final answer")}},
                    {"event": "on_chain_end", "name": "LangGraph", "data": {}},
                ]
            ]
        )

        async def collect():
            with patch("backend.agent.load_llm_config", return_value={}), patch("backend.agent.create_chat_deepseek", return_value=CompleteAuditor()):
                return [event async for event in stream_agent_events(agent, "request", "final-answer-stop")]

        events = asyncio.run(collect())
        done_events = [event for event in events if event["event"] == "done"]

        self.assertEqual(len(agent.calls), 1)
        self.assertEqual(len(done_events), 1)
