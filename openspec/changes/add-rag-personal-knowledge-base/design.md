## Context

The current agent has a tool-based runtime, session persistence, skill discovery, web search/fetch adapters, and prompt policies for scoped tool use. It does not have a durable personal knowledge layer that can ingest user-owned documents, retrieve them later, and ground answers in source evidence.

The proposed RAG capability adds a local-first personal knowledge subsystem behind agent tools. It must work with the existing `backend/tools.py` tool collection, runtime configuration in `backend/config.py`, and prompt policy in `backend/prompts/system_prompt.md`. It must also respect the project rule that code provides generic mechanisms while the LLM interprets user intent. Therefore, retrieval routing cannot depend on hardcoded entity, brand, school, person, place, product, or domain mappings.

Research basis:

- RAG is the established pattern for combining parametric generation with non-parametric retrieved evidence for knowledge-intensive tasks.
- Dense passage retrieval and vector indexes are mature for semantic recall, while lexical retrieval remains strong for exact terms, IDs, filenames, and rare tokens.
- Public benchmarks such as BEIR, MS MARCO Passage, Natural Questions Open, and HotpotQA provide repeatable retrieval and QA validation surfaces.
- RAG-specific evaluators such as RAGAS define useful end-to-end checks for context relevance, answer faithfulness, and answer correctness when labels or judge models are available.

Reference sources consulted:

- Retrieval-Augmented Generation for Knowledge-Intensive NLP Tasks: https://arxiv.org/abs/2005.11401
- BEIR heterogeneous retrieval benchmark: https://arxiv.org/abs/2104.08663
- MS MARCO dataset and passage ranking task: https://microsoft.github.io/msmarco/
- Natural Questions dataset: https://github.com/google-research-datasets/natural-questions
- HotpotQA dataset: https://hotpotqa.github.io/
- RAGAS metrics documentation: https://docs.ragas.io/en/latest/concepts/metrics/available_metrics/
- Faiss vector similarity search: https://github.com/facebookresearch/faiss
- Reciprocal Rank Fusion in hybrid search: https://learn.microsoft.com/en-us/azure/search/hybrid-search-ranking

## Goals / Non-Goals

**Goals:**

- Add a personal knowledge base that can ingest local user-selected documents into durable collections.
- Use a project-local `knowledge_base/` directory as the default personal knowledge workspace, containing both source documents and generated index artifacts.
- Support two user import paths: manual drag/drop into the project knowledge folder and frontend-driven document import.
- Make vectorization/index refresh an explicit frontend sync action so newly added or deleted documents are reconciled on demand.
- Provide a generic retrieval API/tool that supports structure-first semantic chunking, lexical search, dense search, hybrid search, metadata filtering, and reranking.
- Preserve source provenance from document to chunk to answer citation.
- Render citations in the frontend whenever the user chooses to answer from knowledge-base evidence.
- Give the agent clear instructions for using retrieved knowledge and declining unsupported answers.
- Keep model, embedding, vector store, sparse index, reranker, and evaluation datasets behind configuration and interfaces.
- Validate the RAG implementation with a reproducible public-dataset evaluation harness before treating it as production-ready.
- Produce machine-readable reports that can become regression baselines for later changes.

**Non-Goals:**

- Build a multi-user SaaS permission model.
- Automatically crawl arbitrary private cloud drives without explicit user action.
- Replace web search. Web search and personal knowledge retrieval remain separate evidence sources.
- Guarantee perfect answer correctness. The goal is traceable, measurable, bounded behavior with explicit refusal when evidence is insufficient.
- Hardcode behavior for specific real-world entities, domains, or user topics.

## Decisions

### Use a local-first RAG service with explicit provider adapters

Implement a new `backend/rag/` package with provider-neutral interfaces:

- `DocumentParser`: extracts normalized text and structure from supported files.
- `Chunker`: creates stable, semantically coherent chunks with offsets, headings, boundary metadata, and token counts.
- `KnowledgeStore`: persists document manifests, chunk metadata, and collection state.
- `EmbeddingClient`: turns chunk/query text into vectors through a configured provider.
- `VectorIndex`: stores/searches vectors.
- `SparseIndex`: supports BM25 or an equivalent lexical retriever.
- `Reranker`: optionally reranks candidate chunks.
- `RagEvaluator`: runs public-dataset and local regression checks.

The initial implementation can use simple local stores such as SQLite/JSONL plus FAISS or a similarly local vector backend. The design keeps this replaceable by pgvector, Milvus, Qdrant, or another backend through the same interface.

Alternative considered: directly integrate a framework-level RAG chain into the agent. That is faster initially but couples storage, chunking, retrieval policy, and evaluation to one framework and makes privacy and regression behavior harder to control.

### Use project-local `knowledge_base/` as the default workspace

The default personal knowledge store lives under the project root:

- `knowledge_base/documents/`: user-managed source files added by drag/drop, copy, or frontend import.
- `knowledge_base/index/`: generated dense vector index, sparse lexical index, and retrieval metadata.
- `knowledge_base/manifests/`: document manifests, sync state, deletion tombstones when needed, and collection metadata.
- `knowledge_base/reports/`: optional local sync and indexing reports that are safe to inspect from the frontend.

The frontend import flow writes source files into the document area, and the sync action is responsible for parsing, chunking, embedding, and updating index artifacts. Direct filesystem edits are allowed: if the user copies a file into `knowledge_base/documents/` or deletes one from that folder, the next sync compares filesystem state against manifests and reconciles the indexes.

The folder layout is configurable, but the default must be predictable and portable with the project. Runtime code still validates paths against allowed roots and never indexes arbitrary files outside explicit knowledge-base locations or user-selected import paths.

Alternative considered: store all personal knowledge under a global runtime cache. That makes multi-project behavior ambiguous and makes it harder for users to inspect, back up, or delete the knowledge base together with the project.

### Treat collections and document manifests as the source of truth

Each indexed document gets a manifest record:

- `doc_id`: stable hash from collection, normalized source path or external source id, and content hash.
- `collection`: user-visible namespace.
- `source_uri`: local path or opaque external source reference.
- `source_type`: file type or connector type.
- `content_hash`: hash of extracted normalized content.
- `parser_version`, `chunker_version`, `embedding_model`, `indexed_at`, `updated_at`.
- `title`, `mime_type`, `metadata`, `chunk_count`, `status`, and latest error.

Chunks store:

- `chunk_id`, `doc_id`, `collection`, `ordinal`, `text`, `content_hash`.
- `heading_path`, `page`, `section`, `start_offset`, `end_offset`.
- `token_count`, `source_ref`, `chunking_strategy`, `boundary_method`, `overlap_from_previous`, `overlap_to_next`, and optional structured fields for tables.

Refresh compares content hashes and parser/chunker/embedding versions. Changed documents are re-indexed atomically into a staging index and swapped into the active collection only after all chunks are persisted.

Alternative considered: use vector index contents as the only persistence layer. That hides document lifecycle state and makes deletion, provenance, and migration fragile.

### Make sync an explicit user action

Knowledge-base changes are applied by a frontend-triggered sync operation rather than implicit background indexing. Sync performs a deterministic reconciliation pass:

1. Scan configured document folders and imported-document manifests.
2. Detect new files, changed files, unsupported files, missing files, and frontend-deleted documents.
3. Parse and chunk new or changed supported files.
4. Embed chunks and update dense vector and sparse lexical indexes in a staging state.
5. Remove vector, sparse, chunk, and manifest records for files deleted from the folder or deleted through the frontend.
6. Swap staged index state into the active collection only when the reconciliation succeeds.
7. Return a bounded sync report with indexed, refreshed, skipped, deleted, failed, and unchanged counts plus per-file statuses.

This keeps ingestion predictable for users, avoids surprise embedding costs, and lets the frontend show exactly when local documents have become searchable.

Alternative considered: automatically watch the folder and index on every filesystem event. That is convenient but increases privacy, cost, and partial-index risks, especially when users temporarily copy incomplete files into the folder.

### Use structure-first semantic chunking by default

The default chunking pipeline:

1. Parse source documents into structural blocks such as headings, paragraphs, lists, code blocks, pages, and tables.
2. Preserve natural section boundaries first, including heading hierarchy, page references, table boundaries, and code block boundaries.
3. Merge adjacent small blocks when their combined token count stays below the target chunk size and their headings or semantic similarity indicate they belong together.
4. Split oversized blocks using sentence or paragraph boundaries first, then semantic boundary detection when embeddings are configured.
5. Fall back to token-window splitting with configurable overlap only when no reliable structural or semantic boundary is available.
6. Keep tables, code blocks, and short lists intact when possible; when they exceed the max token budget, split them by row/group or logical block rather than arbitrary character ranges.
7. Emit boundary metadata so evaluation can compare semantic chunking against a fixed-window fallback.

Default target settings should be configurable, with an initial recommended range of 300-800 tokens per chunk and 10-15% overlap only for fallback/token-window splits. The chunker must never use entity-specific rules; semantic decisions are based on structure, token budget, local coherence, and configured embedding similarity.

Alternative considered: fixed-size token chunking. It is simple and deterministic, but it often breaks headings, tables, procedures, and multi-paragraph concepts, weakening both retrieval precision and citation quality for a personal knowledge base.

### Use hybrid retrieval plus rank fusion as the default

The default query pipeline:

1. Normalize the query and derive generic retrieval options from user intent and tool arguments.
2. Apply collection and metadata filters.
3. Retrieve candidates from sparse lexical search, such as BM25 or an equivalent lexical retriever.
4. Retrieve candidates from dense vector search over embedded semantic chunks.
5. Fuse candidate ranks using a generic rank-fusion strategy, with Reciprocal Rank Fusion as the initial default unless benchmarking shows a better configured alternative.
6. Deduplicate near-identical or adjacent chunks.
7. Rerank top candidates when reranking is enabled with a configured cross-encoder or LLM reranker.
8. Pack the final context under a token budget and return snippets with source citations.

This default works for personal knowledge because exact-match facts, filenames, issue IDs, commands, names, and dates are common in user documents, while semantic vectors improve recall for paraphrased questions.

Alternative considered: dense-only retrieval. Dense-only search is simpler but weaker for rare tokens and exact strings, which are common in personal corpora.

Reranking remains configurable because it adds latency and cost. Benchmark reports must compare `lexical`, `dense`, `hybrid`, and `hybrid-plus-rerank` runs before enabling reranking by default.

### Keep answer synthesis citation-bound

The agent should call knowledge retrieval tools when a user asks about indexed personal content or when a query explicitly requests knowledge-base lookup. The tool returns ranked snippets, metadata, and citation ids. Final answers must cite retrieved chunks for factual claims derived from the personal corpus.

If retrieved evidence is weak, contradictory, or absent, the agent must say that the knowledge base does not contain enough evidence and either ask for more documents or offer a web search only if appropriate.

The system prompt update should describe behavior generically:

- Prefer personal knowledge retrieval for questions about indexed user documents, prior imported files, or named collections.
- Do not invent facts absent from retrieved snippets.
- Keep personal knowledge citations separate from web citations.
- Do not route based on entity-specific domain or keyword maps.

Alternative considered: hide retrieval inside a single answer tool. A single answer tool is convenient but makes it harder for the agent and user to inspect sources, retry with filters, and debug citation quality. The first version should expose retrieval and answer-grounding separately, while allowing a convenience wrapper later.

### Expose citations as frontend answer evidence

When the user selects a knowledge-base answer mode, the frontend passes that intent to the agent runtime and expects returned answer content plus citation metadata. Citation payloads should include source title, source path or safe source reference, chunk id, heading path, page or section when available, snippet, score/support metadata, and collection name.

The frontend renders citations near the claims they support or in a source panel associated with the answer. If the retrieval tool reports insufficient evidence, the frontend must surface that refusal state instead of showing uncited generated facts as knowledge-base output.

Alternative considered: return plain text answers with inline citation strings only. Structured citation data is easier to render, inspect, test, and preserve across different clients.

### Add narrowly scoped runtime tools

Add structured tools to `get_all_tools()`:

- `knowledge_sync(collection=None, dry_run=False)`: reconcile the project `knowledge_base/` folder and frontend-managed document state with manifests and index artifacts.
- `knowledge_import_files(collection, paths, metadata=None)`: copy or register frontend-selected files into the project knowledge-base document area.
- `knowledge_index_files(collection, paths, refresh=False, metadata=None)`: ingest or refresh selected files under allowed workspace roots; retained as a lower-level operation used by sync/import flows.
- `knowledge_search(query, collection=None, filters=None, top_k=8, include_scores=True)`: retrieve chunks and return citations.
- `knowledge_list_collections()`: list collection summaries and document counts.
- `knowledge_list_documents(collection=None)`: list indexed document manifests without dumping full contents.
- `knowledge_delete_document(doc_id=None, collection=None, source_uri=None)`: remove one indexed document and its chunks.
- `knowledge_evaluate(config_path=None, suite=None)`: run the public benchmark harness or a configured subset and write a report.

Tool schemas must stay entity-agnostic. They accept query text, collection names, paths, filters, and numeric options, but they do not branch on specific real-world names.

Alternative considered: automatically index every workspace file. That creates privacy and cost risk. The first version requires explicit indexing.

### Make benchmark evaluation reproducible and provider-neutral

Add evaluation configuration under a repo path such as `backend/rag/eval/default_suites.yaml`. The code reads dataset entries from config; it does not hardcode dataset-specific behavior into retrieval logic.

The initial public suite should include adapters for:

- BEIR-compatible retrieval datasets for heterogeneous retrieval quality.
- MS MARCO Passage for passage-ranking and retrieval behavior.
- Natural Questions Open for open-domain single-hop QA.
- HotpotQA for multi-hop QA and supporting-evidence behavior.

Metrics:

- Retrieval: nDCG@10, Recall@5/10/20, MRR@10, MAP, and candidate coverage.
- Answering: exact match, token F1, citation support rate, grounded-answer rate, refusal accuracy where no answer is supported.
- RAG quality: context precision, context recall, faithfulness, and answer correctness when labels or judge models are configured.
- Operational: index throughput, query p50/p95 latency, average context tokens, model calls, and embedding cost estimate.

Quality gates:

- A lexical baseline run must be generated and stored for every configured public retrieval dataset.
- The default structure-first semantic chunking plus hybrid pipeline must meet or beat the lexical baseline on average nDCG@10 across the configured public retrieval suite.
- No individual public retrieval dataset may fall below 95% of the lexical baseline nDCG@10 unless the evaluation report records an explicit waiver.
- On labeled QA datasets, answer EM/F1 must not regress against the stored baseline, and citation support rate must meet the configured minimum.
- Every report must include environment, dataset versions, corpus sizes, model identifiers, retrieval settings, random seeds, and pass/fail status.

Alternative considered: only unit-test retrieval with synthetic documents. Synthetic tests are still necessary, but they do not prove the system works on public, independently maintained retrieval and QA tasks.

### Store evaluation artifacts separately from personal knowledge

Benchmark corpora, downloaded datasets, predictions, and reports should live under a configured evaluation cache path, separate from the user's personal knowledge store. Reports should be safe to commit only when they do not contain private documents or credentials.

Alternative considered: reuse the personal knowledge store for benchmark corpora. Separation avoids accidental mixing of public benchmark material and private user data.

## Risks / Trade-offs

- [Embedding provider sends private text externally] -> Make provider selection explicit in config, document the privacy implication, and allow local embedding providers.
- [Vector backend dependency is hard to install] -> Keep the first backend behind an interface and provide a small in-memory test backend for unit tests.
- [Poor chunking reduces answer quality] -> Add chunking tests for headings, tables, overlap, and stable ids; expose parser/chunker versions in manifests.
- [Hybrid retrieval increases latency] -> Make top-k, candidate limits, reranker use, and timeouts configurable; log p50/p95 latency in benchmark reports.
- [Reranker improves quality but adds cost] -> Treat reranking as optional and compare with and without reranking in evaluation.
- [Public benchmark downloads are large] -> Support named suites, sample limits for smoke tests, and full runs for release gates.
- [LLM-as-judge metrics vary by model] -> Keep deterministic retrieval and QA metrics primary; record judge model and prompt version for RAGAS-style metrics.
- [Source citations may look authoritative despite weak support] -> Include support-score thresholds and require refusal when no retrieved chunk passes the configured confidence floor.
- [Entity-specific shortcuts creep into prompts or code] -> Add tests or static checks that fail on entity-to-domain and entity-to-keyword routing patterns in touched RAG code and prompts.

## Migration Plan

1. Add configuration keys for the project-local `knowledge_base/` path, document/import directories, index directories, evaluation cache path, embedding provider, vector backend, sparse backend, reranker, semantic chunking settings, and default retrieval limits.
2. Add the `backend/rag/` interfaces and local test backends without wiring them into the agent by default.
3. Implement ingestion, manifest persistence, chunking, indexing, retrieval, deletion, and refresh behavior.
4. Implement project-folder sync that detects added, changed, deleted, and skipped files and reconciles dense/sparse indexes atomically.
5. Add frontend import, delete, manual sync, answer-mode selection, and citation rendering integration.
6. Add agent tools and prompt guidance behind a feature flag.
7. Add evaluation harness, dataset adapters, baseline generation, and report writing.
8. Run smoke evaluations on small public subsets, then run the configured release suite.
9. Enable the tools by default only after the public benchmark report passes quality gates.

Rollback:

- Disable the feature flag to remove RAG tools from the agent while keeping stored indexes untouched.
- Keep manifests versioned so a later build can read or migrate existing indexes.
- Evaluation artifacts can be deleted independently of personal knowledge indexes.

## Open Questions

- Which embedding provider should be the default for this deployment: local-only, configured OpenAI-compatible embeddings, or a project-hosted embedding service?
- Which vector backend is preferred for production packaging: local FAISS-style index, SQLite extension, pgvector, or an external vector database?
- What private-document file types must be in the first supported set beyond Markdown, text, PDF, DOCX, CSV, and HTML?
- Should imported files be physically copied into `knowledge_base/documents/`, referenced in place when under allowed roots, or selectable per import?
- What minimum citation support rate should be required for release on the labeled QA suite after the first baseline run establishes realistic values?
