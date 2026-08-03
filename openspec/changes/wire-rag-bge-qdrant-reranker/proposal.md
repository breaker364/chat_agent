## Why

The RAG runtime currently exposes configuration for BGE-M3, Qdrant, and a reranker, but the live search path still uses an in-memory bag-of-words approximation. This makes configured quality and persistence guarantees misleading, and it is now worth wiring the selected stack end to end before adding more retrieval features.

## What Changes

- Add a provider-backed embedding adapter that generates and persists BGE-M3 embeddings for indexed chunks and queries.
- Add a Qdrant vector-store adapter with a local persistent collection, deterministic point ids, collection isolation, refresh, and deletion support.
- Add a real configurable reranker adapter using the configured BGE reranker model for fused candidates.
- Keep lexical retrieval and RRF fusion for exact identifiers, filenames, dates, and numeric values.
- Make `runtime_config.json` the effective source of truth for embedding, vector, sparse, fusion, and reranker settings.
- Fail clearly when the configured model, Qdrant backend, or reranker is unavailable; do not silently claim that the requested stack is active.
- Preserve the deterministic in-memory backend as an explicit test/fallback profile only.
- Add TDD coverage for embedding calls, Qdrant upsert/search/delete/refresh, reranker ordering, configuration validation, and end-to-end citations.

## Capabilities

### New Capabilities

- `rag-retrieval-stack`: Real BGE-M3 embedding, Qdrant vector retrieval, hybrid rank fusion, and configured reranking for the personal knowledge base.

### Modified Capabilities

- None.

## Impact

- Affected code: `backend/rag/config.py`, `backend/rag/service.py`, `backend/rag/tools.py`, new provider/index modules under `backend/rag/`, and RAG tests.
- Affected runtime data: persistent Qdrant storage and embedding/reranker model cache under the configured knowledge-base paths.
- Affected dependencies: `qdrant-client`, `sentence-transformers` or an equivalent BGE-compatible embedding runtime, and a cross-encoder/reranker runtime.
- Affected behavior: RAG search will use the configured provider stack and will return an explicit unavailable/error state instead of silently using an unintended backend.
