import asyncio
import json
import unittest
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from backend.agent import (
    analyze_exception_for_user,
    build_activity_event,
    build_exception_analysis_context,
    format_exception_analysis,
)


class ExceptionAnalysisContextTests(unittest.TestCase):
    def test_exception_analysis_context_is_safe_and_bounded(self):
        context = build_exception_analysis_context(
            exc_text="RuntimeError: service failed\ntraceback line\nsecret-token",
            active_tool="web_search",
            tool_call_history=[
                {"tool": "web_search", "arguments": {"query": "safe query", "authorization": "secret-token"}},
            ],
            tool_errors=[{"tool": "web_search", "message": "HTTP 500 authorization=secret-token"}],
            latest_activity={"summary": "正在查询公开资料", "token": "secret-token"},
            partial_response="x" * 2000,
        )

        rendered = str(context)
        self.assertEqual(context["active_tool"], "web_search")
        self.assertLessEqual(len(context["partial_response"]), 800)
        self.assertNotIn("authorization", rendered.lower())
        self.assertNotIn("secret-token", rendered)
        self.assertNotIn("traceback line", rendered)

    def test_valid_agent_analysis_is_used_for_user_message(self):
        async def invoke_analysis(_context):
            return json.dumps({
                "what_happened": "资料查询在返回结果前中断。",
                "likely_cause": "查询服务返回了临时错误。",
                "completion_status": "尚未获得最终结果。",
                "next_step": "稍后重试查询。",
            })

        analysis = asyncio.run(analyze_exception_for_user({"exception_summary": "RuntimeError: failed"}, invoke_analysis))

        self.assertIsNotNone(analysis)
        message = format_exception_analysis(analysis)
        self.assertIn("资料查询在返回结果前中断", message)
        self.assertIn("稍后重试查询", message)

    def test_invalid_or_unsafe_agent_analysis_is_rejected(self):
        async def invoke_analysis(_context):
            return '{"what_happened": "Traceback: secret-token"}'

        analysis = asyncio.run(analyze_exception_for_user({"exception_summary": "RuntimeError: failed"}, invoke_analysis))

        self.assertIsNone(analysis)

    def test_activity_event_contains_safe_user_facing_state(self):
        event = build_activity_event(
            state="working",
            summary="正在查询资料，authorization=secret-token",
            elapsed_seconds=4,
            evidence="已确认需要查询公开资料",
            next_step="等待查询结果",
            progress={"current": 2, "total": 3, "label": "查询资料"},
        )

        self.assertEqual(event["state"], "working")
        self.assertEqual(event["elapsed_seconds"], 4)
        self.assertEqual(event["progress"]["current"], 2)
        self.assertNotIn("authorization", event["summary"].lower())
        self.assertNotIn("secret-token", event["summary"])
