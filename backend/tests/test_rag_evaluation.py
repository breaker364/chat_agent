import json
import shutil
import unittest
from pathlib import Path
from uuid import uuid4


def _load_eval_symbols():
    try:
        from backend.rag.evaluation import (
            BenchmarkDataset,
            DatasetAcquirer,
            EvaluationConfig,
            EvaluationHarness,
            compute_ragas_metrics,
            compute_answer_metrics,
            compute_retrieval_metrics,
            load_default_dataset_registry,
        )
    except ModuleNotFoundError as exc:
        raise AssertionError(f"RAG evaluation module missing: {exc}") from exc
    return (
        BenchmarkDataset,
        DatasetAcquirer,
        EvaluationConfig,
        EvaluationHarness,
        compute_ragas_metrics,
        compute_answer_metrics,
        compute_retrieval_metrics,
        load_default_dataset_registry,
    )


class RagEvaluationTests(unittest.TestCase):
    def setUp(self):
        temp_root = Path.cwd() / "tmp" / "unittest"
        temp_root.mkdir(parents=True, exist_ok=True)
        self.temp_dir = temp_root / f"{self._testMethodName}-{uuid4().hex}"
        self.temp_dir.mkdir(parents=True, exist_ok=False)

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_retrieval_metrics_include_ndcg_recall_mrr_map_and_failures(self):
        _, _, _, _, _, _, compute_retrieval_metrics, _ = _load_eval_symbols()
        qrels = {"q1": {"d1": 1}, "q2": {"d3": 1}}
        rankings = {"q1": ["d2", "d1"], "q2": ["d4", "d5"]}

        metrics = compute_retrieval_metrics(qrels, rankings, cutoffs=(1, 2))

        self.assertIn("ndcg@10", metrics)
        self.assertIn("recall@1", metrics)
        self.assertIn("recall@2", metrics)
        self.assertIn("mrr@10", metrics)
        self.assertIn("map", metrics)
        self.assertEqual(metrics["candidate_coverage"], 0.5)
        self.assertEqual(metrics["failures"][0]["query_id"], "q2")

    def test_answer_metrics_include_exact_match_token_f1_and_citation_support(self):
        _, _, _, _, _, compute_answer_metrics, _, _ = _load_eval_symbols()
        predictions = [
            {
                "query_id": "q1",
                "answer": "weekly calibration",
                "gold_answers": ["weekly calibration"],
                "citations_supported": True,
            },
            {
                "query_id": "q2",
                "answer": "unsupported answer",
                "gold_answers": ["different answer"],
                "citations_supported": False,
            },
        ]

        metrics = compute_answer_metrics(predictions)

        self.assertEqual(metrics["exact_match"], 0.5)
        self.assertLess(metrics["token_f1"], 1.0)
        self.assertEqual(metrics["citation_support_rate"], 0.5)

    def test_smoke_harness_writes_pass_and_fail_reports(self):
        BenchmarkDataset, _, EvaluationConfig, EvaluationHarness, _, _, _, _ = _load_eval_symbols()
        dataset = BenchmarkDataset(
            dataset_id="public-fixture",
            kind="retrieval",
            corpus={"d1": "alpha beta", "d2": "gamma delta"},
            queries={"q1": "alpha"},
            qrels={"q1": {"d1": 1}},
            public_source="fixture public dataset",
        )
        harness = EvaluationHarness(EvaluationConfig(report_dir=self.temp_dir, quality_gates={"average_ndcg@10": 0.1}))

        passed = harness.run([dataset], run_id="passing-smoke")
        failed = EvaluationHarness(
            EvaluationConfig(report_dir=self.temp_dir, quality_gates={"average_ndcg@10": 1.1})
        ).run([dataset], run_id="failing-smoke")

        self.assertEqual(passed["status"], "passed")
        self.assertEqual(failed["status"], "failed")
        for report in (passed, failed):
            self.assertTrue(Path(report["json_summary_path"]).exists())
            self.assertTrue(Path(report["markdown_report_path"]).exists())
            self.assertTrue(report["per_dataset_metric_paths"])
            summary = json.loads(Path(report["json_summary_path"]).read_text(encoding="utf-8"))
            self.assertEqual(summary["run_id"], report["run_id"])
            self.assertIn("environment", summary)
            comparison = summary["datasets"][0]["pipeline_comparisons"]
            self.assertIn("lexical", comparison)
            self.assertIn("dense", comparison)
            self.assertIn("semantic_chunk_hybrid", comparison)
            self.assertIn("semantic_chunk_hybrid_plus_rerank", comparison)

    def test_default_public_dataset_registry_is_data_driven(self):
        _, _, _, _, _, _, _, load_default_dataset_registry = _load_eval_symbols()

        registry = load_default_dataset_registry()
        dataset_ids = {entry["dataset_id"] for entry in registry}

        self.assertIn("beir", dataset_ids)
        self.assertIn("ms-marco-passage", dataset_ids)
        self.assertIn("natural-questions-open", dataset_ids)
        self.assertIn("hotpotqa", dataset_ids)
        self.assertTrue(all("source" in entry for entry in registry))
        self.assertTrue(all("kind" in entry for entry in registry))

    def test_dataset_acquirer_writes_cache_metadata_and_blocks_manual_setup(self):
        _, DatasetAcquirer, _, _, _, _, _, _ = _load_eval_symbols()
        acquirer = DatasetAcquirer(cache_dir=self.temp_dir)

        acquired = acquirer.acquire(
            {
                "dataset_id": "fixture-public",
                "version": "v1",
                "source": "local-fixture",
                "kind": "retrieval",
                "requires_manual_setup": False,
            }
        )
        blocked = acquirer.acquire(
            {
                "dataset_id": "blocked-public",
                "version": "v1",
                "source": "manual",
                "kind": "qa",
                "requires_manual_setup": True,
            }
        )

        self.assertEqual(acquired["status"], "available")
        self.assertTrue(Path(acquired["metadata_path"]).exists())
        self.assertEqual(blocked["status"], "blocked")
        self.assertIn("manual setup", blocked["reason"])

    def test_ragas_metrics_are_blocked_without_judge_model(self):
        _, _, _, _, compute_ragas_metrics, _, _, _ = _load_eval_symbols()

        metrics = compute_ragas_metrics([], judge_model=None)

        self.assertEqual(metrics["status"], "blocked")
        self.assertIn("judge model", metrics["reason"])

    def test_baseline_promotion_requires_passing_run_and_explicit_request(self):
        BenchmarkDataset, _, EvaluationConfig, EvaluationHarness, _, _, _, _ = _load_eval_symbols()
        dataset = BenchmarkDataset(
            dataset_id="public-fixture",
            kind="retrieval",
            corpus={"d1": "alpha beta"},
            queries={"q1": "alpha"},
            qrels={"q1": {"d1": 1}},
            public_source="fixture public dataset",
        )
        harness = EvaluationHarness(EvaluationConfig(report_dir=self.temp_dir, quality_gates={"average_ndcg@10": 0.1}))
        report = harness.run([dataset], run_id="baseline-source")

        rejected = harness.promote_baseline(report, explicit=False)
        promoted = harness.promote_baseline(report, explicit=True)

        self.assertEqual(rejected["status"], "rejected")
        self.assertEqual(promoted["status"], "promoted")
        self.assertTrue(Path(promoted["baseline_path"]).exists())


if __name__ == "__main__":
    unittest.main()
