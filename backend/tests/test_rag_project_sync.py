import json
import shutil
import types
import unittest
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from fastapi.testclient import TestClient


def _load_symbols():
    from backend.rag.config import load_rag_config
    from backend.rag.service import PersonalKnowledgeBase
    from backend.rag.tools import build_knowledge_tools

    return load_rag_config, PersonalKnowledgeBase, build_knowledge_tools


class RagProjectSyncTests(unittest.TestCase):
    def setUp(self):
        temp_root = Path.cwd() / "tmp" / "unittest"
        temp_root.mkdir(parents=True, exist_ok=True)
        self.temp_dir = temp_root / f"{self._testMethodName}-{uuid4().hex}"
        self.temp_dir.mkdir(parents=True, exist_ok=False)
        self.workspace = self.temp_dir / "workspace"
        self.workspace.mkdir()
        self.store_dir = self.workspace / "knowledge_base"

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_default_config_uses_project_knowledge_base_layout(self):
        load_rag_config, _, _ = _load_symbols()

        config = load_rag_config({"enabled": True})

        self.assertEqual(config.knowledge_store_path.name, "knowledge_base")
        self.assertEqual(config.documents_path, config.knowledge_store_path / "documents")
        self.assertEqual(config.index_path, config.knowledge_store_path / "index")
        self.assertEqual(config.manifests_path, config.knowledge_store_path / "manifests")
        self.assertEqual(config.sync_reports_path, config.knowledge_store_path / "reports")

    def test_project_sync_indexes_new_files_skips_unsupported_then_removes_deleted_vectors(self):
        _, PersonalKnowledgeBase, _ = _load_symbols()
        kb = PersonalKnowledgeBase(workspace_root=self.workspace, store_path=self.store_dir)
        collection_dir = kb.documents_path / "notes"
        collection_dir.mkdir(parents=True)
        source = collection_dir / "guide.md"
        source.write_text("# Guide\nalpha exact sync token", encoding="utf-8")
        unsupported = collection_dir / "archive.bin"
        unsupported.write_bytes(b"\x00\x01")

        first = kb.sync()

        self.assertEqual(first["counts"]["indexed"], 1)
        self.assertEqual(first["counts"]["skipped"], 1)
        self.assertTrue(kb.search("sync token", collection="notes")["results"])
        self.assertTrue((kb.index_path / "chunks.json").exists())
        self.assertTrue((kb.manifests_path / "manifests.json").exists())

        second = kb.sync()

        self.assertEqual(second["counts"]["indexed"], 0)
        self.assertEqual(second["counts"]["unchanged"], 1)

        source.unlink()
        third = kb.sync()

        self.assertEqual(third["counts"]["deleted"], 1)
        self.assertEqual(kb.search("sync token", collection="notes")["results"], [])

    def test_frontend_import_copies_file_to_collection_documents_without_indexing_until_sync(self):
        _, PersonalKnowledgeBase, _ = _load_symbols()
        incoming_dir = self.workspace / "incoming"
        incoming_dir.mkdir()
        source = incoming_dir / "plan.md"
        source.write_text("# Plan\nimported knowledge token", encoding="utf-8")
        kb = PersonalKnowledgeBase(workspace_root=self.workspace, store_path=self.store_dir)

        imported = kb.import_files("team notes", [source])

        self.assertEqual(imported["counts"]["imported"], 1)
        target = Path(imported["imported"][0]["knowledge_path"])
        self.assertEqual(target.parent, kb.documents_path / "team_notes")
        self.assertTrue(target.exists())
        self.assertEqual(kb.list_documents("team_notes"), [])

        synced = kb.sync(collection="team_notes")

        self.assertEqual(synced["counts"]["indexed"], 1)
        self.assertTrue(kb.search("imported token", collection="team_notes")["results"])

    def test_pdf_import_and_sync_extracts_text_when_reader_is_available(self):
        _, PersonalKnowledgeBase, _ = _load_symbols()

        class FakePage:
            def extract_text(self):
                return "PDF knowledge token for direct indexing"

        fake_pypdf = types.SimpleNamespace(
            PdfReader=lambda _handle: types.SimpleNamespace(pages=[FakePage()])
        )
        kb = PersonalKnowledgeBase(workspace_root=self.workspace, store_path=self.store_dir)

        with patch.dict("sys.modules", {"pypdf": fake_pypdf}):
            imported = kb.import_file_bytes("pdf notes", "guide.pdf", b"%PDF-1.4 fake")
            self.assertEqual(imported["counts"]["imported"], 1)
            self.assertEqual(imported["imported"][0]["status"], "pending_sync")

            synced = kb.sync(collection="pdf_notes")

        self.assertEqual(synced["counts"]["indexed"], 1)
        results = kb.search("PDF knowledge token", collection="pdf_notes")["results"]
        self.assertTrue(results)
        self.assertEqual(results[0]["source_type"], "pdf")

    def test_unicode_pdf_import_preserves_extension_before_sync(self):
        _, PersonalKnowledgeBase, _ = _load_symbols()
        kb = PersonalKnowledgeBase(workspace_root=self.workspace, store_path=self.store_dir)

        imported = kb.import_file_bytes("pdf notes", "资料文档.pdf", b"%PDF-1.4 fake")

        self.assertEqual(imported["counts"]["imported"], 1)
        self.assertTrue(imported["imported"][0]["knowledge_path"].endswith(".pdf"))

    def test_source_file_listing_includes_indexed_pending_and_unsupported_files(self):
        _, PersonalKnowledgeBase, _ = _load_symbols()
        kb = PersonalKnowledgeBase(workspace_root=self.workspace, store_path=self.store_dir)
        collection_dir = kb.documents_path / "notes"
        collection_dir.mkdir(parents=True)
        indexed = collection_dir / "indexed.md"
        pending = collection_dir / "pending.md"
        unsupported = collection_dir / "archive.bin"
        indexed.write_text("# Indexed\nindexed source token", encoding="utf-8")
        pending.write_text("# Pending\npending source token", encoding="utf-8")
        unsupported.write_bytes(b"\x00\x01")

        kb.sync(collection="notes")
        pending.write_text("# Pending\nchanged but not synced", encoding="utf-8")
        sources = kb.list_source_files(collection="notes")
        by_name = {Path(item["source_uri"]).name: item for item in sources}

        self.assertEqual(by_name["indexed.md"]["status"], "indexed")
        self.assertEqual(by_name["pending.md"]["status"], "pending_sync")
        self.assertEqual(by_name["archive.bin"]["status"], "unsupported")
        self.assertEqual(by_name["indexed.md"]["chunk_count"], 1)

    def test_sync_and_import_tools_are_registered_and_callable(self):
        _, _, build_knowledge_tools = _load_symbols()
        source = self.workspace / "source.md"
        source.write_text("tool import sync token", encoding="utf-8")

        tools = {
            tool.name: tool
            for tool in build_knowledge_tools(
                workspace_root=self.workspace,
                config_overrides={"enabled": True, "knowledge_store_path": str(self.store_dir)},
            )
        }

        self.assertIn("knowledge_import_files", tools)
        self.assertIn("knowledge_sync", tools)

        imported = json.loads(tools["knowledge_import_files"].invoke({"collection": "tooling", "paths": [str(source)]}))
        self.assertEqual(imported["counts"]["imported"], 1)

        synced = json.loads(tools["knowledge_sync"].invoke({"collection": "tooling"}))
        self.assertEqual(synced["counts"]["indexed"], 1)

    def test_knowledge_tools_reload_indexes_written_after_tool_creation(self):
        _, PersonalKnowledgeBase, build_knowledge_tools = _load_symbols()
        tools = {
            tool.name: tool
            for tool in build_knowledge_tools(
                workspace_root=self.workspace,
                config_overrides={"enabled": True, "knowledge_store_path": str(self.store_dir)},
            )
        }

        collection_dir = self.store_dir / "documents" / "notes"
        collection_dir.mkdir(parents=True)
        source = collection_dir / "late.md"
        source.write_text("# Late\nfresh reload token", encoding="utf-8")
        external_kb = PersonalKnowledgeBase(workspace_root=self.workspace, store_path=self.store_dir)
        external_kb.sync(collection="notes")

        listed = json.loads(tools["knowledge_list_documents"].invoke({"collection": "notes"}))
        collections = json.loads(tools["knowledge_list_collections"].invoke({}))
        found = json.loads(tools["knowledge_search"].invoke({"query": "fresh reload token", "collection": "notes"}))

        collection_counts = {item["collection"]: item["document_count"] for item in collections}
        self.assertEqual(collection_counts["notes"], 1)
        self.assertEqual([item["title"] for item in listed], ["late.md"])
        self.assertEqual(found["results"][0]["source_ref"], "late.md")

    def test_knowledge_http_endpoints_import_sync_list_and_delete_documents(self):
        from backend import main as main_module
        from backend.main import app

        source_bytes = b"# API\nendpoint import token"
        with patch.object(main_module, "_workspace", return_value=self.workspace):
            client = TestClient(app)
            import_resp = client.post(
                "/knowledge/import",
                data={"collection": "api"},
                files={"files": ("api.md", source_bytes, "text/markdown")},
            )
            self.assertEqual(import_resp.status_code, 200)
            self.assertEqual(import_resp.json()["counts"]["imported"], 1)

            sync_resp = client.post("/knowledge/sync", json={"collection": "api"})
            self.assertEqual(sync_resp.status_code, 200)
            self.assertEqual(sync_resp.json()["counts"]["indexed"], 1)

            docs = client.get("/knowledge/documents?collection=api").json()["documents"]
            self.assertEqual(len(docs), 1)

            detail_resp = client.get(f"/knowledge/documents/{docs[0]['doc_id']}")
            self.assertEqual(detail_resp.status_code, 200)
            detail = detail_resp.json()
            self.assertEqual(detail["document"]["doc_id"], docs[0]["doc_id"])
            self.assertEqual(len(detail["chunks"]), 1)
            self.assertIn("endpoint import token", detail["chunks"][0]["text"])

            sources_resp = client.get("/knowledge/sources?collection=api")
            self.assertEqual(sources_resp.status_code, 200)
            self.assertEqual(sources_resp.json()["sources"][0]["status"], "indexed")

            delete_resp = client.delete(f"/knowledge/documents/{docs[0]['doc_id']}?remove_source=true")
            self.assertEqual(delete_resp.status_code, 200)
            self.assertEqual(delete_resp.json()["deleted"], [docs[0]["doc_id"]])

    def test_knowledge_http_endpoint_deletes_pending_source_file(self):
        from backend import main as main_module
        from backend.main import app

        collection_dir = self.store_dir / "documents" / "pending_api"
        collection_dir.mkdir(parents=True)
        pending = collection_dir / "pending.md"
        pending.write_text("# Pending\nnot indexed yet", encoding="utf-8")

        with patch.object(main_module, "_workspace", return_value=self.workspace):
            client = TestClient(app)

            delete_resp = client.delete("/knowledge/sources", params={"source_uri": str(pending)})
            self.assertEqual(delete_resp.status_code, 200)
            self.assertEqual(delete_resp.json()["removed_sources"], [str(pending)])
            self.assertFalse(pending.exists())

            sources_resp = client.get("/knowledge/sources?collection=pending_api")
            self.assertEqual(sources_resp.status_code, 200)
            self.assertEqual(sources_resp.json()["sources"], [])


if __name__ == "__main__":
    unittest.main()
