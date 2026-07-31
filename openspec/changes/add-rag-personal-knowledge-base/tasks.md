## 1. Configuration and Interfaces

- [x] 1.1 Add runtime configuration keys for enabling personal knowledge, knowledge store path, evaluation cache path, embedding provider, vector backend, sparse backend, reranker, semantic chunking settings, retrieval limits, and report directory.
- [x] 1.2 Create `backend/rag/` module structure with provider-neutral interfaces for parsers, chunkers, stores, embeddings, vector indexes, sparse indexes, rerankers, retrieval, and evaluation.
- [x] 1.3 Add an in-memory RAG backend for deterministic unit tests without external services.
- [x] 1.4 Add dependency notes or optional dependency groups for text extraction, sparse retrieval, vector indexing, reranking, and public benchmark evaluation.

## 2. Document Ingestion

- [x] 2.1 Implement parsers for the first supported local file types and structured skipped-file errors for unsupported or inaccessible inputs.
- [x] 2.2 Implement workspace-root path validation so indexing only accepts explicitly selected files under allowed roots.
- [x] 2.3 Implement structure-first semantic chunking that preserves headings, paragraphs, pages, tables, lists, and code blocks before semantic or token-window fallback splitting.
- [x] 2.4 Persist stable chunk ids, token counts, content hashes, parser version, chunker version, chunking strategy, boundary method, overlap metadata, and source offsets.
- [x] 2.5 Implement document manifests and chunk metadata persistence with collection names, source references, statuses, timestamps, and latest error details.
- [x] 2.6 Implement atomic refresh that stages replacement chunks and swaps them into the active collection only after persistence and indexes succeed, including refresh when chunking configuration changes.
- [x] 2.7 Implement document deletion that removes manifests, chunks, sparse index entries, and vector index entries consistently.

## 3. Retrieval Pipeline

- [x] 3.1 Implement lexical retrieval over indexed chunks with configurable candidate depth and metadata filters.
- [x] 3.2 Implement dense embedding and vector retrieval over semantic chunks through the configured embedding and vector index interfaces.
- [x] 3.3 Implement hybrid rank fusion using Reciprocal Rank Fusion as the initial default, duplicate suppression, adjacent chunk handling, configurable reranking, and context packing under token limits.
- [x] 3.4 Implement `knowledge_search` result shaping with snippets, citation ids, provenance metadata, score components, and retrieval settings.
- [x] 3.5 Add retrieval tests covering exact token queries, semantic paraphrases, metadata filters, deleted documents, refreshed documents, reranked results, and bounded snippets.

## 4. Agent Integration

- [x] 4.1 Add structured tools for indexing files, searching knowledge, listing collections, listing documents, deleting documents, and running evaluation.
- [x] 4.2 Register knowledge tools in `get_all_tools()` only when the personal knowledge feature flag is enabled.
- [x] 4.3 Update the system prompt and policy text with generic personal-knowledge retrieval guidance, citation requirements, refusal behavior, and no entity-specific routing rules.
- [x] 4.4 Add tests proving tool schemas are typed, entity-agnostic, and hidden when the feature flag is disabled.
- [x] 4.5 Add integration tests proving the agent can retrieve indexed snippets and cite them without dumping entire documents into logs or session transcripts.

## 5. Benchmark Evaluation Harness

- [x] 5.1 Add a data-driven evaluation registry for public retrieval and QA datasets, including fields, splits, corpus paths, labels, metrics, gates, and license/setup notes.
- [x] 5.2 Implement dataset acquisition and cache metadata with dataset version, checksum when available, source, split, sample limit, and preprocessing settings.
- [x] 5.3 Implement retrieval metrics: nDCG@10, Recall@5, Recall@10, Recall@20, MRR@10, MAP, candidate coverage, and per-query failure examples.
- [x] 5.4 Implement QA metrics: exact match, token F1, grounded-answer rate, citation support rate, and refusal accuracy where no-answer labels are configured.
- [x] 5.5 Implement optional RAGAS-compatible metrics for context precision, context recall, faithfulness, and answer correctness when a judge model is configured.
- [x] 5.6 Implement lexical baseline, dense run, semantic-chunk hybrid run, and semantic-chunk hybrid-plus-rerank comparisons in the same report.
- [x] 5.7 Implement quality gate evaluation and failed-report behavior for metric regressions or blocked datasets.
- [x] 5.8 Implement baseline promotion only for explicit requests after a passing full-suite run.

## 6. Reports and Operational Safety

- [x] 6.1 Write benchmark artifacts as Markdown report, JSON summary, per-dataset metrics, run configuration, environment metadata, and bounded failure examples.
- [x] 6.2 Keep benchmark corpora and reports under evaluation cache paths separate from personal knowledge indexes.
- [x] 6.3 Add privacy-aware logging so indexing and retrieval outputs expose metadata and bounded snippets, not full personal documents.
- [x] 6.4 Add static or focused tests that prevent entity-specific domain, keyword, or routing maps from being introduced into RAG code and prompt additions.

## 7. Verification

- [x] 7.1 Run unit tests for ingestion, chunking, manifest persistence, refresh, deletion, retrieval, tool schemas, and prompt-policy behavior.
- [x] 7.2 Run evaluator smoke tests with fixture corpora and verify pass and fail report paths.
- [x] 7.3 Run a sampled public benchmark suite and record blocked datasets, metric values, and report paths.
- [x] 7.4 Run the configured full public benchmark suite before enabling RAG tools by default. Report: `tmp/rag_eval_reports/rag-full-public-registry-20260731.summary.json` (status: blocked; BEIR, MS MARCO Passage, Natural Questions Open, and HotpotQA require manual setup/license preparation).
- [x] 7.5 Run OpenSpec validation for `add-rag-personal-knowledge-base` in strict mode.

## 8. Project Knowledge Base UX and Sync

- [x] 8.1 Add configuration and initialization for project-local `knowledge_base/` subdirectories for source documents, generated indexes, manifests, and sync reports.
- [x] 8.2 Add frontend document import and drag/drop handling that places or registers user-selected files under the configured knowledge-base source area.
- [x] 8.3 Add a frontend manual sync action backed by a reconciliation API/tool that detects new, changed, unchanged, skipped, failed, and deleted documents.
- [x] 8.4 Update sync behavior so deleting files from `knowledge_base/` or deleting documents in the frontend removes the corresponding manifests, chunks, sparse entries, and vector entries.
- [x] 8.5 Add a user-selectable knowledge-base answer mode that passes retrieval intent to the agent and requires knowledge citations for supported factual claims.
- [x] 8.6 Render frontend citations from structured retrieval metadata, including source title/reference, collection, chunk id, snippet, and page/heading/section metadata when available.
- [x] 8.7 Add tests for project-folder sync, frontend import/delete flows, no-op sync, failed sync rollback, and citation rendering.
- [x] 8.8 Re-run OpenSpec validation and focused RAG/frontend tests after implementing the UX and sync changes.
