from __future__ import annotations

import tempfile
import unittest
from pathlib import Path


class FaissIndexTests(unittest.TestCase):
    def _records(self):
        from backend.rag.faiss_index import FaissRecord

        return [
            FaissRecord(
                chunk_id="chunk-a",
                doc_id="doc-a",
                collection="notes",
                vector=[1.0, 0.0, 0.0],
                payload={"title": "Alpha", "metadata": {"scope": "private"}},
            ),
            FaissRecord(
                chunk_id="chunk-b",
                doc_id="doc-b",
                collection="notes",
                vector=[0.0, 1.0, 0.0],
                payload={"title": "Beta", "metadata": {"scope": "public"}},
            ),
            FaissRecord(
                chunk_id="chunk-c",
                doc_id="doc-c",
                collection="other",
                vector=[0.0, 0.0, 1.0],
                payload={"title": "Gamma", "metadata": {"scope": "private"}},
            ),
        ]

    def test_faiss_search_filters_metadata_and_survives_reload(self):
        from backend.rag.faiss_index import FaissVectorIndex

        with tempfile.TemporaryDirectory() as temp_dir:
            index_path = Path(temp_dir) / "faiss"
            index = FaissVectorIndex(path=index_path, vector_size=3)
            index.upsert(self._records())

            hits = index.search(
                [1.0, 0.0, 0.0],
                limit=3,
                collection="notes",
                filters={"scope": "private"},
            )
            self.assertEqual([hit.chunk_id for hit in hits], ["chunk-a"])
            self.assertGreater(hits[0].score, 0.99)

            reloaded = FaissVectorIndex(path=index_path, vector_size=3)
            self.assertEqual(reloaded.ntotal, 3)
            self.assertEqual(
                [hit.chunk_id for hit in reloaded.search([0.0, 1.0, 0.0], limit=1)],
                ["chunk-b"],
            )

    def test_faiss_refresh_and_delete_remove_stale_vectors(self):
        from backend.rag.faiss_index import FaissRecord, FaissVectorIndex

        with tempfile.TemporaryDirectory() as temp_dir:
            index = FaissVectorIndex(path=Path(temp_dir), vector_size=3)
            index.upsert(self._records())
            index.upsert([
                FaissRecord(
                    chunk_id="chunk-a",
                    doc_id="doc-a",
                    collection="notes",
                    vector=[1.0, 1.0, 0.0],
                    payload={"title": "Alpha updated", "metadata": {}},
                )
            ])
            self.assertEqual(
                [hit.chunk_id for hit in index.search([1.0, 1.0, 0.0], limit=1)],
                ["chunk-a"],
            )

            index.delete_document("doc-b")
            self.assertEqual(index.search([0.0, 1.0, 0.0], limit=3)[0].chunk_id, "chunk-a")
            self.assertNotIn("chunk-b", {hit.chunk_id for hit in index.search([0.0, 1.0, 0.0], limit=3)})


class FaissConfigurationTests(unittest.TestCase):
    def test_faiss_is_configurable_and_disabled_for_deterministic_profile(self):
        from backend.rag.config import load_rag_config

        production = load_rag_config({
            "retrieval_profile": "production",
            "vector_backend": "qdrant",
            "faiss_enabled": True,
            "faiss_candidate_multiplier": 7,
        })
        self.assertTrue(production.faiss_enabled)
        self.assertEqual(production.faiss_candidate_multiplier, 7)
        self.assertTrue(str(production.faiss_path).endswith("faiss"))

        deterministic = load_rag_config({
            "retrieval_profile": "deterministic",
            "faiss_enabled": True,
        })
        self.assertFalse(deterministic.faiss_enabled)


class QdrantFaissAccelerationTests(unittest.TestCase):
    def test_qdrant_dense_search_uses_faiss_accelerator(self):
        from backend.rag.retrieval import QdrantVectorStore, VectorRecord

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = QdrantVectorStore(
                path=root / "qdrant",
                collection_name="chunks",
                vector_size=3,
                faiss_path=root / "faiss",
                faiss_enabled=True,
            )
            store.upsert([
                VectorRecord(
                    chunk_id="chunk-a",
                    doc_id="doc-a",
                    collection="notes",
                    vector=[1.0, 0.0, 0.0],
                    payload={"title": "Alpha", "metadata": {"scope": "private"}},
                ),
                VectorRecord(
                    chunk_id="chunk-b",
                    doc_id="doc-b",
                    collection="notes",
                    vector=[0.0, 1.0, 0.0],
                    payload={"title": "Beta", "metadata": {"scope": "public"}},
                ),
            ])
            self.assertTrue(store.faiss_active)

            def qdrant_must_not_be_called(*_args, **_kwargs):
                raise AssertionError("dense search should use FAISS when it is active")

            store.client.query_points = qdrant_must_not_be_called
            hits = store.search(
                [1.0, 0.0, 0.0],
                limit=2,
                collection="notes",
                filters={"scope": "private"},
            )
            self.assertEqual([hit.chunk_id for hit in hits], ["chunk-a"])
            store.close()

    def test_qdrant_dense_search_falls_back_when_faiss_fails(self):
        from backend.rag.retrieval import QdrantVectorStore, VectorRecord

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = QdrantVectorStore(
                path=root / "qdrant",
                collection_name="chunks",
                vector_size=3,
                faiss_path=root / "faiss",
                faiss_enabled=True,
            )
            store.upsert([
                VectorRecord(
                    chunk_id="chunk-a",
                    doc_id="doc-a",
                    collection="notes",
                    vector=[1.0, 0.0, 0.0],
                    payload={"title": "Alpha", "metadata": {}},
                ),
            ])
            self.assertIsNotNone(store.faiss_index)

            def fail_faiss(*_args, **_kwargs):
                raise RuntimeError("simulated FAISS failure")

            store.faiss_index.search = fail_faiss
            hits = store.search([1.0, 0.0, 0.0], limit=1)
            self.assertEqual([hit.chunk_id for hit in hits], ["chunk-a"])
            self.assertFalse(store.faiss_active)
            store.close()

    def test_qdrant_rebuilds_corrupt_faiss_index_from_persistent_vectors(self):
        from backend.rag.retrieval import QdrantVectorStore, VectorRecord

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            first = QdrantVectorStore(
                path=root / "qdrant",
                collection_name="chunks",
                vector_size=3,
                faiss_path=root / "faiss",
                faiss_enabled=True,
            )
            first.upsert([
                VectorRecord(
                    chunk_id="chunk-a",
                    doc_id="doc-a",
                    collection="notes",
                    vector=[1.0, 0.0, 0.0],
                    payload={"title": "Alpha", "metadata": {}},
                ),
            ])
            first.close()
            (root / "faiss" / "metadata.json").write_text("{corrupt", encoding="utf-8")

            rebuilt = QdrantVectorStore(
                path=root / "qdrant",
                collection_name="chunks",
                vector_size=3,
                faiss_path=root / "faiss",
                faiss_enabled=True,
            )
            try:
                self.assertTrue(rebuilt.faiss_active)
                self.assertEqual(
                    [hit.chunk_id for hit in rebuilt.search([1.0, 0.0, 0.0], limit=1)],
                    ["chunk-a"],
                )
            finally:
                rebuilt.close()

    def test_personal_knowledge_search_reports_active_faiss_accelerator(self):
        from backend.rag.config import load_rag_config
        from backend.rag.retrieval import QdrantVectorStore
        from backend.rag.service import PersonalKnowledgeBase

        class FakeEmbedding:
            model_id = "test-embedding"
            dimension = 3

            def embed(self, texts):
                return [[1.0, 0.0, 0.0] for _ in texts]

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = load_rag_config({
                "retrieval_profile": "production",
                "knowledge_store_path": str(root / "store"),
                "vector_backend": "qdrant",
                "embedding_provider": "huggingface_local",
                "embedding_model": "test-embedding",
                "embedding_dimension": 3,
                "faiss_enabled": True,
                "reranker": {"enabled": False},
            })
            store = QdrantVectorStore(
                path=root / "store" / "qdrant",
                collection_name="chunks",
                vector_size=3,
                faiss_path=root / "store" / "faiss",
                faiss_enabled=True,
            )
            try:
                source = root / "note.md"
                source.write_text("FAISS acceleration marker", encoding="utf-8")
                kb = PersonalKnowledgeBase(
                    workspace_root=root,
                    store_path=root / "store",
                    config=config,
                    embedding_provider=FakeEmbedding(),
                    vector_store=store,
                )
                self.assertEqual(kb.index_files("notes", [source])["failed"], [])
                result = kb.search("acceleration marker", collection="notes", top_k=1)
                self.assertEqual(result["settings"]["vector_accelerator"], "faiss")
                self.assertTrue(result["settings"]["vector_accelerator_active"])
            finally:
                store.close()


if __name__ == "__main__":
    unittest.main()
