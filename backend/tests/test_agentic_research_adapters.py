from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path


class EvidenceAdapterTests(unittest.TestCase):
    def test_knowledge_adapter_preserves_citations_and_settings(self):
        from backend.agentic_research.adapters import KnowledgeEvidenceAdapter

        adapter = KnowledgeEvidenceAdapter(lambda query: json.dumps({
            "query": query,
            "results": [{
                "chunk_id": "chunk-1",
                "title": "Indexed note",
                "source_uri": "feishu://document/opaque",
                "excerpt": "bounded evidence",
            }],
            "settings": {"embedding_provider": "huggingface_local", "vector_backend": "qdrant"},
        }))
        observation = adapter.collect("retrieval behavior")
        self.assertEqual(observation.source_kind, "personal_knowledge")
        self.assertEqual(observation.citations[0]["id"], "chunk-1")
        self.assertEqual(observation.metadata["settings"]["vector_backend"], "qdrant")
        self.assertEqual(observation.excerpts, ["bounded evidence"])

    def test_workspace_adapter_rejects_paths_outside_root(self):
        from backend.agentic_research.adapters import WorkspaceEvidenceAdapter

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "note.md").write_text("workspace evidence", encoding="utf-8")
            calls: list[str] = []
            adapter = WorkspaceEvidenceAdapter(
                workspace_root=root,
                read_file=lambda path: calls.append(path) or "workspace evidence",
            )
            observation = adapter.collect("note.md")
            self.assertEqual(observation.source_kind, "workspace")
            self.assertEqual(calls, ["note.md"])
            outside = adapter.collect(str(root.parent / "outside.md"))
            self.assertEqual(outside.status, "access_denied")

    def test_workspace_adapter_reads_a_scoped_directory_without_exposing_other_roots(self):
        from backend.agentic_research.adapters import WorkspaceEvidenceAdapter

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "notes").mkdir()
            calls: list[str] = []
            adapter = WorkspaceEvidenceAdapter(
                workspace_root=root,
                read_file=lambda _path: "unused",
                list_directory=lambda path: calls.append(path) or "guide.md\nplan.md",
            )
            observation = adapter.collect("notes")

            self.assertEqual(calls, ["notes"])
            self.assertEqual(observation.status, "ok")
            self.assertEqual(observation.citations[0]["uri"], "workspace://notes")
            self.assertEqual(observation.excerpts, ["guide.md\nplan.md"])

    def test_web_adapter_bounds_results_and_maps_provider_errors(self):
        from backend.agentic_research.adapters import WebEvidenceAdapter

        adapter = WebEvidenceAdapter(lambda query: json.dumps({
            "results": [{
                "title": "Primary result",
                "url": "https://example.invalid/source",
                "snippet": "a bounded snippet",
            }],
        }))
        observation = adapter.collect("current fact")
        self.assertEqual(observation.source_kind, "web")
        self.assertEqual(observation.citations[0]["uri"], "https://example.invalid/source")
        self.assertEqual(observation.excerpts, ["a bounded snippet"])

        failing = WebEvidenceAdapter(lambda _query: (_ for _ in ()).throw(TimeoutError("timeout")))
        self.assertEqual(failing.collect("fact").status, "provider_unavailable")


class EvidenceRegistryTests(unittest.TestCase):
    def test_registry_contains_only_read_only_source_kinds(self):
        from backend.agentic_research.adapters import EvidenceRegistry, EvidenceRegistryError

        class FakeAdapter:
            def collect(self, _query):
                return None

        fake = FakeAdapter()
        registry = EvidenceRegistry({"personal_knowledge": fake, "workspace": fake, "web": fake})
        self.assertEqual(set(registry.source_kinds), {"personal_knowledge", "workspace", "web"})
        with self.assertRaises(EvidenceRegistryError):
            registry.get("knowledge_import_feishu_document")
        with self.assertRaises(EvidenceRegistryError):
            EvidenceRegistry({"delete": object()})


if __name__ == "__main__":
    unittest.main()
