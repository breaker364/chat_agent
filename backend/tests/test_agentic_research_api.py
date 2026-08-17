from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


class JsonRequest:
    def __init__(self, body):
        self.body = body

    async def json(self):
        return self.body


class StreamRequest(JsonRequest):
    async def is_disconnected(self):
        return False


async def fake_stream_agent_events(_agent, _message, _session_id, _history, synthesis_context=""):
    yield {"event": "done", "data": json.dumps("grounded answer")}


def research_result():
    from backend.agentic_research.models import EvidenceAssessment, EvidenceObservation, ResearchTrace
    from backend.agentic_research.orchestrator import ResearchResult

    trace = ResearchTrace(policy="auto")
    trace.record(EvidenceObservation(
        source_kind="web",
        citations=[{"id": "source", "title": "Source", "uri": "https://example.invalid"}],
        excerpts=["bounded evidence"],
    ))
    trace.finish("answer_ready")
    return ResearchResult(
        trace=trace,
        observations=[EvidenceObservation(
            source_kind="web",
            citations=[{"id": "source", "title": "Source", "uri": "https://example.invalid"}],
            excerpts=["bounded evidence"],
        )],
        assessment=EvidenceAssessment("answer_ready", usable=True),
    )


class AgenticResearchApiTests(unittest.TestCase):
    def setUp(self):
        from backend.session_store import SessionStore

        self.temp_dir = tempfile.TemporaryDirectory()
        self.store = SessionStore(Path(self.temp_dir.name))

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_sync_lets_agent_own_research_route(self):
        from backend.main import chat_sync

        async def fake_get_agent():
            return object()

        with patch("backend.main.get_session_store", return_value=self.store), patch(
            "backend.main.get_agent", fake_get_agent
        ), patch("backend.main.stream_agent_events", fake_stream_agent_events):
            response = asyncio.run(chat_sync(JsonRequest({
                "message": "question",
                "session_id": "policy-sync",
                "knowledge_policy": "auto",
            })))

        payload = json.loads(response.body)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(payload["research"]["route_class"], "direct")
        self.assertEqual(payload["research"]["policy"], "auto")
        self.assertNotIn("planner_reasoning", json.dumps(payload))

    def test_stream_emits_bounded_route_after_agent_events(self):
        from backend.main import chat_stream

        async def fake_get_agent():
            return object()

        with patch("backend.main.get_session_store", return_value=self.store), patch(
            "backend.main.get_agent", fake_get_agent
        ), patch("backend.main.stream_agent_events", fake_stream_agent_events), patch(
            "backend.main.EventSourceResponse", lambda generator: generator
        ):
            generator = asyncio.run(chat_stream(StreamRequest({
                "message": "question",
                "session_id": "policy-stream",
                "knowledge_policy": "auto",
            })))
            events = asyncio.run(self._collect(generator))

        research_events = [event for event in events if event["event"] == "research"]
        self.assertEqual(len(research_events), 1)
        self.assertEqual(json.loads(research_events[0]["data"])["route_class"], "direct")
        self.assertNotIn("planner_reasoning", json.dumps(events))

    def test_conflicting_policy_is_rejected_before_agent_execution(self):
        from backend.main import chat_sync

        get_agent_calls = []

        async def fake_get_agent():
            get_agent_calls.append(True)
            return object()

        with patch("backend.main.get_session_store", return_value=self.store), patch(
            "backend.main.get_agent", fake_get_agent
        ):
            response = asyncio.run(chat_sync(JsonRequest({
                "message": "question",
                "session_id": "policy-error",
                "knowledge_mode": True,
                "knowledge_policy": "disabled",
            })))

        self.assertEqual(response.status_code, 400)
        self.assertEqual(json.loads(response.body)["error"]["code"], "invalid_request")
        self.assertEqual(get_agent_calls, [])

    async def _collect(self, generator):
        return [event async for event in generator]


if __name__ == "__main__":
    unittest.main()
