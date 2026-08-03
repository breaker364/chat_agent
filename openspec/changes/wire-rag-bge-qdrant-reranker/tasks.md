## 1. Configuration and Retrieval Contracts

- [x] 1.1 Extend RAG configuration with explicit embedding, vector, sparse, reranker, model-cache, Qdrant collection, batch-size, and retrieval-signature settings.
- [x] 1.2 Extend document manifests with backward-compatible retrieval signature and indexed-at metadata.
- [x] 1.3 Add provider protocols and structured backend-unavailable/dimension-mismatch error types.

## 2. TDD Red Tests

- [x] 2.1 Add failing embedding-provider contract tests for local BGE model loading, normalized vectors, batching, and configured dimension validation using mocked Transformers objects.
- [x] 2.2 Add failing Qdrant adapter tests for deterministic point ids, collection creation, upsert, filtered search, refresh, and deletion in a temporary local Qdrant path.
- [x] 2.3 Add failing reranker tests proving the configured reranker score changes final ordering and is reported separately from lexical score.
- [x] 2.4 Add failing integration tests proving `PersonalKnowledgeBase` passes the full RAG configuration into indexing and search, and reports the active BGE/Qdrant/reranker stack.
- [x] 2.5 Add failing migration tests proving missing or changed retrieval signatures force re-embedding and Qdrant population.

## 3. Provider and Index Implementation

- [x] 3.1 Implement the direct Transformers BGE-M3 embedding provider with local-only loading, masked mean pooling, normalization, batching, and dimension checks.
- [x] 3.2 Implement the direct Transformers BGE reranker provider with local-only loading, pair batching, scalar score normalization, and reusable model instances.
- [x] 3.3 Implement the local persistent Qdrant vector-store adapter with deterministic ids, payload filters, upsert, query, refresh, and delete operations.
- [x] 3.4 Add process-local provider/model reuse and bounded latency/error metadata without logging raw vectors or full documents.

## 4. Knowledge Base Integration

- [x] 4.1 Pass the full `RagConfig` and injectable providers/vector store into `PersonalKnowledgeBase` while preserving explicit deterministic test doubles.
- [x] 4.2 Embed chunks during index/import/sync and write Qdrant points only after all document embeddings are prepared successfully.
- [x] 4.3 Replace the pseudo-dense ranker with Qdrant dense retrieval while retaining lexical retrieval and configured RRF candidate depths.
- [x] 4.4 Apply collection and metadata filters to both lexical and Qdrant retrieval, then perform adjacent-context expansion after fusion/reranking.
- [x] 4.5 Run the real reranker on the fused candidate set when enabled and return reranker score metadata; return a structured error when it is unavailable.
- [x] 4.6 Reconcile Qdrant points on refresh/deletion and force re-indexing when the retrieval signature changes.

## 5. Runtime and Tool Behavior

- [x] 5.1 Wire `knowledge_*` tools to the configured production stack and return structured backend-unavailable results instead of silent fallback.
- [x] 5.2 Enable BGE-M3, local Qdrant, and BGE reranking in the runtime profile with explicit model-cache and collection paths.
- [x] 5.3 Include active provider/backend/model, candidate counts, score components, and bounded latency in search and sync responses.
- [x] 5.4 Update RAG implementation documentation with model preparation, first-sync migration, local Qdrant storage, and deterministic test profile instructions.

## 6. TDD Green and Verification

- [x] 6.1 Run the new focused retrieval-stack tests and fix implementation until all red tests pass.
- [x] 6.2 Run the existing RAG core, project-sync, model-stack, tools, and evaluation tests and fix compatibility regressions.
- [x] 6.3 Run an offline local smoke test against the cached BGE-M3, Qdrant, and reranker models when resource limits permit; record unavailable conditions explicitly otherwise.
- [x] 6.4 Run Python compilation, OpenSpec strict validation, and review that no raw vectors or full documents are emitted in tool results.
