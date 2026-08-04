import os
import unittest
from pathlib import Path


@unittest.skipUnless(
    os.environ.get("FEISHU_KB_SMOKE_REFERENCE", "").strip(),
    "Set FEISHU_KB_SMOKE_REFERENCE to run the authenticated Feishu smoke test.",
)
class FeishuKnowledgeSmokeTests(unittest.TestCase):
    def test_authenticated_import_repeat_refresh_and_sync(self):
        from backend.rag.config import load_rag_config
        from backend.rag.service import PersonalKnowledgeBase

        workspace = Path.cwd()
        store = Path(os.environ.get("FEISHU_KB_SMOKE_STORE", "tmp/feishu_kb_smoke")).resolve()
        collection = os.environ.get("FEISHU_KB_SMOKE_COLLECTION", "smoke").strip() or "smoke"
        reference = os.environ["FEISHU_KB_SMOKE_REFERENCE"].strip()
        config = load_rag_config(
            {
                "enabled": True,
                "knowledge_store_path": str(store),
                "retrieval_profile": os.environ.get("FEISHU_KB_SMOKE_PROFILE", "production"),
            }
        )
        knowledge_base = PersonalKnowledgeBase(
            workspace_root=workspace,
            store_path=store,
            config=config,
        )

        first = knowledge_base.import_remote_document(collection, reference)
        self.assertIn(first.get("status"), {"indexed", "refreshed", "unchanged"})
        self.assertNotIn("content", first)

        repeated = knowledge_base.import_remote_document(collection, reference)
        self.assertEqual(repeated.get("status"), "unchanged")
        self.assertEqual(repeated.get("doc_id"), first.get("doc_id"))

        refreshed = knowledge_base.import_remote_document(collection, reference, refresh=True)
        self.assertIn(refreshed.get("status"), {"refreshed", "unchanged"})
        self.assertEqual(refreshed.get("doc_id"), first.get("doc_id"))

        report = knowledge_base.sync_remote_sources(
            provider_name=config.remote_source.provider,
            collection=collection,
        )
        self.assertIn("counts", report)
        self.assertIn("files", report)
        self.assertNotIn("content", report)


if __name__ == "__main__":
    unittest.main()
