import shutil
import unittest
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4


def _load_tool_symbols():
    try:
        from backend.rag.tools import build_knowledge_tools
    except ModuleNotFoundError as exc:
        raise AssertionError(f"RAG tools module missing: {exc}") from exc
    return build_knowledge_tools


class RagToolTests(unittest.TestCase):
    def setUp(self):
        temp_root = Path.cwd() / "tmp" / "unittest"
        temp_root.mkdir(parents=True, exist_ok=True)
        self.temp_dir = temp_root / f"{self._testMethodName}-{uuid4().hex}"
        self.temp_dir.mkdir(parents=True, exist_ok=False)

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_knowledge_tools_are_hidden_when_feature_disabled(self):
        build_knowledge_tools = _load_tool_symbols()

        tools = build_knowledge_tools(
            workspace_root=self.temp_dir,
            config_overrides={
                "enabled": False,
                "retrieval_profile": "deterministic",
                "knowledge_store_path": str(self.temp_dir / "store"),
            },
        )

        self.assertEqual(tools, [])

    def test_knowledge_tools_are_typed_and_entity_agnostic_when_enabled(self):
        build_knowledge_tools = _load_tool_symbols()

        tools = build_knowledge_tools(
            workspace_root=self.temp_dir,
            config_overrides={
                "enabled": True,
                "retrieval_profile": "deterministic",
                "knowledge_store_path": str(self.temp_dir / "store"),
            },
        )

        names = {tool.name for tool in tools}
        self.assertIn("knowledge_index_files", names)
        self.assertIn("knowledge_search", names)
        self.assertIn("knowledge_evaluate", names)
        self.assertIn("knowledge_download_models", names)
        schema_text = "\n".join(str(getattr(tool, "args_schema", "")) for tool in tools)
        forbidden = ["official_domain", "brand_name", "school_name", "company_name"]
        self.assertFalse(any(term in schema_text for term in forbidden))

    def test_agent_tool_registration_respects_feature_flag(self):
        try:
            from backend import tools as runtime_tools
        except ModuleNotFoundError as exc:
            raise AssertionError(f"runtime tools module missing: {exc}") from exc

        async def no_external_tools(*_args, **_kwargs):
            return []

        with patch.object(runtime_tools, "load_12306_tools", side_effect=no_external_tools), patch.object(
            runtime_tools, "load_mcd_tools", side_effect=no_external_tools
        ), patch.object(runtime_tools, "build_skill_tools", return_value=[]):
            disabled = runtime_tools._run_coro_in_thread(
                runtime_tools.get_all_tools(
                    workspace_dir=self.temp_dir,
                    rag_config_overrides={
                        "enabled": False,
                        "retrieval_profile": "deterministic",
                        "knowledge_store_path": str(self.temp_dir / "store"),
                    },
                )
            )
            enabled = runtime_tools._run_coro_in_thread(
                runtime_tools.get_all_tools(
                    workspace_dir=self.temp_dir,
                    rag_config_overrides={
                        "enabled": True,
                        "retrieval_profile": "deterministic",
                        "knowledge_store_path": str(self.temp_dir / "store"),
                    },
                )
            )

        self.assertNotIn("knowledge_search", {tool.name for tool in disabled})
        self.assertIn("knowledge_search", {tool.name for tool in enabled})

    def test_prompt_policy_instructs_personal_knowledge_grounding(self):
        system_prompt = Path("backend/prompts/system_prompt.md").read_text(encoding="utf-8")
        agent_policy = Path("backend/prompts/agent_policy.md").read_text(encoding="utf-8")
        combined = f"{system_prompt}\n{agent_policy}".lower()

        self.assertIn("personal knowledge", combined)
        self.assertIn("knowledge_search", combined)
        self.assertIn("cite", combined)
        self.assertIn("not contain enough evidence", combined)


if __name__ == "__main__":
    unittest.main()
