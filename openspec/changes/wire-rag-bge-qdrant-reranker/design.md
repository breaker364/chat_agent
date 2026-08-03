## Context

The existing RAG package persists chunks and manifests, but `PersonalKnowledgeBase.search()` currently computes both lexical and so-called dense scores from token counts in memory. Runtime configuration already selects local BGE models and Qdrant, and the model cache contains the configured model artifacts, but no provider is connected to indexing or retrieval.

The implementation must remain local-first, preserve exact lexical matching for identifiers and filenames, and keep deterministic in-memory doubles for unit tests. The current Python environment provides `qdrant-client`, `transformers`, and `torch`; `sentence-transformers` is not assumed. Existing JSON manifests and chunk files must remain readable.

## Goals / Non-Goals

**Goals:**

- Generate normalized 1024-dimensional BGE-M3 dense vectors for chunks and queries through a local Transformers adapter.
- Persist vectors in a local Qdrant collection under the configured knowledge store.
- Keep lexical retrieval and RRF fusion as a complementary exact-match path.
- Rerank the fused candidate set with the configured BGE reranker model when enabled.
- Re-index documents when the embedding/vector/reranker signature changes, without silently treating old chunks as current.
- Expose provider, backend, model, latency, candidate, and failure metadata in bounded structured results.
- Make provider dependencies injectable so tests do not load large models or require network access.

**Non-Goals:**

- Qdrant Cloud or remote multi-tenant deployment.
- Sparse-vector storage in Qdrant; the existing lexical path remains the sparse/exact-match path for this change.
- OCR, multimodal embeddings, query expansion, or a full RAGAS judge pipeline.
- Automatic fallback from the configured production stack to the in-memory backend.

## Decisions

### Use direct Transformers adapters

Add provider-neutral protocols for `EmbeddingProvider` and `RerankerProvider`. The production embedding adapter loads the configured local BGE-M3 directory with `AutoTokenizer` and `AutoModel`, applies masked mean pooling, L2 normalization, and validates the configured dimension. The reranker adapter loads `AutoModelForSequenceClassification`, scores query/chunk pairs in batches, and returns normalized scalar scores.

The model cache path is resolved from the existing model plan. `local_files_only=True` is used for runtime retrieval so a query cannot unexpectedly trigger a network download. Model download remains an explicit tool operation.

Alternative considered: `sentence-transformers`. It is convenient, but it is not installed in the current runtime and would add a wrapper dependency around capabilities already available through Transformers.

### Use local persistent Qdrant

Add a `QdrantVectorStore` backed by `QdrantClient(path=<knowledge_store>/qdrant)`. One collection is used per knowledge store with payload fields for `chunk_id`, `doc_id`, `collection`, and source metadata. Chunk ids are converted to deterministic UUID point ids. Collection creation validates the configured vector dimension and fails clearly on mismatch.

Upsert is idempotent. Refresh upserts the replacement chunk set and deletes stale point ids for the document. Document deletion removes its point ids. Collection and generic metadata filters are translated to Qdrant payload filters before vector search.

Alternative considered: one Qdrant collection per user collection. A single local collection avoids collection lifecycle races and still provides isolation through payload filters.

### Keep lexical retrieval and RRF

Lexical retrieval continues to run over persisted chunks to preserve exact matching for numbers, dates, commands, filenames, and rare identifiers. Dense Qdrant candidates and lexical candidates are fused by RRF using configured candidate depths and `rrf_k`. Adjacent context expansion happens after fusion and reranking so it cannot displace the strongest primary candidates.

### Wire the complete configuration

`PersonalKnowledgeBase` receives the full `RagConfig`, not only a boolean reranker flag. The effective search response records the embedding provider/model, vector backend, sparse backend, fusion strategy, reranker provider/model, and whether each provider was active. The runtime profile enables the configured BGE/Qdrant/reranker stack. The deterministic backend is selected only by an explicit local/test profile or injected test doubles.

### Version index state

Extend manifests with an optional retrieval signature containing embedding model, dimension, vector backend, and reranker configuration. Missing or changed signatures force a rebuild. Existing manifests without the field remain readable and are treated as stale for the configured production stack.

### Failure and migration behavior

Model load, dimension, Qdrant, and reranker errors are returned as structured backend-unavailable results from knowledge tools. Sync keeps the previous manifest/chunk state when provider preparation fails before replacement is committed. A first sync after this change re-embeds existing documents and populates Qdrant; no manual data conversion is required.

## Risks / Trade-offs

- **[Model memory and latency]** BGE-M3 and the reranker are larger than the current token scorer -> load lazily, reuse process-local provider instances, batch inference, and expose latency metadata.
- **[Existing index compatibility]** Old chunks have no Qdrant vectors -> compare retrieval signatures and force a controlled re-index rather than returning incomplete results.
- **[Qdrant client version drift]** Qdrant APIs differ across releases -> isolate all client calls in one adapter and cover the adapter with local integration tests.
- **[Local model absence]** A clean machine may not have model files -> return an actionable unavailable state; never silently label a lexical fallback as BGE retrieval.
- **[Partial refresh]** A failure during a multi-document sync can leave mixed state -> upsert per document only after its embeddings are ready, delete stale points after replacement, and write manifests after successful vector operations.

## Migration Plan

1. Add provider and Qdrant adapter tests with fakes and a temporary local Qdrant store.
2. Add the production adapters and wire them into indexing/search/deletion.
3. Enable the configured BGE/Qdrant/reranker runtime profile and validate local model files before serving knowledge tools.
4. Run one sync to populate Qdrant and update retrieval signatures for existing documents.
5. Roll back by selecting the explicit deterministic test/local profile; existing `chunks.json` and manifests remain intact.

## Open Questions

- Whether CPU-only deployments need a configurable inference batch size and maximum sequence length beyond the initial safe defaults.
- Whether a later change should add Qdrant sparse vectors, or keep lexical retrieval as the long-term sparse backend.
