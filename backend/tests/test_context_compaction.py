import copy
import asyncio
import json
import shutil
import unittest
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from backend.config import load_context_compaction_config
from backend.agent import _compact_history_if_needed, stream_agent_events
from backend.context_compaction import (
    COMPACTION_SUMMARY_SECTIONS,
    ContextCompactionError,
    append_literal_ledger,
    build_compaction_cache,
    canonical_history_fingerprint,
    chunk_history_items,
    compact_history_with_llm,
    extract_exact_literals,
    partition_history,
    is_compaction_cache_usable,
    should_compact_context,
    validate_summary_text,
)
from backend.session_store import SessionStore


class _Response:
    def __init__(self, content):
        self.content = content


class _FakeLlm:
    def __init__(self, responses=None, error=None, errors=None):
        self.responses = list(responses or [])
        self.error = error
        self.errors = list(errors or [])
        self.calls = []

    def invoke(self, messages):
        self.calls.append(messages)
        if self.errors:
            raise self.errors.pop(0)
        if self.error:
            raise self.error
        response = self.responses.pop(0) if self.responses else ""
        return _Response(response)


class _CapturingAgent:
    def __init__(self):
        self.calls = []

    async def astream_events(self, values, *, config, version):
        self.calls.append(values["messages"])
        if False:
            yield None


class _MemoryCompactionStore:
    def __init__(self, cache=None):
        self.cache = cache
        self.saved = []

    def get_resume_context(self, _session_id):
        return {}

    def get_context_compaction(self, _session_id):
        return self.cache

    def save_context_compaction(self, _session_id, cache):
        self.cache = dict(cache)
        self.saved.append(dict(cache))
        return {"context_compaction": self.cache}


def _summary(literals):
    literal_text = ", ".join(literals)
    return (
        "## Facts and conclusions\n"
        f"Historical facts: {literal_text}\n\n"
        "## Files and artifacts\n"
        f"Referenced artifacts: {literal_text}\n\n"
        "## Unfinished items\n"
        "No unfinished items recorded.\n\n"
        "## Tool evidence\n"
        f"Evidence literals: {literal_text}"
    )


class ContextCompactionConfigurationTests(unittest.TestCase):
    @patch("backend.config.load_app_config")
    def test_defaults_are_backward_compatible_and_exact(self, mock_load):
        mock_load.return_value = {"model": "test-model"}

        config = load_context_compaction_config()

        self.assertTrue(config["enabled"])
        self.assertEqual(config["trigger_remaining_tokens"], 20_000)
        self.assertEqual(config["retain_recent_turns"], 3)
        self.assertGreater(config["summary_max_output_tokens"], 0)
        self.assertGreater(config["chunk_target_tokens"], 0)

    @patch("backend.config.load_app_config")
    def test_invalid_overrides_fall_back_to_defaults(self, mock_load):
        mock_load.return_value = {
            "context_compaction": {
                "enabled": "yes",
                "trigger_remaining_tokens": -1,
                "retain_recent_turns": 0,
                "summary_max_output_tokens": "invalid",
            }
        }

        config = load_context_compaction_config()

        self.assertTrue(config["enabled"])
        self.assertEqual(config["trigger_remaining_tokens"], 20_000)
        self.assertEqual(config["retain_recent_turns"], 3)

    def test_threshold_is_inclusive_and_unknown_windows_skip(self):
        self.assertTrue(should_compact_context(100_000, 120_000, 20_000))
        self.assertTrue(should_compact_context(120_000, 120_000, 20_000))
        self.assertFalse(should_compact_context(99_999, 120_000, 20_000))
        self.assertFalse(should_compact_context(10, None, 20_000))


class ContextCompactionHistoryTests(unittest.TestCase):
    def test_partition_keeps_three_newest_completed_turns_and_does_not_mutate(self):
        history = []
        for index in range(4):
            history.extend(
                [
                    {"role": "user", "content": f"question-{index}"},
                    {
                        "role": "historical_tool_context",
                        "content": {"number": index, "nested": [index]},
                    },
                    {"role": "assistant", "content": f"answer-{index}"},
                ]
            )
        original = copy.deepcopy(history)

        partition = partition_history(history, retain_recent_turns=3)

        self.assertEqual(len(partition.completed_turns), 4)
        self.assertEqual(
            [item["role"] for item in partition.eligible_items],
            ["user", "historical_tool_context", "assistant"],
        )
        self.assertEqual(
            [item["content"] for item in partition.protected_items if item["role"] == "user"],
            ["question-1", "question-2", "question-3"],
        )
        self.assertEqual(history, original)

    def test_incomplete_user_does_not_consume_protected_turn_slot(self):
        history = [{"role": "user", "content": "unfinished"}]
        for index in range(3):
            history.extend(
                [
                    {"role": "user", "content": f"question-{index}"},
                    {"role": "assistant", "content": f"answer-{index}"},
                ]
            )

        partition = partition_history(history, retain_recent_turns=3)

        self.assertEqual(len(partition.completed_turns), 3)
        self.assertEqual(partition.eligible_items, [])
        self.assertEqual(partition.protected_items[0]["content"], "unfinished")

    def test_incomplete_user_between_turns_stays_in_protected_projection(self):
        history = [
            {"role": "user", "content": "request zero"},
            {"role": "assistant", "content": "answer zero"},
            {"role": "user", "content": "unfinished request"},
        ]
        for index in range(1, 4):
            history.extend(
                [
                    {"role": "user", "content": f"request {index}"},
                    {"role": "assistant", "content": f"answer {index}"},
                ]
            )

        partition = partition_history(history, retain_recent_turns=3)

        self.assertEqual(len(partition.completed_turns), 4)
        self.assertEqual(
            [item["content"] for item in partition.eligible_items],
            ["request zero", "answer zero"],
        )
        self.assertEqual(partition.protected_items[0]["content"], "unfinished request")

    def test_exact_literals_are_generic_and_ordered(self):
        source = [
            {
                "role": "assistant",
                "content": (
                    "Use 0.125 on 2026-08-03 with frontend/src/App.jsx; "
                    "failure ERR-42, run-7, https://host.invalid/path"
                ),
            }
        ]

        literals = extract_exact_literals(source)

        for expected in (
            "0.125",
            "2026-08-03",
            "frontend/src/App.jsx",
            "ERR-42",
            "run-7",
            "https://host.invalid/path",
        ):
            self.assertIn(expected, literals)
        self.assertEqual(len(literals), len(set(literals)))

    def test_fingerprint_is_stable_for_equivalent_structures(self):
        first = [{"role": "user", "content": "value", "metadata": {"b": 2, "a": 1}}]
        second = [{"metadata": {"a": 1, "b": 2}, "content": "value", "role": "user"}]

        self.assertEqual(canonical_history_fingerprint(first), canonical_history_fingerprint(second))

    def test_chunking_preserves_all_items_and_order(self):
        items = [{"role": "user", "content": f"item-{index} " * 20} for index in range(5)]

        chunks = chunk_history_items(items, target_tokens=20)

        self.assertGreater(len(chunks), 1)
        self.assertEqual(
            [item["content"] for chunk in chunks for item in chunk],
            [item["content"] for item in items],
        )

    def test_chunking_keeps_turn_and_native_tool_boundaries(self):
        items = [
            {"role": "user", "content": "question " * 25},
            {
                "role": "assistant_tool_calls",
                "tool_calls": [{"id": "call-a", "name": "lookup", "args": {"query": "value"}}],
            },
            {"role": "tool", "tool_call_id": "call-a", "name": "lookup", "content": "result " * 25},
            {"role": "assistant", "content": "answer " * 25},
            {"role": "user", "content": "second question " * 25},
            {"role": "assistant", "content": "second answer " * 25},
        ]

        chunks = chunk_history_items(items, target_tokens=20)

        role_sequences = [[item["role"] for item in chunk] for chunk in chunks]
        self.assertIn(
            ["user", "assistant_tool_calls", "tool", "assistant"],
            role_sequences,
        )
        self.assertIn(["user", "assistant"], role_sequences)


class ContextCompactionSummaryTests(unittest.TestCase):
    def test_summary_requires_sections_and_exact_literals(self):
        literal = "frontend/src/App.jsx"
        valid = _summary([literal])

        validate_summary_text(valid, [literal], max_output_tokens=500)
        with self.assertRaises(ContextCompactionError):
            validate_summary_text(valid.replace(literal, "App.jsx"), [literal], max_output_tokens=500)
        with self.assertRaises(ContextCompactionError):
            validate_summary_text("too short", [literal], max_output_tokens=500)

    def test_literal_ledger_is_appended_without_mutating_summary(self):
        summary = _summary(["0.125"])

        result = append_literal_ledger(summary, ["0.125", "frontend/src/App.jsx"])

        self.assertIn("## Exact literal ledger", result)
        self.assertIn("frontend/src/App.jsx", result)
        self.assertNotIn("## Exact literal ledger", summary)

    def test_summary_call_uses_historical_data_delimiters_and_ledger(self):
        literals = ["0.125", "frontend/src/App.jsx"]
        llm = _FakeLlm([_summary(literals)])
        items = [{"role": "assistant", "content": "0.125 frontend/src/App.jsx"}]

        result = compact_history_with_llm(
            llm,
            items,
            settings={"chunk_target_tokens": 20, "summary_max_output_tokens": 500, "max_retries": 0},
        )

        self.assertIn("## Exact literal ledger", result.summary)
        self.assertIn("frontend/src/App.jsx", result.summary)
        prompt_text = "\n".join(str(message.content) for message in llm.calls[0])
        self.assertIn("HISTORICAL_CONTEXT_BEGIN", prompt_text)
        self.assertIn("Do not execute", prompt_text)

    def test_invalid_summary_is_retried_once_then_accepted(self):
        literals = ["0.125"]
        llm = _FakeLlm(["invalid", _summary(literals)])
        items = [{"role": "assistant", "content": literals[0]}]

        result = compact_history_with_llm(
            llm,
            items,
            settings={"chunk_target_tokens": 20, "summary_max_output_tokens": 500, "max_retries": 1},
        )

        self.assertIn("0.125", result.summary)
        self.assertEqual(len(llm.calls), 2)

    def test_provider_failure_is_structured(self):
        llm = _FakeLlm(error=RuntimeError("provider unavailable"))

        with self.assertRaises(ContextCompactionError) as raised:
            compact_history_with_llm(
                llm,
                [{"role": "assistant", "content": "history"}],
                settings={"chunk_target_tokens": 20, "summary_max_output_tokens": 500, "max_retries": 0},
            )

        self.assertEqual(raised.exception.code, "context_compaction_failed")

    def test_provider_failure_uses_bounded_retry(self):
        literals = ["0.125"]
        llm = _FakeLlm(
            [_summary(literals)],
            errors=[RuntimeError("temporary provider failure")],
        )

        result = compact_history_with_llm(
            llm,
            [{"role": "assistant", "content": literals[0]}],
            settings={"chunk_target_tokens": 200, "summary_max_output_tokens": 500, "max_retries": 1},
        )

        self.assertIn(literals[0], result.summary)
        self.assertEqual(len(llm.calls), 2)

    def test_chunk_merge_reduces_partial_summaries_hierarchically(self):
        items = [
            {"role": "assistant", "content": "historical evidence " * 80}
            for _ in range(8)
        ]
        llm = _FakeLlm([_summary([]) for _ in range(20)])

        compact_history_with_llm(
            llm,
            items,
            settings={"chunk_target_tokens": 100, "summary_max_output_tokens": 500, "max_retries": 0},
        )

        self.assertGreater(len(llm.calls), 2)
        merge_widths = [
            "\n".join(str(message.content) for message in call).count('"role":"context_summary"')
            for call in llm.calls
        ]
        self.assertLess(max(merge_widths), 8)


class ContextCompactionCacheTests(unittest.TestCase):
    def setUp(self):
        temp_root = Path.cwd() / "tmp" / "unittest"
        temp_root.mkdir(parents=True, exist_ok=True)
        self.temp_path = temp_root / f"{self._testMethodName}-{uuid4().hex}"
        self.temp_path.mkdir(parents=True, exist_ok=False)
        self.store = SessionStore(self.temp_path)

    def tearDown(self):
        shutil.rmtree(self.temp_path, ignore_errors=True)

    def test_cache_provenance_matches_full_or_extended_history(self):
        source = [
            {"role": "user", "content": "old-1"},
            {"role": "assistant", "content": "answer-1"},
        ]
        result = compact_history_with_llm(
            _FakeLlm([_summary(["old-1", "answer-1", "1"])]),
            source,
            settings={"chunk_target_tokens": 200, "summary_max_output_tokens": 500, "max_retries": 0},
        )
        cache = build_compaction_cache(result, model_name="test-model", boundary_index=2)

        self.assertTrue(is_compaction_cache_usable(cache, source, model_name="test-model"))
        self.assertTrue(
            is_compaction_cache_usable(
                cache,
                source + [{"role": "user", "content": "newly eligible"}],
                model_name="test-model",
                allow_extended_source=True,
            )
        )
        self.assertFalse(is_compaction_cache_usable(cache, source, model_name="other-model"))

    def test_cache_records_estimates_and_rejects_stale_boundary(self):
        source = [
            {"role": "user", "content": "old history"},
            {"role": "assistant", "content": "old answer"},
        ]
        result = compact_history_with_llm(
            _FakeLlm([_summary([])]),
            source,
            settings={"chunk_target_tokens": 200, "summary_max_output_tokens": 500, "max_retries": 0},
        )
        cache = build_compaction_cache(
            result,
            model_name="test-model",
            boundary_index=0,
            context_token_estimate_before=90_000,
            context_token_estimate_after=12_000,
        )

        self.assertEqual(cache["context_token_estimate_before"], 90_000)
        self.assertEqual(cache["context_token_estimate_after"], 12_000)
        self.assertTrue(is_compaction_cache_usable(cache, source, model_name="test-model", expected_boundary_index=0))
        self.assertFalse(is_compaction_cache_usable(cache, source, model_name="test-model", expected_boundary_index=2))

    def test_rolling_cache_merges_newly_eligible_source(self):
        initial_history = [
            {"role": "user", "content": "request one"},
            {"role": "assistant", "content": "answer one"},
            {"role": "user", "content": "request two"},
            {"role": "assistant", "content": "answer two"},
            {"role": "user", "content": "request three"},
            {"role": "assistant", "content": "answer three"},
            {"role": "user", "content": "request four"},
            {"role": "assistant", "content": "answer four"},
        ]
        extended_history = initial_history + [
            {"role": "user", "content": "request five"},
            {"role": "assistant", "content": "answer five"},
        ]
        llm = _FakeLlm([_summary([]), _summary([])])

        async def run():
            first, first_details = await _compact_history_if_needed(
                initial_history,
                session_id="rolling-session",
                model_config={"model": "test-model"},
                model_context_window=100_000,
                context_token_estimate=90_000,
                settings={
                    "enabled": True,
                    "trigger_remaining_tokens": 20_000,
                    "retain_recent_turns": 3,
                    "summary_max_output_tokens": 500,
                    "chunk_target_tokens": 500,
                    "max_retries": 0,
                },
            )
            second, second_details = await _compact_history_if_needed(
                extended_history,
                session_id="rolling-session",
                model_config={"model": "test-model"},
                model_context_window=100_000,
                context_token_estimate=90_000,
                settings={
                    "enabled": True,
                    "trigger_remaining_tokens": 20_000,
                    "retain_recent_turns": 3,
                    "summary_max_output_tokens": 500,
                    "chunk_target_tokens": 500,
                    "max_retries": 0,
                },
            )
            return first, first_details, second, second_details

        with patch("backend.agent.SessionStore", return_value=self.store), patch(
            "backend.agent.create_chat_deepseek", return_value=llm
        ):
            first, first_details, second, second_details = asyncio.run(run())

        self.assertTrue(first_details["triggered"])
        self.assertTrue(second_details["triggered"])
        self.assertEqual(len(llm.calls), 2)
        second_prompt = "\n".join(str(message.content) for message in llm.calls[1])
        self.assertIn("request two", second_prompt)
        self.assertEqual(self.store.get_context_compaction("rolling-session")["covered_item_count"], 4)
        self.assertIn("request five", "\n".join(str(item) for item in second))
        self.assertFalse(second_details["cache_hit"])

    def test_cache_source_change_forces_regeneration(self):
        history = [
            {"role": "user", "content": "request one"},
            {"role": "assistant", "content": "answer one"},
            {"role": "user", "content": "request two"},
            {"role": "assistant", "content": "answer two"},
            {"role": "user", "content": "request three"},
            {"role": "assistant", "content": "answer three"},
            {"role": "user", "content": "request four"},
            {"role": "assistant", "content": "answer four"},
        ]
        changed_history = [dict(item) for item in history]
        changed_history[0]["content"] = "changed request one"
        llm = _FakeLlm([_summary([]), _summary([])])

        async def run():
            await _compact_history_if_needed(
                history,
                session_id="stale-session",
                model_config={"model": "test-model"},
                model_context_window=100_000,
                context_token_estimate=90_000,
                settings={"summary_max_output_tokens": 500, "chunk_target_tokens": 500, "max_retries": 0},
            )
            return await _compact_history_if_needed(
                changed_history,
                session_id="stale-session",
                model_config={"model": "test-model"},
                model_context_window=100_000,
                context_token_estimate=90_000,
                settings={"summary_max_output_tokens": 500, "chunk_target_tokens": 500, "max_retries": 0},
            )

        with patch("backend.agent.SessionStore", return_value=self.store), patch(
            "backend.agent.create_chat_deepseek", return_value=llm
        ):
            asyncio.run(run())

        self.assertEqual(len(llm.calls), 2)

    def test_concurrent_compaction_serializes_generation_and_commit(self):
        history = [
            {"role": "user", "content": "request one"},
            {"role": "assistant", "content": "answer one"},
            {"role": "user", "content": "request two"},
            {"role": "assistant", "content": "answer two"},
            {"role": "user", "content": "request three"},
            {"role": "assistant", "content": "answer three"},
            {"role": "user", "content": "request four"},
            {"role": "assistant", "content": "answer four"},
        ]
        llm = _FakeLlm([_summary([])])

        async def run():
            return await asyncio.gather(
                *(
                    _compact_history_if_needed(
                        history,
                        session_id="concurrent-session",
                        model_config={"model": "test-model"},
                        model_context_window=100_000,
                        context_token_estimate=90_000,
                        settings={"summary_max_output_tokens": 500, "chunk_target_tokens": 500, "max_retries": 0},
                    )
                    for _ in range(2)
                )
            )

        with patch("backend.agent.SessionStore", return_value=self.store), patch(
            "backend.agent.create_chat_deepseek", return_value=llm
        ), patch.object(self.store, "save_context_compaction", wraps=self.store.save_context_compaction) as save_cache:
            results = asyncio.run(run())

        self.assertEqual(len(llm.calls), 1)
        self.assertEqual(save_cache.call_count, 1)
        self.assertEqual(results[0][1]["cache_hit"], False)
        self.assertEqual(results[1][1]["cache_hit"], True)

    def test_session_store_writes_cache_without_changing_canonical_messages(self):
        self.store.append_message("cache-session", "user", "original request")
        before = self.store.load_session("cache-session")["messages"]
        cache = {
            "schema_version": 1,
            "summary_prompt_version": "context-compaction-v1",
            "model_name": "test-model",
            "summary": "summary",
            "source_fingerprint": "fingerprint",
            "covered_item_count": 0,
            "covered_turn_count": 0,
        }

        self.store.save_context_compaction("cache-session", cache)

        loaded = self.store.load_session("cache-session")
        self.assertEqual(loaded["messages"], before)
        self.assertEqual(self.store.get_context_compaction("cache-session")["summary"], "summary")

    def test_failed_cache_commit_preserves_last_valid_cache(self):
        initial_cache = {
            "schema_version": 1,
            "summary_prompt_version": "context-compaction-v1",
            "model_name": "test-model",
            "summary": "initial summary",
        }
        replacement_cache = {**initial_cache, "summary": "replacement summary"}
        self.store.save_context_compaction("atomic-session", initial_cache)

        with patch.object(
            self.store,
            "_save_context_compaction_atomically",
            side_effect=OSError("disk unavailable"),
        ), self.assertRaises(OSError):
            self.store.save_context_compaction("atomic-session", replacement_cache)

        self.assertEqual(
            self.store.get_context_compaction("atomic-session")["summary"],
            "initial summary",
        )


class ContextCompactionAgentIntegrationTests(unittest.TestCase):
    def _history_with_four_turns(self):
        history = []
        for index in range(4):
            history.extend(
                [
                    {"role": "user", "content": "old request" if index == 0 else f"recent request {index}"},
                    {"role": "assistant", "content": "old answer" if index == 0 else f"recent answer {index}"},
                ]
            )
        return history

    def _run(
        self,
        *,
        summary_llm,
        post_exceeded=False,
        pre_remaining=9_999,
        history=None,
        unknown_window=False,
        enabled=True,
        memory_store=None,
    ):
        agent = _CapturingAgent()
        memory_store = memory_store or _MemoryCompactionStore()
        history = history if history is not None else self._history_with_four_turns()
        pre_window = None if unknown_window else 100_000
        pre_estimate = 0 if unknown_window else pre_window - pre_remaining
        capacity_values = [
            {
                "model_context_window": pre_window,
                "context_token_estimate": pre_estimate,
                "remaining_tokens": None if unknown_window else pre_remaining,
                "is_exceeded": False,
            },
            {
                "model_context_window": pre_window,
                "context_token_estimate": 10_000,
                "remaining_tokens": None if unknown_window else 90_000,
                "is_exceeded": post_exceeded,
            },
        ]
        context_metrics = [
            {
                "history_payloads": [],
                "protected_tool_chars": 0,
                "context_char_count": 100,
                "context_message_count": 10,
                "context_token_estimate": pre_estimate,
            },
            {
                "history_payloads": [],
                "protected_tool_chars": 0,
                "context_char_count": 100,
                "context_message_count": 5,
                "context_token_estimate": 10_000,
            },
        ]

        async def consume():
            return [
                event
                async for event in stream_agent_events(
                    agent,
                    "current request",
                    "compaction-integration",
                    history,
                )
            ]

        with patch("backend.agent.SessionStore", return_value=memory_store), patch(
            "backend.agent.load_llm_config",
            return_value={"model": "test-model", "base_url": "", "api_key": ""},
        ), patch(
            "backend.agent.load_context_compaction_config",
            return_value={
                "enabled": enabled,
                "trigger_remaining_tokens": 20_000,
                "retain_recent_turns": 3,
                "summary_max_output_tokens": 500,
                "chunk_target_tokens": 500,
                "max_retries": 0,
            },
        ), patch(
            "backend.agent.check_context_capacity",
            side_effect=capacity_values,
        ), patch(
            "backend.agent._estimate_context_metrics",
            side_effect=context_metrics,
        ), patch(
            "backend.agent.get_skill_catalog_text",
            return_value="",
        ), patch(
            "backend.agent.create_chat_deepseek",
            return_value=summary_llm,
        ):
            events = asyncio.run(consume())
        return agent, memory_store, events

    def test_successful_compaction_sends_summary_and_recent_turns_only(self):
        summary_llm = _FakeLlm([_summary([])])

        agent, memory_store, events = self._run(summary_llm=summary_llm)

        self.assertEqual(len(agent.calls), 1)
        message_text = "\n".join(str(message.content) for message in agent.calls[0])
        self.assertIn("Facts and conclusions", message_text)
        self.assertNotIn("old answer", message_text)
        self.assertIn("recent request 1", message_text)
        self.assertIn("recent request 3", message_text)
        stages = [
            json.loads(event["data"]).get("stage")
            for event in events
            if event.get("event") == "debug"
        ]
        self.assertIn("context_compaction_started", stages)
        self.assertIn("context_compaction_completed", stages)
        self.assertEqual(len(memory_store.saved), 1)

    def test_compaction_provider_failure_does_not_call_primary_agent(self):
        summary_llm = _FakeLlm(error=RuntimeError("summary unavailable"))

        agent, memory_store, events = self._run(summary_llm=summary_llm)

        self.assertEqual(len(agent.calls), 0)
        self.assertEqual(memory_store.saved, [])
        errors = [
            json.loads(event["data"])
            for event in events
            if event.get("event") == "error"
        ]
        self.assertEqual(errors[-1]["code"], "context_compaction_failed")

    def test_post_compaction_overflow_does_not_call_primary_agent(self):
        summary_llm = _FakeLlm([_summary([])])

        agent, _memory_store, events = self._run(summary_llm=summary_llm, post_exceeded=True)

        self.assertEqual(len(agent.calls), 0)
        errors = [
            json.loads(event["data"])
            for event in events
            if event.get("event") == "error"
        ]
        self.assertEqual(errors[-1]["code"], "context_compaction_capacity_exceeded")

    def test_reused_cache_emits_reuse_event_without_second_summary_call(self):
        summary_llm = _FakeLlm([_summary([])])
        memory_store = _MemoryCompactionStore()

        self._run(summary_llm=summary_llm, memory_store=memory_store)
        _agent, _store, events = self._run(summary_llm=summary_llm, memory_store=memory_store)

        stages = [
            json.loads(event["data"]).get("stage")
            for event in events
            if event.get("event") == "debug"
        ]
        self.assertEqual(len(summary_llm.calls), 1)
        self.assertIn("context_compaction_reused", stages)

    def test_exact_threshold_triggers_compaction(self):
        summary_llm = _FakeLlm([_summary([])])

        agent, _memory_store, _events = self._run(
            summary_llm=summary_llm,
            pre_remaining=20_000,
        )

        self.assertEqual(len(summary_llm.calls), 1)
        self.assertEqual(len(agent.calls), 1)

    def test_above_threshold_skips_compaction(self):
        summary_llm = _FakeLlm([_summary([])])

        agent, _memory_store, events = self._run(
            summary_llm=summary_llm,
            pre_remaining=20_001,
        )

        self.assertEqual(summary_llm.calls, [])
        self.assertEqual(len(agent.calls), 1)
        stages = [
            json.loads(event["data"]).get("stage")
            for event in events
            if event.get("event") == "debug"
        ]
        self.assertNotIn("context_compaction_started", stages)

    def test_unknown_window_skips_compaction_and_preserves_model_call(self):
        summary_llm = _FakeLlm([_summary([])])

        agent, _memory_store, events = self._run(
            summary_llm=summary_llm,
            unknown_window=True,
        )

        self.assertEqual(summary_llm.calls, [])
        self.assertEqual(len(agent.calls), 1)
        stages = [
            json.loads(event["data"]).get("stage")
            for event in events
            if event.get("event") == "debug"
        ]
        self.assertIn("skipped_unknown_context_window", stages)

    def test_no_eligible_history_skips_compaction(self):
        history = []
        for index in range(3):
            history.extend(
                [
                    {"role": "user", "content": f"request {index}"},
                    {"role": "assistant", "content": f"answer {index}"},
                ]
            )
        summary_llm = _FakeLlm([_summary([])])

        agent, _memory_store, _events = self._run(
            summary_llm=summary_llm,
            history=history,
        )

        self.assertEqual(summary_llm.calls, [])
        self.assertEqual(len(agent.calls), 1)


if __name__ == "__main__":
    unittest.main()
