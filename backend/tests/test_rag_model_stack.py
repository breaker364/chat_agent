import json
import shutil
import unittest
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4


def _load_symbols():
    from backend.rag.config import load_rag_config
    from backend.rag.model_assets import (
        download_configured_huggingface_models,
        resolve_huggingface_model_plan,
    )

    return load_rag_config, resolve_huggingface_model_plan, download_configured_huggingface_models


class RagModelStackTests(unittest.TestCase):
    def setUp(self):
        temp_root = Path.cwd() / "tmp" / "unittest"
        temp_root.mkdir(parents=True, exist_ok=True)
        self.temp_dir = temp_root / uuid4().hex
        self.temp_dir.mkdir(parents=True, exist_ok=False)

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _selected_stack(self):
        return {
            "enabled": True,
            "knowledge_store_path": str(self.temp_dir / "knowledge_base"),
            "vector_backend": "qdrant",
            "embedding_provider": "huggingface_local",
            "embedding_model": "bge-m3",
            "embedding_dimension": 1024,
            "sparse_backend": "qdrant_sparse_or_bm25",
            "hybrid_fusion": "rrf",
            "reranker_provider": "huggingface_local",
            "reranker_model": "bge-reranker-v2-m3",
            "reranker_enabled_by_default": False,
            "reranker_candidate_top_k": 50,
            "final_top_k": 8,
        }

    def test_selected_rag_stack_config_normalizes_model_aliases(self):
        load_rag_config, _, _ = _load_symbols()

        config = load_rag_config(self._selected_stack())

        self.assertEqual(config.vector_backend, "qdrant")
        self.assertEqual(config.retrieval_profile, "production")
        self.assertEqual(config.embedding_provider, "huggingface_local")
        self.assertEqual(config.embedding_model, "BAAI/bge-m3")
        self.assertEqual(config.embedding_dimension, 1024)
        self.assertEqual(config.model_cache_path, self.temp_dir / "knowledge_base" / "models")
        self.assertEqual(config.sparse_backend, "qdrant_sparse_or_bm25")
        self.assertEqual(config.hybrid.fusion_strategy, "rrf")
        self.assertEqual(config.hybrid.top_k, 8)
        self.assertEqual(config.reranker.provider, "huggingface_local")
        self.assertEqual(config.reranker.model, "BAAI/bge-reranker-v2-m3")
        self.assertFalse(config.reranker.enabled)
        self.assertEqual(config.reranker.candidate_top_k, 50)

    def test_huggingface_model_plan_resolves_local_cache_directories(self):
        load_rag_config, resolve_huggingface_model_plan, _ = _load_symbols()
        config = load_rag_config(self._selected_stack())

        plan = resolve_huggingface_model_plan(config)

        self.assertEqual([item["role"] for item in plan], ["embedding", "reranker"])
        self.assertEqual(plan[0]["repo_id"], "BAAI/bge-m3")
        self.assertEqual(plan[1]["repo_id"], "BAAI/bge-reranker-v2-m3")
        self.assertEqual(plan[0]["local_dir"], str(config.model_cache_path / "embedding" / "BAAI__bge-m3"))
        self.assertEqual(plan[1]["local_dir"], str(config.model_cache_path / "reranker" / "BAAI__bge-reranker-v2-m3"))

    def test_download_configured_huggingface_models_writes_manifest(self):
        load_rag_config, _, download_configured_huggingface_models = _load_symbols()
        config = load_rag_config(self._selected_stack())

        def fake_snapshot_download(*, repo_id, local_dir, **_kwargs):
            target = Path(local_dir)
            target.mkdir(parents=True, exist_ok=True)
            (target / "config.json").write_text(json.dumps({"repo_id": repo_id}), encoding="utf-8")
            (target / "model.safetensors").write_bytes(b"weights")
            return str(target)

        with patch("backend.rag.model_assets.snapshot_download", side_effect=fake_snapshot_download) as mocked:
            result = download_configured_huggingface_models(config)

        self.assertEqual(mocked.call_count, 2)
        self.assertIn("allow_patterns", mocked.call_args_list[0].kwargs)
        self.assertIn("*.safetensors", mocked.call_args_list[0].kwargs["allow_patterns"])
        self.assertNotIn("*.jpg", mocked.call_args_list[0].kwargs["allow_patterns"])
        self.assertEqual(result["status"], "downloaded")
        self.assertEqual(len(result["models"]), 2)
        manifest = config.model_cache_path / "manifest.json"
        self.assertTrue(manifest.exists())
        manifest_data = json.loads(manifest.read_text(encoding="utf-8"))
        self.assertEqual(manifest_data["embedding_model"], "BAAI/bge-m3")
        self.assertEqual(manifest_data["reranker_model"], "BAAI/bge-reranker-v2-m3")

    def test_download_reports_failure_when_snapshot_has_no_weights(self):
        load_rag_config, _, download_configured_huggingface_models = _load_symbols()
        config = load_rag_config(self._selected_stack())

        def fake_snapshot_download(*, repo_id, local_dir, **_kwargs):
            target = Path(local_dir)
            target.mkdir(parents=True, exist_ok=True)
            (target / "config.json").write_text(json.dumps({"repo_id": repo_id}), encoding="utf-8")
            return str(target)

        with patch("backend.rag.model_assets.snapshot_download", side_effect=fake_snapshot_download):
            result = download_configured_huggingface_models(config)

        self.assertEqual(result["status"], "failed")
        self.assertEqual(len(result["errors"]), 2)
        self.assertIn("no model weight files", result["errors"][0]["error"])

    def test_download_reports_failure_when_snapshot_has_incomplete_artifacts(self):
        load_rag_config, _, download_configured_huggingface_models = _load_symbols()
        config = load_rag_config(self._selected_stack())

        def fake_snapshot_download(*, repo_id, local_dir, **_kwargs):
            target = Path(local_dir)
            download_cache = target / ".cache" / "huggingface" / "download"
            download_cache.mkdir(parents=True, exist_ok=True)
            (target / "model.safetensors").write_bytes(b"partial-weights")
            (download_cache / "model.safetensors.lock").write_bytes(b"")
            (download_cache / "payload.incomplete").write_bytes(b"")
            return str(target)

        with patch("backend.rag.model_assets.snapshot_download", side_effect=fake_snapshot_download):
            result = download_configured_huggingface_models(config)

        self.assertEqual(result["status"], "failed")
        self.assertEqual(len(result["errors"]), 2)
        self.assertIn("incomplete model download artifacts", result["errors"][0]["error"])

    def test_download_ignores_incomplete_non_model_asset_artifacts(self):
        load_rag_config, _, download_configured_huggingface_models = _load_symbols()
        config = load_rag_config(self._selected_stack())

        def fake_snapshot_download(*, repo_id, local_dir, **_kwargs):
            target = Path(local_dir)
            stale_asset_cache = target / ".cache" / "huggingface" / "download" / "imgs"
            stale_asset_cache.mkdir(parents=True, exist_ok=True)
            (target / "model.safetensors").write_bytes(b"weights")
            (stale_asset_cache / "preview.incomplete").write_bytes(b"")
            return str(target)

        with patch("backend.rag.model_assets.snapshot_download", side_effect=fake_snapshot_download):
            result = download_configured_huggingface_models(config)

        self.assertEqual(result["status"], "downloaded")
        self.assertEqual(result["errors"], [])

    def test_runtime_config_contains_selected_rag_stack(self):
        runtime = json.loads(Path("runtime_config.json").read_text(encoding="utf-8"))
        rag = runtime.get("rag", {})

        self.assertEqual(rag.get("vector_backend"), "qdrant")
        self.assertEqual(rag.get("retrieval_profile"), "production")
        self.assertEqual(rag.get("embedding_provider"), "huggingface_local")
        self.assertEqual(rag.get("embedding_model"), "bge-m3")
        self.assertEqual(rag.get("reranker_model"), "bge-reranker-v2-m3")
        self.assertTrue(rag.get("reranker_enabled_by_default"))
        self.assertEqual(rag.get("reranker_candidate_top_k"), 50)
        self.assertEqual(rag.get("final_top_k"), 8)


if __name__ == "__main__":
    unittest.main()
