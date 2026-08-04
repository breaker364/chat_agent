import json
import shutil
import unittest
from pathlib import Path
from uuid import uuid4

from fastapi.testclient import TestClient
from unittest.mock import patch


class FakeRemoteProvider:
    provider_name = "feishu"

    def __init__(self):
        from backend.rag.sources import RemoteDocumentMetadata

        self.metadata_type = RemoteDocumentMetadata
        self.content = "# Remote Guide\nremote knowledge token ZX-991"
        self.revision = "rev-1"
        self.source_url = "https://docs.example.test/docx/remote-token"
        self.fail_category = ""
        self.inspect_calls = 0
        self.fetch_calls = 0

    def inspect(self, _reference):
        from backend.rag.sources import RemoteSourceError

        self.inspect_calls += 1
        if self.fail_category:
            raise RemoteSourceError(
                "remote provider failure",
                category=self.fail_category,
                retryable=self.fail_category in {"auth_required", "provider_unavailable", "rate_limited"},
            )
        return self.metadata_type(
            provider=self.provider_name,
            object_token="remote-token",
            source_uri="feishu://document/remote-token",
            source_url=self.source_url,
            title="Remote Guide",
            remote_revision=self.revision,
            remote_updated_at="2026-08-04T00:00:00Z",
            owner="owner-1",
        )

    def fetch(self, metadata):
        from backend.rag.sources import RemoteDocumentSnapshot

        self.fetch_calls += 1
        return RemoteDocumentSnapshot(metadata=metadata, content=self.content, content_format="markdown")


class FeishuKnowledgeImportTests(unittest.TestCase):
    def setUp(self):
        root = Path.cwd() / "tmp" / "unittest"
        root.mkdir(parents=True, exist_ok=True)
        self.temp_dir = root / f"feishu-import-{uuid4().hex}"
        self.temp_dir.mkdir()
        self.workspace = self.temp_dir / "workspace"
        self.store = self.temp_dir / "store"
        self.workspace.mkdir()
        self.provider = FakeRemoteProvider()

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _kb(self):
        from backend.rag.config import load_rag_config
        from backend.rag.service import PersonalKnowledgeBase

        config = load_rag_config(
            {
                "enabled": True,
                "retrieval_profile": "deterministic",
                "knowledge_store_path": str(self.store),
                "sparse_backend": "bm25",
            }
        )
        return PersonalKnowledgeBase(
            workspace_root=self.workspace,
            store_path=self.store,
            config=config,
            remote_provider=self.provider,
        )

    def test_import_is_idempotent_and_keeps_remote_provenance(self):
        kb = self._kb()

        first = kb.import_remote_document("notes", "https://docs.example.test/wiki/remote-token")
        second = kb.import_remote_document("notes", "remote-token")

        self.assertEqual(first["status"], "indexed")
        self.assertEqual(second["status"], "unchanged")
        self.assertEqual(first["doc_id"], second["doc_id"])
        self.assertEqual(first["source_uri"], "feishu://document/remote-token")
        self.assertEqual(first["source_url"], self.provider.source_url)
        self.assertEqual(self.provider.fetch_calls, 1)
        snapshot = Path(first["snapshot_path"])
        self.assertTrue(snapshot.exists())
        self.assertNotEqual(snapshot.as_posix(), first["source_uri"])

        result = kb.search("ZX-991", collection="notes", top_k=1)
        self.assertEqual(result["results"][0]["source_url"], self.provider.source_url)

    def test_changed_remote_revision_refreshes_same_document(self):
        kb = self._kb()
        first = kb.import_remote_document("notes", "remote-token")
        self.provider.revision = "rev-2"
        self.provider.content = "# Remote Guide\nupdated remote knowledge token"

        refreshed = kb.import_remote_document("notes", "remote-token")

        self.assertEqual(refreshed["status"], "refreshed")
        self.assertEqual(refreshed["doc_id"], first["doc_id"])
        self.assertEqual(self.provider.fetch_calls, 2)
        self.assertTrue(kb.search("updated remote", collection="notes")["results"])

    def test_retryable_sync_failure_preserves_last_good_index(self):
        kb = self._kb()
        imported = kb.import_remote_document("notes", "remote-token")
        self.provider.fail_category = "provider_unavailable"

        report = kb.sync_remote_sources(provider_name="feishu", collection="notes")

        self.assertEqual(report["counts"]["failed"], 1)
        self.assertEqual(report["files"][0]["status"], "sync_failed")
        self.assertTrue(kb.search("ZX-991", collection="notes")["results"])
        self.assertEqual(kb.manifests[imported["doc_id"]].status, "sync_failed")

    def test_confirmed_permission_loss_removes_active_index(self):
        kb = self._kb()
        imported = kb.import_remote_document("notes", "remote-token")
        self.provider.fail_category = "permission_denied"

        report = kb.sync_remote_sources(provider_name="feishu", collection="notes")

        self.assertEqual(report["counts"]["access_denied"], 1)
        self.assertEqual(kb.manifests[imported["doc_id"]].status, "access_denied")
        self.assertEqual(kb.search("ZX-991", collection="notes")["results"], [])

    def test_local_sync_does_not_treat_remote_uri_as_a_filesystem_path(self):
        kb = self._kb()
        imported = kb.import_remote_document("notes", "remote-token")

        report = kb.sync(collection="notes")

        self.assertNotIn(imported["doc_id"], {item.get("doc_id") for item in report["files"] if item.get("doc_id")})
        self.assertEqual(kb.manifests[imported["doc_id"]].status, "indexed")

    def test_feishu_provider_canonicalizes_and_normalizes_with_injected_reader(self):
        from backend.rag.sources import FeishuDocumentProvider

        class Reader:
            DOC_HOST = "docs.example.test"

            def resolve_doc_token(self, _cookies, _reference):
                return "resolved-token"

            def http_get(self, _cookies, _host, _path):
                return {"data": {"code": 0, "data": {"title": "Injected Doc", "revision": "r1"}}}

            def fetch_doc_blocks(self, _cookies, _token):
                return {"sequence": ["1"], "blocks": {"1": {"data": {}}}}

            def doc_blocks_to_markdown(self, _sequence, _blocks, _cookies):
                return "# Injected Doc\n\ncontent"

        provider = FeishuDocumentProvider(
            workspace_root=self.workspace,
            reader=Reader(),
            session_loader=lambda: [{"name": "session", "value": "redacted"}],
        )

        metadata = provider.inspect("https://docs.example.test/docx/source-token")
        snapshot = provider.fetch(metadata)

        self.assertEqual(metadata.object_token, "resolved-token")
        self.assertEqual(metadata.title, "Injected Doc")
        self.assertEqual(snapshot.content_format, "markdown")
        self.assertIn("content", snapshot.content)

    def test_feishu_provider_reports_missing_session_without_login(self):
        from backend.rag.sources import FeishuDocumentProvider, RemoteSourceError

        provider = FeishuDocumentProvider(
            workspace_root=self.workspace,
            reader=object(),
            session_loader=lambda: [],
        )

        with self.assertRaises(RemoteSourceError) as context:
            provider.inspect("remote-token")

        self.assertEqual(context.exception.category, "auth_required")

    def test_remote_reference_contract_and_safe_configuration(self):
        from dataclasses import asdict

        from backend.rag.config import load_rag_config
        from backend.rag.sources import RemoteSourceError, canonical_source_uri, parse_remote_reference

        parsed_url = parse_remote_reference("https://docs.example.test/wiki/remote-token")
        parsed_token = parse_remote_reference("remote-token")
        self.assertEqual(parsed_url.token, parsed_token.token)
        self.assertEqual(canonical_source_uri("feishu", parsed_url.token), "feishu://document/remote-token")
        for reference in ("", "not a token", "https://docs.example.test/folder/no-document"):
            with self.subTest(reference=reference):
                with self.assertRaises(RemoteSourceError) as context:
                    parse_remote_reference(reference)
                self.assertEqual(context.exception.category, "invalid_reference")

        config = load_rag_config(
            {
                "enabled": True,
                "remote_source": {
                    "provider": "feishu",
                    "session_mode": "existing_session_store",
                    "max_content_chars": 1000,
                    "max_retries": 2,
                },
            }
        )
        serialized = json.dumps(asdict(config.remote_source), ensure_ascii=False)
        self.assertEqual(config.remote_source.max_retries, 2)
        self.assertNotIn("session_cookie", serialized)
        self.assertNotIn("authorization", serialized.lower())
        self.assertNotIn("secret", serialized.lower())

    def test_remote_snapshot_normalizes_content_and_rejects_empty_text(self):
        from backend.rag.sources import RemoteDocumentMetadata, RemoteDocumentSnapshot, RemoteSourceError

        metadata = RemoteDocumentMetadata(
            provider="feishu",
            object_token="remote-token",
            source_uri="feishu://document/remote-token",
            title="Normalized",
        )
        snapshot = RemoteDocumentSnapshot(
            metadata=metadata,
            content="<h1>Title</h1>\r\n<p>body</p>",
            content_format="html",
        )
        self.assertEqual(snapshot.content, "Title\n\nbody")
        self.assertTrue(snapshot.content_hash)
        with self.assertRaises(RemoteSourceError) as context:
            RemoteDocumentSnapshot(metadata=metadata, content="   ")
        self.assertEqual(context.exception.category, "content_invalid")

    def test_snapshot_write_failure_preserves_previous_remote_index(self):
        kb = self._kb()
        first = kb.import_remote_document("notes", "remote-token")
        snapshot_path = Path(first["snapshot_path"])
        original_content = snapshot_path.read_text(encoding="utf-8")
        original_hash = kb.manifests[first["doc_id"]].content_hash
        self.provider.revision = "rev-2"
        self.provider.content = "# Changed\nreplacement content"

        def fail_snapshot(*_args, **_kwargs):
            raise OSError("snapshot write failed")

        kb._write_remote_snapshot = fail_snapshot
        result = kb.import_remote_document("notes", "remote-token")

        self.assertEqual(result["status"], "error")
        self.assertEqual(result["error"]["code"], "rag_backend_error")
        self.assertEqual(kb.manifests[first["doc_id"]].content_hash, original_hash)
        self.assertEqual(snapshot_path.read_text(encoding="utf-8"), original_content)
        self.assertTrue(kb.search("ZX-991", collection="notes")["results"])

    def test_confirmed_remote_deletion_removes_snapshot_and_search_content(self):
        kb = self._kb()
        imported = kb.import_remote_document("notes", "remote-token")
        snapshot_path = Path(imported["snapshot_path"])
        self.provider.fail_category = "not_found"

        report = kb.sync_remote_sources(provider_name="feishu", collection="notes")

        self.assertEqual(report["counts"]["remote_deleted"], 1)
        self.assertEqual(report["files"][0]["status"], "remote_deleted")
        self.assertEqual(kb.manifests[imported["doc_id"]].status, "remote_deleted")
        self.assertFalse(snapshot_path.exists())
        self.assertEqual(kb.search("ZX-991", collection="notes")["results"], [])

    def test_feishu_tools_and_http_endpoints_are_exposed_with_bounded_results(self):
        from backend import main as main_module
        from backend.rag.tools import build_knowledge_tools

        kb = self._kb()
        with patch.object(main_module, "_project_knowledge_base", return_value=kb):
            client = TestClient(main_module.app)
            response = client.post(
                "/knowledge/import/feishu",
                json={"reference": "remote-token", "collection": "notes"},
            )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["status"], "indexed")
        self.assertNotIn("content", payload)

        tools = {tool.name: tool for tool in build_knowledge_tools(
            workspace_root=self.workspace,
            config_overrides={
                "enabled": True,
                "retrieval_profile": "deterministic",
                "knowledge_store_path": str(self.store),
            },
            remote_provider=self.provider,
        )}
        self.assertIn("knowledge_import_feishu_document", tools)
        result = json.loads(tools["knowledge_import_feishu_document"].invoke({
            "reference": "remote-token",
            "collection": "notes",
        }))
        self.assertIn(result["status"], {"unchanged", "indexed", "refreshed"})
        self.assertNotIn("content", result)

    def test_feishu_http_validates_requests_and_maps_import_errors(self):
        from backend import main as main_module

        kb = self._kb()
        with patch.object(main_module, "_project_knowledge_base", return_value=kb):
            client = TestClient(main_module.app)

            missing_reference = client.post(
                "/knowledge/import/feishu",
                json={"collection": "notes"},
            )
            self.assertEqual(missing_reference.status_code, 400)

            expected_statuses = {
                "invalid_reference": 400,
                "auth_required": 401,
                "permission_denied": 403,
                "not_found": 404,
                "rate_limited": 429,
                "provider_unavailable": 503,
            }
            for category, expected_status in expected_statuses.items():
                with self.subTest(category=category):
                    self.provider.fail_category = category
                    response = client.post(
                        "/knowledge/import/feishu",
                        json={"reference": "remote-token", "collection": "notes"},
                    )
                    self.assertEqual(response.status_code, expected_status)
                    self.assertEqual(response.json()["error"]["code"], category)

    def test_feishu_sync_endpoint_maps_access_errors_and_accepts_dry_run(self):
        from backend import main as main_module

        kb = self._kb()
        kb.import_remote_document("notes", "remote-token")
        self.provider.fail_category = "permission_denied"
        with patch.object(main_module, "_project_knowledge_base", return_value=kb):
            client = TestClient(main_module.app)

            denied = client.post(
                "/knowledge/sync/feishu",
                json={"collection": "notes"},
            )
            self.assertEqual(denied.status_code, 403)
            self.assertEqual(denied.json()["error"]["code"], "permission_denied")

            dry_run = client.post(
                "/knowledge/sync/feishu",
                json={"collection": "notes", "dry_run": True},
            )
            self.assertEqual(dry_run.status_code, 200)
            self.assertTrue(dry_run.json()["dry_run"])


if __name__ == "__main__":
    unittest.main()
