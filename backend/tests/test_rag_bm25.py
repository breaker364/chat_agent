import shutil
import unittest
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4


class Bm25Tests(unittest.TestCase):
    def setUp(self):
        root = Path.cwd() / "tmp" / "unittest"
        root.mkdir(parents=True, exist_ok=True)
        self.temp_dir = root / f"bm25-{uuid4().hex}"
        self.temp_dir.mkdir()

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_bm25_applies_idf_term_frequency_and_length_normalization(self):
        from backend.rag.bm25 import BM25Index

        chunks = [
            SimpleNamespace(chunk_id="short", text="rare marker"),
            SimpleNamespace(chunk_id="long", text="rare marker " + ("filler " * 80)),
            SimpleNamespace(chunk_id="common", text="common common common"),
        ]
        index = BM25Index(chunks, k1=1.2, b=0.75)

        results = index.search("rare marker", limit=3)
        scores = {chunk.chunk_id: score for chunk, score in results}

        self.assertEqual(results[0][0].chunk_id, "short")
        self.assertGreater(scores["short"], scores["long"])
        self.assertEqual(index.search("missing-term", limit=3), [])
        self.assertEqual(len(index.search("rare marker", limit=1)), 1)

    def test_bm25_participates_in_rrf_and_exposes_separate_score_metadata(self):
        from backend.rag.config import load_rag_config
        from backend.rag.service import PersonalKnowledgeBase

        class FakeEmbedding:
            model_id = "test-embedding"
            dimension = 3

            def embed(self, texts):
                return [[1.0, 0.0, 0.0] for _ in texts]

        class FakeVectorStore:
            def __init__(self):
                self.chunk_id = ""

            def refresh(self, _doc_id, records):
                self.chunk_id = records[0].chunk_id if records else ""
                return None

            def search(self, _query_vector, *, limit, collection=None, filters=None):
                from backend.rag.retrieval import VectorSearchHit

                del limit, collection, filters
                if not self.chunk_id:
                    return []
                return [VectorSearchHit(
                    chunk_id=self.chunk_id,
                    doc_id="",
                    collection="notes",
                    score=0.9,
                )]

            def document_point_ids(self, _doc_id):
                return []

            def delete_document(self, _doc_id):
                return None

        workspace = self.temp_dir / "workspace"
        store = self.temp_dir / "store"
        workspace.mkdir()
        source = workspace / "bm25.md"
        source.write_text("# Guide\nrare identifier ZX-991 appears here", encoding="utf-8")
        config = load_rag_config(
            {
                "enabled": True,
                "retrieval_profile": "production",
                "knowledge_store_path": str(store),
                "embedding_provider": "huggingface_local",
                "embedding_model": "BAAI/bge-m3",
                "embedding_dimension": 3,
                "vector_backend": "qdrant",
                "sparse_backend": "bm25",
                "bm25": {"k1": 1.2, "b": 0.75},
                "reranker_enabled_by_default": False,
            }
        )
        vector_store = FakeVectorStore()
        kb = PersonalKnowledgeBase(
            workspace_root=workspace,
            store_path=store,
            config=config,
            embedding_provider=FakeEmbedding(),
            vector_store=vector_store,
        )
        kb.index_files("notes", [source])

        result = kb.search("ZX-991", collection="notes", top_k=1)

        self.assertEqual(result["settings"]["sparse_backend"], "bm25")
        self.assertEqual(result["settings"]["candidate_counts"]["bm25"], 1)
        self.assertEqual(result["settings"]["candidate_counts"]["dense"], 1)
        scores = result["results"][0]["scores"]
        self.assertIn("bm25_score", scores)
        self.assertIn("dense_score", scores)
        self.assertIn("fusion_score", scores)
        self.assertEqual(scores["bm25_score"], scores["lexical_score"])


if __name__ == "__main__":
    unittest.main()
