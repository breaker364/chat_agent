from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


class AgentWorkspaceRootTests(unittest.TestCase):
    def test_build_agent_attaches_workspace_root_to_compiled_agent(self):
        from backend.agent import AppendAwareChatModel, build_agent
        from backend.config import load_workflow_config

        async def empty_tools(**_kwargs):
            return []

        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory).resolve()
            with patch("backend.agent.load_llm_config", return_value={}), patch(
                "backend.agent.create_chat_deepseek", return_value=object()
            ), patch(
                "backend.agent.wrap_chat_model_with_append_injection",
                side_effect=lambda model, **_kwargs: AppendAwareChatModel(model),
            ), patch("backend.agent.get_all_tools", empty_tools), patch(
                "backend.agent.build_agentic_research_runtime", return_value=None
            ), patch(
                "backend.agent.load_workflow_config", return_value=load_workflow_config({"enabled": False})
            ):
                agent = asyncio.run(build_agent(workspace_dir=workspace))

            self.assertEqual(Path(getattr(agent, "workspace_root")), workspace)

    def test_append_command_event_is_recorded_in_workspace_store(self):
        from backend.agent import _record_append_command_injected
        from backend.session_store import SessionStore

        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory).resolve()

            _record_append_command_injected(
                "ws-session",
                {"run_id": "run-1", "append_id": "append-1", "status": "injected", "sequence": 1},
                workspace_root=workspace,
            )

            session = SessionStore(workspace).create_or_get_session("ws-session")
            commands = (session.get("task_progress") or {}).get("append_commands") or []
            self.assertTrue(any(command.get("append_id") == "append-1" for command in commands))


if __name__ == "__main__":
    unittest.main()
