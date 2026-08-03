import math
import shutil
import types
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

import torch


class _FakeTokenizer:
    def __call__(self, texts, text_pair=None, **_kwargs):
        rows = [[index + 1, index + 2, 0] for index, _ in enumerate(texts)]
        mask = [[1, 1, 0] for _ in texts]
        return {
            "input_ids": torch.tensor(rows, dtype=torch.long),
            "attention_mask": torch.tensor(mask, dtype=torch.long),
        }


@contextmanager
def _test_directory():
    root = Path.cwd() / "tmp" / "unittest" / f"retrieval-stack-{uuid4().hex}"
    root.mkdir(parents=True, exist_ok=False)
    try:
        yield root
    finally:
        shutil.rmtree(root, ignore_errors=True)


class _FakeEmbeddingModel:
    def eval(self):
        return self

    def to(self, _device):
        return self

    def __call__(self, **kwargs):
        input_ids = kwargs["input_ids"].float()
        hidden = torch.stack((input_ids, input_ids + 1, input_ids + 2), dim=-1)
        return types.SimpleNamespace(last_hidden_state=hidden)


class _FakeRerankerModel:
    def eval(self):
        return self

    def to(self, _device):
        return self

    def __call__(self, **kwargs):
        input_ids = kwargs["input_ids"].float()
        logits = input_ids.sum(dim=1, keepdim=True)
        return types.SimpleNamespace(logits=logits)


class RagRetrievalStackTests(unittest.TestCase):
    def test_bge_embedding_provider_batches_normalizes_and_validates_dimension(self):
        from backend.rag.retrieval import LocalBgeEmbeddingProvider

        with _test_directory() as temp_dir:
            model_path = temp_dir / "cached-bge-m3"
            model_path.mkdir()
            with patch("backend.rag.retrieval.AutoTokenizer.from_pretrained", return_value=_FakeTokenizer()), patch(
                "backend.rag.retrieval.AutoModel.from_pretrained", return_value=_FakeEmbeddingModel()
            ):
                provider = LocalBgeEmbeddingProvider(
                    model_path=model_path,
                    model_id="BAAI/bge-m3",
                    dimension=3,
                    batch_size=2,
                    device="cpu",
                )
                vectors = provider.embed(["first", "second", "third"])

        self.assertEqual(len(vectors), 3)
        self.assertTrue(all(len(vector) == 3 for vector in vectors))
        self.assertTrue(all(math.isclose(sum(value * value for value in vector), 1.0, rel_tol=1e-5) for vector in vectors))

    def test_qdrant_store_supports_upsert_filtered_search_refresh_and_delete(self):
        from backend.rag.retrieval import QdrantVectorStore, VectorRecord

        with _test_directory() as temp_dir:
            store = QdrantVectorStore(path=temp_dir / "qdrant", collection_name="chunks", vector_size=3)
            store.upsert(
                [
                    VectorRecord("chunk-a", "doc-a", "notes", [1.0, 0.0, 0.0], {"title": "A"}),
                    VectorRecord("chunk-b", "doc-b", "other", [0.0, 1.0, 0.0], {"title": "B"}),
                ]
            )

            filtered = store.search([1.0, 0.0, 0.0], limit=5, collection="notes")
            self.assertEqual([hit.chunk_id for hit in filtered], ["chunk-a"])

            store.upsert([VectorRecord("chunk-a", "doc-a", "notes", [0.0, 0.0, 1.0], {"title": "A2"})])
            refreshed = store.search([0.0, 0.0, 1.0], limit=5, collection="notes")
            self.assertEqual(refreshed[0].chunk_id, "chunk-a")
            self.assertEqual(refreshed[0].payload["title"], "A2")

            store.delete_document("doc-a")
            self.assertEqual(store.search([0.0, 0.0, 1.0], limit=5, collection="notes"), [])

    def test_bge_reranker_returns_model_scores(self):
        from backend.rag.retrieval import LocalBgeRerankerProvider

        with _test_directory() as temp_dir:
            model_path = temp_dir / "cached-reranker"
            model_path.mkdir()
            with patch("backend.rag.retrieval.AutoTokenizer.from_pretrained", return_value=_FakeTokenizer()), patch(
                "backend.rag.retrieval.AutoModelForSequenceClassification.from_pretrained",
                return_value=_FakeRerankerModel(),
            ):
                provider = LocalBgeRerankerProvider(
                    model_path=model_path,
                    model_id="BAAI/bge-reranker-v2-m3",
                    batch_size=2,
                    device="cpu",
                )
                scores = provider.score("query", ["first", "second"])

        self.assertEqual(len(scores), 2)
        self.assertGreater(scores[1], scores[0])

    def test_knowledge_base_uses_injected_dense_store_and_reranker(self):
        from backend.rag.config import load_rag_config
        from backend.rag.retrieval import QdrantVectorStore
        from backend.rag.service import PersonalKnowledgeBase

        class FakeEmbedding:
            model_id = "BAAI/bge-m3"
            dimension = 3

            def embed(self, texts):
                return [[1.0, 0.0, 0.0] for _ in texts]

        class FakeReranker:
            model_id = "BAAI/bge-reranker-v2-m3"

            def score(self, _query, texts):
                return [float(index) for index, _ in enumerate(texts)]

        with _test_directory() as root:
            workspace = root / "workspace"
            store_path = root / "knowledge_base"
            workspace.mkdir()
            source = workspace / "guide.md"
            source.write_text("# Guide\nalpha exact token\n\nsemantic maintenance detail", encoding="utf-8")
            config = load_rag_config(
                {
                    "enabled": True,
                    "knowledge_store_path": str(store_path),
                    "embedding_provider": "huggingface_local",
                    "embedding_model": "BAAI/bge-m3",
                    "embedding_dimension": 3,
                    "vector_backend": "qdrant",
                    "reranker_provider": "huggingface_local",
                    "reranker_model": "BAAI/bge-reranker-v2-m3",
                    "reranker_enabled_by_default": True,
                }
            )
            kb = PersonalKnowledgeBase(
                workspace_root=workspace,
                store_path=store_path,
                config=config,
                embedding_provider=FakeEmbedding(),
                reranker_provider=FakeReranker(),
                vector_store=QdrantVectorStore(
                    path=store_path / "qdrant",
                    collection_name="chunks",
                    vector_size=3,
                ),
            )

            kb.index_files("notes", [source])
            result = kb.search("semantic maintenance", collection="notes", top_k=1)

        self.assertEqual(result["settings"]["embedding_provider"], "huggingface_local")
        self.assertEqual(result["settings"]["vector_backend"], "qdrant")
        self.assertEqual(result["settings"]["reranker_model"], "BAAI/bge-reranker-v2-m3")
        self.assertIn("reranker_score", result["results"][0]["scores"])

    def test_reranker_score_changes_fused_order_and_stays_separate_from_lexical_score(self):
        from backend.rag.config import load_rag_config
        from backend.rag.retrieval import QdrantVectorStore
        from backend.rag.service import PersonalKnowledgeBase

        class FakeEmbedding:
            model_id = "BAAI/bge-m3"
            dimension = 3

            def embed(self, texts):
                return [[1.0, 0.0, 0.0] for _ in texts]

        class FakeReranker:
            model_id = "BAAI/bge-reranker-v2-m3"

            def score(self, _query, texts):
                return [0.9 if "high relevance" in text else 0.1 for text in texts]

        with _test_directory() as root:
            workspace = root / "workspace"
            store_path = root / "knowledge_base"
            workspace.mkdir()
            low = workspace / "low.md"
            high = workspace / "high.md"
            low.write_text("# Low\ncommon semantic query low relevance", encoding="utf-8")
            high.write_text("# High\ncommon semantic query high relevance", encoding="utf-8")
            config = load_rag_config(
                {
                    "enabled": True,
                    "retrieval_profile": "production",
                    "knowledge_store_path": str(store_path),
                    "embedding_provider": "huggingface_local",
                    "embedding_model": "BAAI/bge-m3",
                    "embedding_dimension": 3,
                    "vector_backend": "qdrant",
                    "qdrant_collection": "chunks",
                    "reranker_provider": "huggingface_local",
                    "reranker_model": "BAAI/bge-reranker-v2-m3",
                    "reranker_enabled_by_default": True,
                    "reranker_candidate_top_k": 10,
                }
            )
            kb = PersonalKnowledgeBase(
                workspace_root=workspace,
                store_path=store_path,
                config=config,
                embedding_provider=FakeEmbedding(),
                reranker_provider=FakeReranker(),
                vector_store=QdrantVectorStore(
                    path=store_path / "qdrant",
                    collection_name="chunks",
                    vector_size=3,
                ),
            )
            kb.index_files("notes", [low, high])
            result = kb.search("common semantic query", collection="notes", top_k=2)

        self.assertIn("high relevance", result["results"][0]["snippet"])
        scores = result["results"][0]["scores"]
        self.assertIn("reranker_score", scores)
        self.assertIn("lexical_score", scores)
        self.assertNotEqual(scores["reranker_score"], scores["lexical_score"])

    def test_retrieval_signature_change_forces_reembedding_and_qdrant_refresh(self):
        from backend.rag.config import load_rag_config
        from backend.rag.retrieval import QdrantVectorStore
        from backend.rag.service import PersonalKnowledgeBase

        class CountingEmbedding:
            model_id = "BAAI/bge-m3"
            dimension = 3

            def __init__(self):
                self.calls = 0

            def embed(self, texts):
                self.calls += 1
                return [[1.0, 0.0, 0.0] for _ in texts]

        with _test_directory() as root:
            workspace = root / "workspace"
            store_path = root / "knowledge_base"
            workspace.mkdir()
            source = workspace / "migration.md"
            source.write_text("migration signature token", encoding="utf-8")
            base_overrides = {
                "enabled": True,
                "retrieval_profile": "production",
                "knowledge_store_path": str(store_path),
                "embedding_provider": "huggingface_local",
                "embedding_model": "BAAI/bge-m3",
                "embedding_dimension": 3,
                "vector_backend": "qdrant",
                "qdrant_collection": "chunks",
                "reranker_enabled_by_default": False,
            }
            config = load_rag_config(base_overrides)
            embedding = CountingEmbedding()
            store = QdrantVectorStore(
                path=store_path / "qdrant",
                collection_name="chunks",
                vector_size=3,
            )
            kb = PersonalKnowledgeBase(
                workspace_root=workspace,
                store_path=store_path,
                config=config,
                embedding_provider=embedding,
                vector_store=store,
            )
            first = kb.index_files("notes", [source])
            doc_id = first["indexed"][0]["doc_id"]
            self.assertEqual(embedding.calls, 1)

            unchanged = kb.index_files("notes", [source])
            self.assertEqual(unchanged["indexed"][0]["status"], "unchanged")
            self.assertEqual(embedding.calls, 1)

            changed_config = load_rag_config({**base_overrides, "embedding_model": "BAAI/bge-m3-revision"})
            migrated = PersonalKnowledgeBase(
                workspace_root=workspace,
                store_path=store_path,
                config=changed_config,
                embedding_provider=embedding,
                vector_store=store,
            )
            refreshed = migrated.index_files("notes", [source])
            point_ids = store.document_point_ids(doc_id)
            chunk_ids = [chunk["chunk_id"] for chunk in migrated.list_chunks(doc_id)]

        self.assertTrue(refreshed["indexed"][0]["refreshed"])
        self.assertEqual(embedding.calls, 2)
        self.assertEqual(point_ids, chunk_ids)

    def test_production_model_failure_is_reported_without_deterministic_fallback(self):
        from backend.rag.config import load_rag_config
        from backend.rag.retrieval import QdrantVectorStore
        from backend.rag.service import PersonalKnowledgeBase

        class FakeEmbedding:
            model_id = "BAAI/bge-m3"
            dimension = 3

            def embed(self, texts):
                return [[1.0, 0.0, 0.0] for _ in texts]

        with _test_directory() as root:
            workspace = root / "workspace"
            store_path = root / "knowledge_base"
            workspace.mkdir()
            source = workspace / "production.md"
            source.write_text("production exact token", encoding="utf-8")
            overrides = {
                "enabled": True,
                "retrieval_profile": "production",
                "knowledge_store_path": str(store_path),
                "model_cache_path": str(root / "missing-model-cache"),
                "embedding_provider": "huggingface_local",
                "embedding_model": "BAAI/bge-m3",
                "embedding_dimension": 3,
                "vector_backend": "qdrant",
                "qdrant_collection": "chunks",
                "reranker_enabled_by_default": False,
            }
            config = load_rag_config(overrides)
            store = QdrantVectorStore(
                path=store_path / "qdrant",
                collection_name="chunks",
                vector_size=3,
            )
            seed = PersonalKnowledgeBase(
                workspace_root=workspace,
                store_path=store_path,
                config=config,
                embedding_provider=FakeEmbedding(),
                vector_store=store,
            )
            seed.index_files("notes", [source])
            production = PersonalKnowledgeBase(
                workspace_root=workspace,
                store_path=store_path,
                config=config,
                vector_store=store,
            )
            result = production.search("production exact token", collection="notes")

        self.assertEqual(result["status"], "error")
        self.assertEqual(result["error"]["code"], "backend_unavailable")
        self.assertEqual(result["results"], [])
        self.assertEqual(result["settings"]["vector_backend"], "qdrant")

    def test_metadata_filters_restrict_lexical_and_dense_candidates(self):
        from backend.rag.config import load_rag_config
        from backend.rag.retrieval import QdrantVectorStore
        from backend.rag.service import PersonalKnowledgeBase

        class FakeEmbedding:
            model_id = "BAAI/bge-m3"
            dimension = 3

            def embed(self, texts):
                return [[1.0, 0.0, 0.0] for _ in texts]

        with _test_directory() as root:
            workspace = root / "workspace"
            store_path = root / "knowledge_base"
            workspace.mkdir()
            source = workspace / "filtered.md"
            source.write_text("filterable exact token", encoding="utf-8")
            config = load_rag_config(
                {
                    "enabled": True,
                    "retrieval_profile": "production",
                    "knowledge_store_path": str(store_path),
                    "embedding_provider": "huggingface_local",
                    "embedding_model": "BAAI/bge-m3",
                    "embedding_dimension": 3,
                    "vector_backend": "qdrant",
                    "qdrant_collection": "chunks",
                    "reranker_enabled_by_default": False,
                }
            )
            kb = PersonalKnowledgeBase(
                workspace_root=workspace,
                store_path=store_path,
                config=config,
                embedding_provider=FakeEmbedding(),
                vector_store=QdrantVectorStore(
                    path=store_path / "qdrant",
                    collection_name="chunks",
                    vector_size=3,
                ),
            )
            kb.index_files("notes", [source], metadata={"scope": "private"})
            included = kb.search("filterable exact token", collection="notes", filters={"scope": "private"})
            excluded = kb.search("filterable exact token", collection="notes", filters={"scope": "public"})

        self.assertTrue(included["results"])
        self.assertEqual(excluded["results"], [])


if __name__ == "__main__":
    unittest.main()
