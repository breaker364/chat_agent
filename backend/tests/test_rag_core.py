import json
import shutil
import unittest
from pathlib import Path
from uuid import uuid4


def _load_rag_symbols():
    try:
        from backend.rag.chunking import SemanticChunkingConfig, StructureFirstSemanticChunker
        from backend.rag.config import load_rag_config
        from backend.rag.service import PersonalKnowledgeBase
    except ModuleNotFoundError as exc:
        raise AssertionError(f"RAG module missing: {exc}") from exc
    return SemanticChunkingConfig, StructureFirstSemanticChunker, load_rag_config, PersonalKnowledgeBase


class RagCoreTests(unittest.TestCase):
    def setUp(self):
        temp_root = Path.cwd() / "tmp" / "unittest"
        temp_root.mkdir(parents=True, exist_ok=True)
        self.temp_dir = temp_root / f"{self._testMethodName}-{uuid4().hex}"
        self.temp_dir.mkdir(parents=True, exist_ok=False)
        self.workspace = self.temp_dir / "workspace"
        self.store_dir = self.temp_dir / "store"
        self.workspace.mkdir()
        self.store_dir.mkdir()

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_config_exposes_personal_knowledge_defaults(self):
        _, _, load_rag_config, _ = _load_rag_symbols()

        config = load_rag_config({"enabled": True, "knowledge_store_path": str(self.store_dir)})

        self.assertTrue(config.enabled)
        self.assertEqual(config.knowledge_store_path, self.store_dir)
        self.assertGreaterEqual(config.chunking.target_tokens, 300)
        self.assertLessEqual(config.chunking.target_tokens, 800)
        self.assertEqual(config.hybrid.fusion_strategy, "rrf")
        self.assertEqual(config.reranker.enabled, False)

    def test_structure_first_semantic_chunking_preserves_boundaries_and_metadata(self):
        SemanticChunkingConfig, StructureFirstSemanticChunker, _, _ = _load_rag_symbols()
        chunker = StructureFirstSemanticChunker(
            SemanticChunkingConfig(target_tokens=18, max_tokens=35, overlap_ratio=0.1)
        )
        text = (
            "# Install\n"
            "Follow the setup command carefully. Keep the command with the setup paragraph.\n\n"
            "| Step | Command |\n"
            "| --- | --- |\n"
            "| One | run setup |\n\n"
            "```bash\n"
            "run setup --safe\n"
            "```\n\n"
            "# Troubleshooting\n"
            "Restart the process when the cache is stale. Verify logs afterwards."
        )

        chunks = chunker.chunk_text(text, source_ref="guide.md")

        self.assertGreaterEqual(len(chunks), 2)
        self.assertTrue(all(chunk.chunking_strategy == "structure-first-semantic" for chunk in chunks))
        self.assertTrue(all(chunk.boundary_method for chunk in chunks))
        self.assertTrue(all(chunk.source_ref == "guide.md" for chunk in chunks))
        joined = "\n".join(chunk.text for chunk in chunks)
        self.assertIn("| Step | Command |", joined)
        self.assertIn("```bash", joined)
        self.assertTrue(any(chunk.heading_path == ["Install"] for chunk in chunks))
        self.assertTrue(any(chunk.heading_path == ["Troubleshooting"] for chunk in chunks))

    def test_index_rejects_outside_paths_and_unsupported_files_with_structured_results(self):
        _, _, _, PersonalKnowledgeBase = _load_rag_symbols()
        kb = PersonalKnowledgeBase(workspace_root=self.workspace, store_path=self.store_dir)
        outside = self.temp_dir / "outside.txt"
        outside.write_text("outside", encoding="utf-8")
        unsupported = self.workspace / "archive.bin"
        unsupported.write_bytes(b"\x00\x01")

        result = kb.index_files("notes", [outside, unsupported])

        skipped = {Path(item["path"]).name: item["reason"] for item in result["skipped"]}
        self.assertIn("outside.txt", skipped)
        self.assertIn("outside allowed workspace roots", skipped["outside.txt"])
        self.assertIn("archive.bin", skipped)
        self.assertIn("unsupported file type", skipped["archive.bin"])
        self.assertEqual(result["indexed"], [])

    def test_refresh_rechunks_when_chunking_config_changes(self):
        _, _, _, PersonalKnowledgeBase = _load_rag_symbols()
        source = self.workspace / "guide.md"
        source.write_text("# A\nalpha beta gamma delta epsilon zeta eta theta\n\n# B\nsecond topic", encoding="utf-8")
        kb = PersonalKnowledgeBase(workspace_root=self.workspace, store_path=self.store_dir)

        first = kb.index_files("notes", [source])
        first_doc = first["indexed"][0]["doc_id"]
        first_chunks = [chunk["chunk_id"] for chunk in kb.list_chunks(first_doc)]
        kb.chunker_config.target_tokens = 4
        refreshed = kb.index_files("notes", [source], refresh=True)
        refreshed_chunks = [chunk["chunk_id"] for chunk in kb.list_chunks(first_doc)]

        self.assertTrue(refreshed["indexed"][0]["refreshed"])
        self.assertNotEqual(first_chunks, refreshed_chunks)

    def test_delete_document_removes_manifest_chunks_and_search_results(self):
        _, _, _, PersonalKnowledgeBase = _load_rag_symbols()
        source = self.workspace / "notes.txt"
        source.write_text("alpha topic with exact token ZX-991", encoding="utf-8")
        kb = PersonalKnowledgeBase(workspace_root=self.workspace, store_path=self.store_dir)
        doc_id = kb.index_files("notes", [source])["indexed"][0]["doc_id"]

        self.assertTrue(kb.search("ZX-991", collection="notes")["results"])
        delete_result = kb.delete_document(doc_id=doc_id)

        self.assertEqual(delete_result["deleted"], [doc_id])
        self.assertEqual(kb.list_chunks(doc_id), [])
        self.assertEqual(kb.search("ZX-991", collection="notes")["results"], [])

    def test_hybrid_search_uses_lexical_dense_rrf_and_reranking_metadata(self):
        _, _, _, PersonalKnowledgeBase = _load_rag_symbols()
        exact = self.workspace / "exact.md"
        semantic = self.workspace / "semantic.md"
        exact.write_text("# Reference\nDevice code ZX-991 requires calibration every week.", encoding="utf-8")
        semantic.write_text("# Maintenance\nWeekly adjustment keeps the instrument accurate.", encoding="utf-8")
        kb = PersonalKnowledgeBase(workspace_root=self.workspace, store_path=self.store_dir, reranker_enabled=True)
        kb.index_files("manuals", [exact, semantic])

        result = kb.search("weekly calibration for ZX-991", collection="manuals", top_k=2, include_scores=True)

        self.assertEqual(result["settings"]["fusion_strategy"], "rrf")
        self.assertTrue(result["settings"]["reranker_enabled"])
        self.assertEqual(len(result["results"]), 2)
        self.assertIn("reranker_score", result["results"][0]["scores"])
        self.assertTrue(any("ZX-991" in item["snippet"] for item in result["results"]))
        self.assertTrue(any("Weekly adjustment" in item["snippet"] for item in result["results"]))

    def test_search_output_is_bounded_and_does_not_dump_full_document(self):
        _, _, _, PersonalKnowledgeBase = _load_rag_symbols()
        source = self.workspace / "long.txt"
        source.write_text("secret term " + ("longcontent " * 300), encoding="utf-8")
        kb = PersonalKnowledgeBase(workspace_root=self.workspace, store_path=self.store_dir)
        kb.index_files("notes", [source])

        result = kb.search("secret term", collection="notes", top_k=1)
        payload = json.dumps(result, ensure_ascii=False)

        self.assertLess(len(result["results"][0]["snippet"]), 500)
        self.assertLess(len(payload), len(source.read_text(encoding="utf-8")))

    def test_search_snippet_focuses_on_query_match_inside_long_chunk(self):
        _, _, _, PersonalKnowledgeBase = _load_rag_symbols()
        source = self.workspace / "late-answer.txt"
        source.write_text(
            "# Guide\n"
            + ("introductory filler " * 40)
            + "The answer marker ZX-991 appears near the end.",
            encoding="utf-8",
        )
        kb = PersonalKnowledgeBase(workspace_root=self.workspace, store_path=self.store_dir)
        kb.index_files("notes", [source])

        result = kb.search("ZX-991", collection="notes", top_k=1)

        snippet = result["results"][0]["snippet"]
        self.assertIn("ZX-991", snippet)
        self.assertLess(len(snippet), 500)

    def test_search_excerpt_prefers_specific_chinese_query_terms_over_early_dates(self):
        _, _, _, PersonalKnowledgeBase = _load_rag_symbols()
        source = self.workspace / "stage-plan.md"
        source.write_text(
            "# Plan\n"
            "2025-04-01 ~2025-09-30 early stage content. "
            + ("前期说明 " * 120)
            + "2025-10-01 ~2026-03-31 研发并集成异常模式发\n现方法，"
            + ("阶段说明 " * 120)
            + "考核指标为异常模式发\n现方法准\n确率超过85%。",
            encoding="utf-8",
        )
        kb = PersonalKnowledgeBase(workspace_root=self.workspace, store_path=self.store_dir)
        kb.index_files("notes", [source])

        result = kb.search("2025年10月到2026年3月阶段异常模式发现方法准确率要求是多少？", collection="notes", top_k=1)

        excerpt = result["results"][0]["excerpt"]
        self.assertIn("准确率超过85%", excerpt.replace(" ", ""))

    def test_search_includes_adjacent_chunk_for_split_stage_evidence(self):
        SemanticChunkingConfig, _, _, PersonalKnowledgeBase = _load_rag_symbols()
        source = self.workspace / "adjacent-stage.md"
        source.write_text(
            "# Stage\n"
            "phase marker alpha beta gamma delta.\n\n"
            "OMEGA-85 criterion appears only in the adjacent paragraph.",
            encoding="utf-8",
        )
        kb = PersonalKnowledgeBase(
            workspace_root=self.workspace,
            store_path=self.store_dir,
            chunker_config=SemanticChunkingConfig(target_tokens=5, max_tokens=50),
        )
        kb.index_files("notes", [source])

        result = kb.search("phase marker alpha beta gamma delta", collection="notes", top_k=2)

        excerpts = [item["excerpt"] for item in result["results"]]
        self.assertTrue(any("OMEGA-85" in excerpt for excerpt in excerpts))
        self.assertTrue(any(item["metadata"].get("adjacent_context") for item in result["results"]))


if __name__ == "__main__":
    unittest.main()
