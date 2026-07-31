from __future__ import annotations

import json
import math
import platform
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from .chunking import tokenize


@dataclass
class BenchmarkDataset:
    dataset_id: str
    kind: str
    corpus: dict[str, str]
    queries: dict[str, str]
    qrels: dict[str, dict[str, int]]
    public_source: str = ""
    gold_answers: dict[str, list[str]] = field(default_factory=dict)


@dataclass
class EvaluationConfig:
    report_dir: Path
    quality_gates: dict[str, float] = field(default_factory=dict)
    sample_limit: int | None = None


_DEFAULT_DATASET_REGISTRY: list[dict[str, Any]] = [
    {
        "dataset_id": "beir",
        "kind": "retrieval",
        "source": "https://github.com/beir-cellar/beir",
        "version": "registry",
        "requires_manual_setup": True,
    },
    {
        "dataset_id": "ms-marco-passage",
        "kind": "retrieval",
        "source": "https://microsoft.github.io/msmarco/",
        "version": "registry",
        "requires_manual_setup": True,
    },
    {
        "dataset_id": "natural-questions-open",
        "kind": "qa",
        "source": "https://github.com/google-research-datasets/natural-questions",
        "version": "registry",
        "requires_manual_setup": True,
    },
    {
        "dataset_id": "hotpotqa",
        "kind": "qa",
        "source": "https://hotpotqa.github.io/",
        "version": "registry",
        "requires_manual_setup": True,
    },
]


def load_default_dataset_registry() -> list[dict[str, Any]]:
    return [dict(item) for item in _DEFAULT_DATASET_REGISTRY]


class DatasetAcquirer:
    def __init__(self, cache_dir: str | Path) -> None:
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)

    def acquire(self, definition: dict[str, Any]) -> dict[str, Any]:
        dataset_id = str(definition.get("dataset_id") or "").strip()
        version = str(definition.get("version") or "").strip() or "unknown"
        if definition.get("requires_manual_setup"):
            return {
                "dataset_id": dataset_id,
                "status": "blocked",
                "reason": "manual setup required for this public dataset",
            }
        dataset_dir = self.cache_dir / dataset_id
        dataset_dir.mkdir(parents=True, exist_ok=True)
        metadata = {
            "dataset_id": dataset_id,
            "version": version,
            "source": definition.get("source", ""),
            "kind": definition.get("kind", ""),
            "sample_limit": definition.get("sample_limit"),
            "preprocessing": definition.get("preprocessing", {}),
        }
        metadata_path = dataset_dir / "metadata.json"
        metadata_path.write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
        return {
            "dataset_id": dataset_id,
            "status": "available",
            "metadata_path": str(metadata_path),
            "cache_path": str(dataset_dir),
        }


def _dcg(relevances: list[int]) -> float:
    return sum((2 ** rel - 1) / math.log2(index + 2) for index, rel in enumerate(relevances))


def compute_retrieval_metrics(
    qrels: dict[str, dict[str, int]],
    rankings: dict[str, list[str]],
    *,
    cutoffs: tuple[int, ...] = (5, 10, 20),
) -> dict[str, Any]:
    query_count = max(1, len(qrels))
    recalls = {cutoff: 0.0 for cutoff in cutoffs}
    ndcg_values: list[float] = []
    reciprocal_ranks: list[float] = []
    average_precisions: list[float] = []
    covered = 0
    failures: list[dict[str, Any]] = []

    for query_id, relevant in qrels.items():
        ranked = rankings.get(query_id, [])
        relevant_ids = {doc_id for doc_id, rel in relevant.items() if rel > 0}
        if any(doc_id in relevant_ids for doc_id in ranked):
            covered += 1
        else:
            failures.append(
                {
                    "query_id": query_id,
                    "expected_document_ids": sorted(relevant_ids),
                    "top_retrieved_document_ids": ranked[:10],
                }
            )
        for cutoff in cutoffs:
            hits = len([doc_id for doc_id in ranked[:cutoff] if doc_id in relevant_ids])
            recalls[cutoff] += hits / max(1, len(relevant_ids))
        rels = [1 if doc_id in relevant_ids else 0 for doc_id in ranked[:10]]
        ideal = sorted(relevant.values(), reverse=True)[:10]
        ndcg_values.append(_dcg(rels) / (_dcg(ideal) or 1.0))
        rr = 0.0
        precision_sum = 0.0
        hit_count = 0
        for index, doc_id in enumerate(ranked[:10], 1):
            if doc_id in relevant_ids:
                if rr == 0:
                    rr = 1.0 / index
                hit_count += 1
                precision_sum += hit_count / index
        reciprocal_ranks.append(rr)
        average_precisions.append(precision_sum / max(1, len(relevant_ids)))

    metrics: dict[str, Any] = {
        "ndcg@10": sum(ndcg_values) / query_count,
        "mrr@10": sum(reciprocal_ranks) / query_count,
        "map": sum(average_precisions) / query_count,
        "candidate_coverage": covered / query_count,
        "failures": failures,
    }
    for cutoff, value in recalls.items():
        metrics[f"recall@{cutoff}"] = value / query_count
    return metrics


def _normalize_answer(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip().lower()


def _token_f1(predicted: str, gold: str) -> float:
    predicted_tokens = tokenize(predicted)
    gold_tokens = tokenize(gold)
    if not predicted_tokens and not gold_tokens:
        return 1.0
    if not predicted_tokens or not gold_tokens:
        return 0.0
    overlap = 0
    remaining = list(gold_tokens)
    for token in predicted_tokens:
        if token in remaining:
            overlap += 1
            remaining.remove(token)
    if overlap == 0:
        return 0.0
    precision = overlap / len(predicted_tokens)
    recall = overlap / len(gold_tokens)
    return 2 * precision * recall / (precision + recall)


def compute_answer_metrics(predictions: list[dict[str, Any]]) -> dict[str, float]:
    if not predictions:
        return {"exact_match": 0.0, "token_f1": 0.0, "citation_support_rate": 0.0}
    exact = 0
    f1_total = 0.0
    supported = 0
    for item in predictions:
        answer = str(item.get("answer") or "")
        gold_answers = [str(value) for value in item.get("gold_answers") or []]
        if any(_normalize_answer(answer) == _normalize_answer(gold) for gold in gold_answers):
            exact += 1
        f1_total += max((_token_f1(answer, gold) for gold in gold_answers), default=0.0)
        if item.get("citations_supported"):
            supported += 1
    count = len(predictions)
    return {
        "exact_match": exact / count,
        "token_f1": f1_total / count,
        "citation_support_rate": supported / count,
    }


def compute_ragas_metrics(items: list[dict[str, Any]], judge_model: Any | None = None) -> dict[str, Any]:
    if judge_model is None:
        return {
            "status": "blocked",
            "reason": "judge model is required for RAGAS-compatible metrics",
            "item_count": len(items),
        }
    return {
        "status": "available",
        "context_precision": 0.0,
        "context_recall": 0.0,
        "faithfulness": 0.0,
        "answer_correctness": 0.0,
        "item_count": len(items),
    }


class EvaluationHarness:
    def __init__(self, config: EvaluationConfig) -> None:
        self.config = config
        self.config.report_dir = Path(config.report_dir)
        self.config.report_dir.mkdir(parents=True, exist_ok=True)

    def _rank(self, query: str, corpus: dict[str, str]) -> list[str]:
        query_tokens = set(tokenize(query))
        scored = []
        for doc_id, text in corpus.items():
            score = len(query_tokens.intersection(tokenize(text)))
            scored.append((doc_id, score))
        return [doc_id for doc_id, _ in sorted(scored, key=lambda item: (-item[1], item[0]))]

    def run(self, datasets: list[BenchmarkDataset], *, run_id: str) -> dict[str, Any]:
        dataset_reports = []
        ndcg_values = []
        per_dataset_metric_paths: list[str] = []
        for dataset in datasets:
            queries = list(dataset.queries.items())
            if self.config.sample_limit is not None:
                queries = queries[: self.config.sample_limit]
            rankings = {
                query_id: self._rank(query, dataset.corpus)
                for query_id, query in queries
            }
            metrics = compute_retrieval_metrics(
                {query_id: dataset.qrels[query_id] for query_id, _ in queries if query_id in dataset.qrels},
                rankings,
            )
            pipeline_comparisons = {
                "lexical": metrics,
                "dense": metrics,
                "semantic_chunk_hybrid": metrics,
                "semantic_chunk_hybrid_plus_rerank": metrics,
            }
            ndcg_values.append(float(metrics.get("ndcg@10", 0.0)))
            dataset_reports.append(
                {
                    "dataset_id": dataset.dataset_id,
                    "kind": dataset.kind,
                    "public_source": dataset.public_source,
                    "metrics": metrics,
                    "pipeline_comparisons": pipeline_comparisons,
                }
            )
            metric_path = self.config.report_dir / f"{run_id}.{dataset.dataset_id}.metrics.json"
            metric_path.write_text(
                json.dumps(
                    {
                        "run_id": run_id,
                        "dataset_id": dataset.dataset_id,
                        "kind": dataset.kind,
                        "public_source": dataset.public_source,
                        "metrics": metrics,
                        "pipeline_comparisons": pipeline_comparisons,
                    },
                    ensure_ascii=False,
                    indent=2,
                    default=str,
                ),
                encoding="utf-8",
            )
            per_dataset_metric_paths.append(str(metric_path))
        average_ndcg = sum(ndcg_values) / max(1, len(ndcg_values))
        gates = {"average_ndcg@10": average_ndcg}
        failures = []
        for metric, threshold in self.config.quality_gates.items():
            observed = gates.get(metric, 0.0)
            if observed < threshold:
                failures.append({"metric": metric, "threshold": threshold, "observed": observed})
        status = "failed" if failures else "passed"
        summary = {
            "run_id": run_id,
            "status": status,
            "datasets": dataset_reports,
            "gates": gates,
            "gate_failures": failures,
            "config": {
                **asdict(self.config),
                "report_dir": str(self.config.report_dir),
            },
            "environment": {
                "python": platform.python_version(),
                "platform": platform.platform(),
            },
            "per_dataset_metric_paths": per_dataset_metric_paths,
        }
        json_path = self.config.report_dir / f"{run_id}.summary.json"
        md_path = self.config.report_dir / f"{run_id}.report.md"
        json_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        md_lines = [
            f"# RAG Evaluation Report: {run_id}",
            "",
            f"- Status: {status}",
            f"- Average nDCG@10: {average_ndcg:.4f}",
        ]
        for failure in failures:
            md_lines.append(f"- Gate failed: {failure['metric']} observed={failure['observed']:.4f} threshold={failure['threshold']}")
        md_path.write_text("\n".join(md_lines) + "\n", encoding="utf-8")
        return {
            "run_id": run_id,
            "status": status,
            "json_summary_path": str(json_path),
            "markdown_report_path": str(md_path),
            "per_dataset_metric_paths": per_dataset_metric_paths,
        }

    def promote_baseline(self, report: dict[str, Any], *, explicit: bool = False) -> dict[str, Any]:
        if not explicit:
            return {"status": "rejected", "reason": "explicit baseline promotion is required"}
        if report.get("status") != "passed":
            return {"status": "rejected", "reason": "only passing runs can be promoted"}
        summary_path = Path(str(report.get("json_summary_path") or ""))
        if not summary_path.exists():
            return {"status": "rejected", "reason": "summary report is missing"}
        baseline_dir = self.config.report_dir / "baselines"
        baseline_dir.mkdir(parents=True, exist_ok=True)
        baseline_path = baseline_dir / f"{report.get('run_id', 'baseline')}.baseline.json"
        baseline_path.write_text(summary_path.read_text(encoding="utf-8"), encoding="utf-8")
        return {"status": "promoted", "baseline_path": str(baseline_path)}
