# Public RAG Retrieval Evaluation Design

## Goal

Measure the production RAG retrieval stack on a reproducible, cross-language public benchmark suite. The evaluation covers document retrieval only and reports nDCG, Recall, MRR, and MAP for the lexical, dense, hybrid, and hybrid-plus-reranker stages.

## Scope

The suite uses BEIR SciFact, BEIR NFCorpus, and the Chinese subset of MIRACL. It evaluates the existing production configuration: BGE-M3 embeddings, BM25, reciprocal-rank fusion, and the configured local BGE reranker.

The suite does not call an LLM, generate answers, compute RAGAS metrics, modify the default knowledge collection, promote a baseline, or tune retrieval parameters.

## Benchmark Inputs

Each dataset adapter produces a generic corpus, query set, and qrels mapping. The adapter records the upstream source URL, dataset revision when available, sample seed, selected query IDs, document count, and preprocessing rules.

To keep the first run practical on local hardware, every dataset is evaluated as a deterministic retrieval subset. The subset contains selected queries, every corpus document judged relevant to those queries, and a seeded sample of non-relevant corpus documents. Metrics are explicitly labeled as subset metrics and are not presented as full-corpus leaderboard results.

The implementation uses generic dataset identifiers, qrels, and document mappings. It contains no entity-specific branching, domain allowlists, or query rewriting.

## Isolated Execution

Downloaded artifacts live under `tmp/rag_eval_cache`. Per-run documents, Qdrant data, FAISS data, manifests, and reports use a run-specific directory below `tmp/rag_eval_runs/<run-id>`. This prevents the benchmark from reading, updating, or deleting the normal knowledge base.

The run uses a dedicated collection name derived from its run identifier. It is disposable benchmark state only. The report records the effective retrieval signature, model identifiers, backend configuration, operating environment, timing, and any unavailable backend condition.

## Retrieval Variants

For the same indexed chunks and queries, the evaluator produces a document-level ranking for each of these generic stages:

1. BM25 lexical retrieval.
2. BGE-M3 dense retrieval.
3. BM25 plus BGE-M3 reciprocal-rank fusion.
4. Fusion followed by the configured BGE reranker and normal adjacent-context expansion.

Chunk results are mapped back to their dataset document identifiers and deduplicated before scoring. This preserves the public datasets' document-level qrels while exercising the application's normal chunking and retrieval behavior.

## Metrics and Reporting

Each dataset and retrieval variant reports:

- nDCG@10
- Recall@5, Recall@10, and Recall@20
- MRR@10
- MAP

The Markdown summary includes the metrics, variant deltas relative to lexical retrieval, corpus and query counts, elapsed time, source metadata, and bounded examples where no relevant document was retrieved. JSON files preserve the corresponding per-query rankings and aggregate values for repeatable comparisons.

The initial run is observational. It records hybrid-versus-lexical deltas without changing a release baseline or asserting an unvalidated quality threshold.

## Failure Handling

Data acquisition, indexing, and search failures are reported per dataset with their structured error details. A missing local model, unavailable Qdrant backend, or malformed upstream dataset fails only the affected dataset and makes the final summary status incomplete. The report never substitutes deterministic retrieval results for unavailable production components.

## Verification

Automated tests cover adapter normalization, deterministic subset selection, document-level deduplication, metric inputs, isolated path construction, and report generation. The existing deterministic RAG evaluation tests continue to run. A first live benchmark run verifies that the local BGE embedding and reranker models can index and retrieve data through the production stack.
