import asyncio
import json
import unittest
from unittest.mock import patch

from pydantic import BaseModel

from backend import tools as runtime_tools
from backend.runtime_context import bind_runtime_context, reset_runtime_context


class _GenericArgs(BaseModel):
    path: str = ""
    text: str = ""
    query: str = ""


class _FakeTool:
    def __init__(self, name: str):
        self.name = name
        self.description = "fake tool"
        self.args_schema = _GenericArgs
        self.calls = 0

    def invoke(self, kwargs):
        raise NotImplementedError

    async def ainvoke(self, kwargs):
        return self.invoke(kwargs)


class _CapturingStore:
    def __init__(self):
        self.events = []

    def get_tool_result_cache_entry(self, session_id, call_key):
        return None

    def append_tool_event(self, session_id, **kwargs):
        self.events.append({"session_id": session_id, **kwargs})


class FailureClassificationWiringTests(unittest.TestCase):
    def test_failure_result_gains_classification_fields(self):
        tool = _FakeTool("probe_tool")
        tool.invoke = lambda _kwargs: json.dumps({"status": "permission_denied", "error": "denied"})
        wrapped = runtime_tools._wrap_tool_with_run_dedupe(tool)
        tokens = bind_runtime_context("cls-session", "cls-run")
        try:
            result = json.loads(wrapped.invoke({"path": "a"}))
        finally:
            reset_runtime_context(tokens)

        self.assertEqual(result["error_category"], "permission_denied")
        self.assertFalse(result["retryable"])

    def test_exception_failure_carries_classification_attributes(self):
        tool = _FakeTool("probe_tool")

        def boom(_kwargs):
            raise TimeoutError("late")

        tool.invoke = boom
        wrapped = runtime_tools._wrap_tool_with_run_dedupe(tool)
        tokens = bind_runtime_context("cls-session", "cls-run-2")
        try:
            with self.assertRaises(TimeoutError) as ctx:
                wrapped.invoke({"path": "a"})
        finally:
            reset_runtime_context(tokens)

        self.assertEqual(getattr(ctx.exception, "error_category", ""), "timeout")
        self.assertTrue(getattr(ctx.exception, "retryable", False))


class FailureFingerprintHintTests(unittest.TestCase):
    def setUp(self):
        self._token = bind_runtime_context("fp-session", "fp-run")

    def tearDown(self):
        runtime_tools.clear_tool_dedupe_cache("fp-run")
        reset_runtime_context(self._token)

    def test_second_identical_exception_failure_returns_soft_result_with_hint(self):
        tool = _FakeTool("probe_tool")

        def boom(_kwargs):
            raise RuntimeError("boom")

        tool.invoke = boom
        wrapped = runtime_tools._wrap_tool_with_run_dedupe(tool)

        with self.assertRaises(RuntimeError):
            wrapped.invoke({"path": "same"})

        soft = json.loads(wrapped.invoke({"path": "same"}))
        self.assertEqual(soft["status"], "command_failed")
        self.assertIn("retry_hint", soft)
        self.assertEqual(soft["error_category"], "unknown")
        self.assertTrue(soft["retryable"])

        third = json.loads(wrapped.invoke({"path": "same"}))
        self.assertNotIn("retry_hint", third)

    def test_second_structured_failure_appends_hint_once(self):
        body = json.dumps({"status": "command_failed", "error": "nope"})

        def failing(_kwargs):
            return body

        tool = _FakeTool("probe_tool")
        tool.invoke = failing
        wrapped = runtime_tools._wrap_tool_with_run_dedupe(tool)

        first = json.loads(wrapped.invoke({"path": "same"}))
        self.assertNotIn("retry_hint", first)

        second = json.loads(wrapped.invoke({"path": "same"}))
        self.assertIn("retry_hint", second)

        third = json.loads(wrapped.invoke({"path": "same"}))
        self.assertNotIn("retry_hint", third)

    def test_success_resets_failure_count(self):
        tool = _FakeTool("probe_tool")
        state = {"fail": True}
        wrapped = runtime_tools._wrap_tool_with_run_dedupe(tool)

        def flaky(_kwargs):
            if state["fail"]:
                raise RuntimeError("boom")
            return json.dumps({"status": "success"})

        tool.invoke = flaky
        with self.assertRaises(RuntimeError):
            wrapped.invoke({"path": "same"})
        state["fail"] = False
        wrapped.invoke({"path": "same"})
        state["fail"] = True
        with self.assertRaises(RuntimeError):
            wrapped.invoke({"path": "same"})

    def test_async_second_failure_returns_hint(self):
        tool = _FakeTool("probe_tool")

        async def failing(_kwargs):
            raise RuntimeError("boom")

        async def ainvoke(kwargs):
            return await failing(kwargs)

        tool.ainvoke = ainvoke
        wrapped = runtime_tools._wrap_tool_with_run_dedupe(tool)

        async def scenario():
            with self.assertRaises(RuntimeError):
                await wrapped.ainvoke({"path": "same"})
            soft = json.loads(await wrapped.ainvoke({"path": "same"}))
            self.assertIn("retry_hint", soft)

        asyncio.run(scenario())


class ActionIdExposureTests(unittest.TestCase):
    def setUp(self):
        self.store = _CapturingStore()
        patcher = patch.object(runtime_tools, "_session_store_for_runtime", return_value=self.store)
        patcher.start()
        self.addCleanup(patcher.stop)
        self._token = bind_runtime_context("aid-session", "aid-run")

    def tearDown(self):
        runtime_tools.clear_tool_dedupe_cache("aid-run")
        reset_runtime_context(self._token)

    def test_side_effect_result_and_block_share_stable_action_id(self):
        tool = _FakeTool("write_report")
        tool.invoke = lambda _kwargs: json.dumps({"status": "success", "path": "out.md"})
        wrapped = runtime_tools._wrap_tool_with_run_dedupe(tool)

        first = json.loads(wrapped.invoke({"path": "out.md", "text": "body"}))
        self.assertTrue(first["action_id"].startswith("sha256:"))

        blocked = json.loads(wrapped.invoke({"path": "out.md", "text": "body"}))
        self.assertTrue(blocked.get("blocked"))
        self.assertEqual(blocked["action_id"], first["action_id"])
        self.assertFalse(blocked["retryable"])
        self.assertEqual(blocked["error_category"], "policy_denied")

    def test_async_side_effect_result_carries_action_id(self):
        tool = _FakeTool("write_report")
        tool.ainvoke = lambda kwargs: asyncio.sleep(0, result=json.dumps({"status": "success"}))
        wrapped = runtime_tools._wrap_tool_with_run_dedupe(tool)

        result = json.loads(asyncio.run(wrapped.ainvoke({"path": "b.md"})))
        self.assertTrue(result["action_id"].startswith("sha256:"))

    def test_audit_events_carry_action_id(self):
        tool = _FakeTool("write_report")
        tool.invoke = lambda _kwargs: json.dumps({"status": "success", "path": "c.md"})
        wrapped = runtime_tools._wrap_tool_with_run_dedupe(tool)

        executed = json.loads(wrapped.invoke({"path": "c.md"}))
        blocked = json.loads(wrapped.invoke({"path": "c.md"}))

        executed_events = [e for e in self.store.events if e["event_type"] == "tool_runtime_executed"]
        blocked_events = [e for e in self.store.events if e["event_type"] == "tool_runtime_blocked"]
        self.assertTrue(executed_events)
        self.assertTrue(blocked_events)
        self.assertEqual(executed_events[0]["content"]["action_id"], executed["action_id"])
        self.assertEqual(blocked_events[0]["content"]["action_id"], blocked["action_id"])

    def test_different_arguments_yield_different_action_ids(self):
        tool = _FakeTool("write_report")
        tool.invoke = lambda _kwargs: json.dumps({"status": "success"})
        wrapped = runtime_tools._wrap_tool_with_run_dedupe(tool)

        first = json.loads(wrapped.invoke({"path": "one.md"}))
        second = json.loads(wrapped.invoke({"path": "two.md"}))
        self.assertNotEqual(first["action_id"], second["action_id"])


if __name__ == "__main__":
    unittest.main()
