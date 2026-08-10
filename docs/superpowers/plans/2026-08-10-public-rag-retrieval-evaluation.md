# Public RAG Retrieval Evaluation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (- [ ]) syntax for tracking.

**Goal:** Run a reproducible, isolated public retrieval benchmark against the production BGE-M3, BM25, RRF, and reranker pipeline, reporting nDCG, Recall, MRR, and MAP.

**Architecture:** A public-dataset adapter materializes deterministic qrels-aware subsets from three configured ir_datasets sources. A benchmark runner indexes each subset into a run-local production knowledge base, converts stage chunk rankings to public document rankings, and passes them to the existing evaluation module for metric and report generation.

**Tech Stack:** Python 3.12, ir-datasets, local BGE-M3 and BGE reranker through Transformers, embedded Qdrant, FAISS, BM25, unittest, pytest.

## Global Constraints

- This is retrieval-only. Do not call an LLM, generate answers, compute RAGAS, promote a baseline, or tune retrieval parameters.
- Use BEIR SciFact, BEIR NFCorpus, and MIRACL Chinese through data records. Selection must be generic and qrels-driven.
- Do not add entity-, brand-, person-, location-, product-, school-, or domain-specific decision logic.
- Keep data cache, documents, Qdrant, FAISS, manifests, and reports under tmp/rag_eval_*; never read, update, or delete knowledge_base.
- Report unavailable models, Qdrant, malformed upstream data, and acquisition failures. Never replace production retrieval with deterministic scores.
- The first run is observational and reports lexical-relative deltas without introducing release thresholds or baselines.
- Do not use emoji in user-visible code or documentation.

---

## File Structure

| Path | Responsibility |
| --- | --- |
| requirements.txt | Declares the public IR dataset reader. |
| backend/rag/evaluation.py | Computes graded metrics and writes multi-stage report artifacts from provided rankings. |
| backend/rag/service.py | Exposes the actual lexical, dense, fusion, and rerank stage rankings. |
| backend/rag/public_benchmark.py | Loads public data, selects subsets, indexes isolated corpora, maps chunks to documents, and runs evaluation. |
| backend/rag/benchmark_cli.py | Provides a human-invoked command for a live benchmark run. |
| backend/tests/test_rag_evaluation.py | Tests metric semantics and supplied ranking reports. |
| backend/tests/test_rag_core.py | Tests the stage-ranking contract. |
| backend/tests/test_rag_public_benchmark.py | Tests offline loader injection, subset selection, isolation, and CLI forwarding. |
| docs/rag-public-benchmark.md | Documents provenance, execution, subset scope, and metric interpretation. |

### Task 1: Support Graded Metrics and Supplied Variant Rankings

**Files:**
- Modify: backend/rag/evaluation.py:13-330
- Modify: backend/tests/test_rag_evaluation.py:1-164

**Interfaces:**
- Add BenchmarkDataset.metadata: dict[str, Any] = field(default_factory=dict).
- Add EvaluationHarness.run_rankings(datasets, rankings_by_dataset, *, run_id, run_metadata=None, primary_variant="semantic_chunk_hybrid_plus_rerank", dataset_errors=None) -> dict[str, Any].
- Keep EvaluationHarness.run as a backwards-compatible smoke wrapper.

- [ ] **Step 1: Write failing tests for graded relevance and independent pipeline scores**

~~~python
def test_retrieval_metrics_use_graded_qrels_for_ndcg(self):
    *_, compute_retrieval_metrics, _ = _load_eval_symbols()
    qrels = {"q1": {"d-high": 2, "d-low": 1}}
    high_first = compute_retrieval_metrics(qrels, {"q1": ["d-high", "d-low"]})
    low_first = compute_retrieval_metrics(qrels, {"q1": ["d-low", "d-high"]})
    self.assertGreater(high_first["ndcg@10"], low_first["ndcg@10"])

def test_harness_reports_supplied_variant_rankings(self):
    BenchmarkDataset, _, EvaluationConfig, EvaluationHarness, *_ = _load_eval_symbols()
    dataset = BenchmarkDataset("fixture", "retrieval", {"d1": "one", "d2": "two"},
        {"q1": "one"}, {"q1": {"d1": 1}}, metadata={"subset": True, "seed": 7})
    rankings = {"fixture": {
        "lexical": {"q1": ["d2", "d1"]},
        "dense": {"q1": ["d1", "d2"]},
        "semantic_chunk_hybrid": {"q1": ["d1", "d2"]},
        "semantic_chunk_hybrid_plus_rerank": {"q1": ["d1", "d2"]},
    }}
    report = EvaluationHarness(EvaluationConfig(report_dir=self.temp_dir)).run_rankings(
        [dataset], rankings, run_id="variants")
    summary = json.loads(Path(report["json_summary_path"]).read_text(encoding="utf-8"))
    self.assertGreater(summary["datasets"][0]["pipeline_comparisons"]["dense"]["ndcg@10"], 0.0)
    self.assertIn("relative_to_lexical", summary["datasets"][0])
~~~

- [ ] **Step 2: Verify the tests fail before implementation**

Run:

~~~powershell
& .venv_py312\Scripts\python.exe -m pytest backend/tests/test_rag_evaluation.py -q -p no:cacheprovider
~~~

Expected: FAIL because BenchmarkDataset does not accept metadata and EvaluationHarness lacks run_rankings.

- [ ] **Step 3: Implement generalized metrics and reports**

Use positive relevance for Recall, MRR, and MAP, and use the actual qrel value for every nDCG rank. Compute MAP over the complete supplied ranking rather than silently truncating it at ten documents. Implement run_rankings to write one metrics JSON per dataset and a full summary. The per-dataset metrics field is the selected primary variant; pipeline_comparisons retains all stages; relative_to_lexical contains only numerical deltas.

Refactor run to calculate its current lexical ranking once and send it under all four legacy stage names through run_rankings, preserving current smoke-test behavior. The new method validates the requested primary variant, computes one metric dictionary per supplied stage, records numeric lexical deltas, writes the JSON and Markdown artifacts, and returns their paths. Render one compact Markdown table per dataset with nDCG@10, Recall@5/10/20, MRR@10, MAP, subset metadata, and bounded failure examples.

- [ ] **Step 4: Verify evaluation regressions pass**

Run:

~~~powershell
& .venv_py312\Scripts\python.exe -m pytest backend/tests/test_rag_evaluation.py -q -p no:cacheprovider
~~~

Expected: PASS.

- [ ] **Step 5: Commit this independently reviewable change**

~~~powershell
git add backend/rag/evaluation.py backend/tests/test_rag_evaluation.py
git commit -m "feat: support ranked RAG benchmark variants"
~~~

### Task 2: Expose Production Retrieval Stages

**Files:**
- Modify: backend/rag/service.py:148-1711
- Modify: backend/tests/test_rag_core.py:100-143

**Interfaces:**
- Add PersonalKnowledgeBase.rank_stages(query, *, collection=None, filters=None, limit=100) -> dict[str, list[dict[str, Any]]].
- Return exactly lexical, dense, semantic_chunk_hybrid, and semantic_chunk_hybrid_plus_rerank stages, each item containing chunk_id, doc_id, and score only.

- [ ] **Step 1: Write the failing stage-ranking contract test**

~~~python
def test_rank_stages_exposes_document_and_chunk_ids(self):
    _, _, _, PersonalKnowledgeBase = _load_rag_symbols()
    first = self.workspace / "first.md"
    second = self.workspace / "second.md"
    first.write_text("alpha retrieval evidence", encoding="utf-8")
    second.write_text("beta retrieval evidence", encoding="utf-8")
    kb = PersonalKnowledgeBase(workspace_root=self.workspace, store_path=self.store_dir,
                               reranker_enabled=True)
    kb.index_files("evaluation", [first, second])
    stages = kb.rank_stages("alpha evidence", collection="evaluation", limit=10)
    self.assertEqual(set(stages), {"lexical", "dense", "semantic_chunk_hybrid",
                                   "semantic_chunk_hybrid_plus_rerank"})
    self.assertTrue(all({"chunk_id", "doc_id", "score"} <= set(hit)
                        for hit in stages["lexical"]))
    final_ids = [hit["chunk_id"] for hit in stages["semantic_chunk_hybrid_plus_rerank"]]
    self.assertEqual(len(final_ids), len(set(final_ids)))
~~~

- [ ] **Step 2: Verify the missing API fails**

Run:

~~~powershell
& .venv_py312\Scripts\python.exe -m pytest backend/tests/test_rag_core.py::RagCoreTests::test_rank_stages_exposes_document_and_chunk_ids -q -p no:cacheprovider
~~~

Expected: FAIL with AttributeError for rank_stages.

- [ ] **Step 3: Extract one shared ranking pipeline and serialize stage results**

Extract the ranking portion of search into one private method. It must produce independent BM25 and dense lists, RRF fusion from those lists, reranking only for the final stage, and adjacent-context expansion only for the final stage. Implement rank_stages as a serialization of that method, with no snippets, excerpts, raw document text, vectors, or manifest payloads. Make search consume the final list from the same private method so the benchmark cannot diverge from production behavior.

~~~python
def rank_stages(self, query: str, *, collection: str | None = None,
                filters: dict[str, Any] | None = None,
                limit: int = 100) -> dict[str, list[dict[str, Any]]]:
    ranked_stages = self._rank_pipeline(query, collection=collection, filters=filters)
    return {
        name: [{"chunk_id": chunk.chunk_id, "doc_id": chunk.doc_id,
                "score": round(score, 6)}
               for chunk, score in ranked[:max(0, int(limit))]]
        for name, ranked in ranked_stages.items()
    }
~~~

rank_stages propagates RagBackendError to its caller. search continues to catch that exception and return its current structured backend-error result. The benchmark runner catches the propagated exception at the dataset boundary and records it as a dataset error.

- [ ] **Step 4: Run core retrieval regressions**

Run:

~~~powershell
& .venv_py312\Scripts\python.exe -m pytest backend/tests/test_rag_core.py -q -p no:cacheprovider
~~~

Expected: PASS.

- [ ] **Step 5: Commit the stage API**

~~~powershell
git add backend/rag/service.py backend/tests/test_rag_core.py
git commit -m "feat: expose RAG retrieval stage rankings"
~~~

### Task 3: Implement a Generic, Isolated Public Benchmark Runner

**Files:**
- Modify: requirements.txt:14-19
- Create: backend/rag/public_benchmark.py
- Create: backend/tests/test_rag_public_benchmark.py

**Interfaces:**
- Add PublicDatasetDefinition(dataset_id, ir_dataset_id, source, language).
- Add load_public_dataset(definition, cache_dir) -> BenchmarkDataset.
- Add select_retrieval_subset(dataset_id, source, documents, queries, qrels, *, seed, query_limit, distractor_limit) -> BenchmarkDataset.
- Add PublicRetrievalBenchmark.run(*, run_id, query_limit=20, distractor_limit=150, seed=20260810) -> dict[str, Any].

- [ ] **Step 1: Write failing offline tests for qrels-aware selection and document deduplication**

~~~python
def test_subset_keeps_relevant_docs_and_is_seeded(self):
    dataset = select_retrieval_subset(
        dataset_id="fixture", source="https://example.invalid/dataset",
        documents={"d1": "one", "d2": "two", "d3": "three", "d4": "four"},
        queries={"q1": "one", "q2": "two"},
        qrels={"q1": {"d1": 1}, "q2": {"d2": 1}},
        seed=11, query_limit=1, distractor_limit=2,
    )
    self.assertEqual(len(dataset.queries), 1)
    self.assertTrue(set(next(iter(dataset.qrels.values()))).issubset(dataset.corpus))
    self.assertEqual(dataset.metadata["seed"], 11)
    self.assertTrue(dataset.metadata["subset"])
~~~

Add a fake knowledge-base factory whose final stage returns two chunks for one internal document. Assert the public ranking contains that source document only once, at its first rank, before passing it to run_rankings.

- [ ] **Step 2: Verify the runner tests fail before the module exists**

Run:

~~~powershell
& .venv_py312\Scripts\python.exe -m pytest backend/tests/test_rag_public_benchmark.py -q -p no:cacheprovider
~~~

Expected: FAIL with ModuleNotFoundError for backend.rag.public_benchmark.

- [ ] **Step 3: Add the reader and implement loading, selection, indexing, and scoring**

Add ir-datasets>=0.5.9 to requirements.txt. Import it inside the live loader only, so tests rely on injected in-memory loaders. Before loading, set IR_DATASETS_HOME to the supplied cache path.

Use the following data records without branching on entity-specific content:

~~~python
PUBLIC_RETRIEVAL_DATASETS = (
    PublicDatasetDefinition("beir-scifact", "beir/scifact/test",
                            "https://github.com/beir-cellar/beir", "en"),
    PublicDatasetDefinition("beir-nfcorpus", "beir/nfcorpus/test",
                            "https://github.com/beir-cellar/beir", "en"),
    PublicDatasetDefinition("miracl-zh", "miracl/zh/dev",
                            "https://project-miracl.github.io/", "zh"),
)
~~~

Select non-empty qrel queries with a local random generator derived from the supplied seed and dataset ID. Keep every qrel-positive document for those queries. From all remaining documents, keep the distractor_limit lowest SHA-256 keys generated from (seed, dataset_id, document_id), which is deterministic and bounded in memory. Preserve title plus body text, filter qrels to retained documents, and record subset, seed, limits, and source in BenchmarkDataset.metadata.

Create every data-dependent path below the run root:

~~~text
tmp/rag_eval_runs/<run-id>/<dataset-id>/workspace/documents/
tmp/rag_eval_runs/<run-id>/<dataset-id>/store/
tmp/rag_eval_runs/<run-id>/<dataset-id>/reports/
~~~

Write selected documents as SHA-256-named Markdown files, retain a resolved filename-to-public-document map, and index through a production RagConfig whose knowledge_store_path and Qdrant collection are run-local while model_cache_path remains the configured local cache. Map index_files results from internal document IDs back to public document IDs through source_uri. For each query call rank_stages with the collection name and a limit of 100, map chunks to public document IDs, retain each document's first occurrence per variant, and pass the four rankings into run_rankings.

The runner must accept dependency injection so tests never need a model or network:

~~~python
class PublicRetrievalBenchmark:
    def __init__(
        self, *, workspace_root: str | Path, cache_dir: str | Path,
        run_root: str | Path,
        dataset_loader: Callable[[PublicDatasetDefinition, Path], BenchmarkDataset] | None = None,
        knowledge_base_factory: Callable[..., PersonalKnowledgeBase] = PersonalKnowledgeBase,
    ) -> None:
        self.workspace_root = Path(workspace_root).resolve()
        self.cache_dir = Path(cache_dir).resolve()
        self.run_root = Path(run_root).resolve()
        self.dataset_loader = dataset_loader or load_public_dataset
        self.knowledge_base_factory = knowledge_base_factory
~~~

Continue other datasets after a dataset-specific exception, append a structured dataset error, and ask the evaluation harness to mark the aggregate report incomplete when any dataset fails.

- [ ] **Step 4: Verify offline public-runner tests**

Run:

~~~powershell
& .venv_py312\Scripts\python.exe -m pytest backend/tests/test_rag_public_benchmark.py backend/tests/test_rag_evaluation.py -q -p no:cacheprovider
~~~

Expected: PASS with no dataset download or model initialization.

- [ ] **Step 5: Commit the runner**

~~~powershell
git add requirements.txt backend/rag/public_benchmark.py backend/tests/test_rag_public_benchmark.py
git commit -m "feat: add isolated public RAG benchmark runner"
~~~

### Task 4: Add a Human-Invoked CLI and Documentation

**Files:**
- Create: backend/rag/benchmark_cli.py
- Create: docs/rag-public-benchmark.md
- Modify: backend/tests/test_rag_public_benchmark.py

**Interfaces:**
- Add main(argv: list[str] | None = None, *, runner_factory: Callable[..., PublicRetrievalBenchmark] = PublicRetrievalBenchmark) -> int.
- Support --run-id, --query-limit, --distractor-limit, and --seed.
- Return 0 only for a complete suite and 1 for an incomplete suite.

- [ ] **Step 1: Write the failing CLI forwarding test**

~~~python
def test_cli_forwards_limits_and_returns_incomplete_status(self):
    calls = []
    class FakeRunner:
        def run(self, **kwargs):
            calls.append(kwargs)
            return {"status": "incomplete", "json_summary_path": "summary.json",
                    "markdown_report_path": "report.md"}
    exit_code = main(
        ["--run-id", "cli-fixture", "--query-limit", "3",
         "--distractor-limit", "5", "--seed", "9"],
        runner_factory=lambda **_: FakeRunner())
    self.assertEqual(exit_code, 1)
    self.assertEqual(calls, [{"run_id": "cli-fixture", "query_limit": 3,
                              "distractor_limit": 5, "seed": 9}])
~~~

- [ ] **Step 2: Verify the CLI test fails before implementation**

Run:

~~~powershell
& .venv_py312\Scripts\python.exe -m pytest backend/tests/test_rag_public_benchmark.py::PublicRagBenchmarkTests::test_cli_forwards_limits_and_returns_incomplete_status -q -p no:cacheprovider
~~~

Expected: FAIL with ModuleNotFoundError for backend.rag.benchmark_cli.

- [ ] **Step 3: Implement the CLI and usage document**

Resolve cache and run roots below the project tmp directory. Print only the status and returned Markdown/JSON paths. Do not expose a networked, high-cost benchmark as an LLM agent tool.

The documentation must include the command, provenance, storage paths, local BGE model requirement, subset caveat, and delta interpretation:

~~~powershell
& .venv_py312\Scripts\python.exe -m pip install -r requirements.txt
& .venv_py312\Scripts\python.exe -m backend.rag.benchmark_cli --run-id public-retrieval-20260810 --query-limit 20 --distractor-limit 150 --seed 20260810
~~~

State that the deterministic subset scores cannot be presented as full-corpus leaderboard results.

- [ ] **Step 4: Verify the full offline evaluation suite**

Run:

~~~powershell
& .venv_py312\Scripts\python.exe -m pytest backend/tests/test_rag_evaluation.py backend/tests/test_rag_core.py backend/tests/test_rag_public_benchmark.py -q -p no:cacheprovider
~~~

Expected: PASS with no public data download.

- [ ] **Step 5: Commit CLI and documentation**

~~~powershell
git add backend/rag/benchmark_cli.py backend/tests/test_rag_public_benchmark.py docs/rag-public-benchmark.md
git commit -m "docs: describe public RAG benchmark execution"
~~~

### Task 5: Install Data Support and Run the Live Suite

**Files:**
- Create at runtime only: tmp/rag_eval_cache/
- Create at runtime only: tmp/rag_eval_runs/public-retrieval-20260810/

**Interfaces:**
- Consumes: backend.rag.benchmark_cli, local model cache, public data sources.
- Produces: one aggregate Markdown/JSON report and per-dataset metrics JSON artifacts.

- [ ] **Step 1: Install and preflight the declared runtime dependency**

Run:

~~~powershell
& .venv_py312\Scripts\python.exe -m pip install -r requirements.txt
& .venv_py312\Scripts\python.exe -c "import ir_datasets, qdrant_client, torch, transformers; from backend.rag.public_benchmark import PublicRetrievalBenchmark; print('public benchmark dependencies ready')"
~~~

Expected: the second command prints public benchmark dependencies ready.

- [ ] **Step 2: Execute the first small public suite**

Run:

~~~powershell
& .venv_py312\Scripts\python.exe -m backend.rag.benchmark_cli --run-id public-retrieval-20260810 --query-limit 20 --distractor-limit 150 --seed 20260810
~~~

Expected: public data downloads only to tmp/rag_eval_cache; all documents and indexes are under tmp/rag_eval_runs/public-retrieval-20260810; the command reports complete or incomplete with per-dataset errors.

- [ ] **Step 3: Inspect the observed report artifacts**

Run:

~~~powershell
Get-Content -Raw tmp\rag_eval_runs\public-retrieval-20260810\reports\public-retrieval-20260810.report.md
Get-Content -Raw tmp\rag_eval_runs\public-retrieval-20260810\reports\public-retrieval-20260810.summary.json
~~~

Expected: each completed dataset reports four stages, nDCG@10, Recall@5/10/20, MRR@10, MAP, lexical-relative deltas, metadata, and bounded failures. An unavailable production dependency is reported as an error, never as a deterministic score.

- [ ] **Step 4: Verify regressions after the live run**

Run:

~~~powershell
& .venv_py312\Scripts\python.exe -m pytest backend/tests/test_rag_evaluation.py backend/tests/test_rag_core.py backend/tests/test_rag_public_benchmark.py -q -p no:cacheprovider
~~~

Expected: PASS. Public artifacts remain ignored under tmp/, and knowledge_base remains unchanged.

- [ ] **Step 5: Commit source, tests, and documentation only**

~~~powershell
git add requirements.txt backend/rag/evaluation.py backend/rag/service.py backend/rag/public_benchmark.py backend/rag/benchmark_cli.py backend/tests/test_rag_evaluation.py backend/tests/test_rag_core.py backend/tests/test_rag_public_benchmark.py docs/rag-public-benchmark.md
git commit -m "feat: evaluate production RAG on public retrieval data"
~~~

## Plan Review

Spec coverage: Tasks 1 through 5 cover all approved datasets, deterministic qrels-aware subsets, isolated storage, current production stages, document deduplication, requested metrics, relative deltas, artifacts, errors, tests, and a real first run. The excluded LLM, RAGAS, baseline, threshold, and tuning work is kept outside scope.

Placeholder scan: the plan has no deferred implementation markers; every code-changing task names exact files, interfaces, test commands, expected results, implementation details, and commit commands.

Type consistency: subset selection returns BenchmarkDataset.metadata; the runner maps rank_stages internal document IDs to qrel document IDs before calling run_rankings; the CLI forwards the exact arguments accepted by PublicRetrievalBenchmark.run.
