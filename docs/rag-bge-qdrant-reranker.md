# RAG Retrieval Stack

This project uses a local production retrieval profile with:

- BGE-M3 embeddings through the local Transformers runtime.
- A persistent Qdrant collection at `knowledge_base/qdrant`.
- Lexical retrieval for exact identifiers, filenames, dates, and numeric values.
- Reciprocal Rank Fusion (RRF) between lexical and dense candidates.
- BGE reranking through the local Transformers sequence-classification runtime.

## Runtime Configuration

The effective production configuration is in `runtime_config.json`:

```json
{
  "rag": {
    "retrieval_profile": "production",
    "embedding_provider": "huggingface_local",
    "embedding_model": "bge-m3",
    "embedding_dimension": 1024,
    "vector_backend": "qdrant",
    "qdrant_collection": "knowledge_chunks",
    "reranker_provider": "huggingface_local",
    "reranker_model": "bge-reranker-v2-m3",
    "reranker_enabled_by_default": true
  }
}
```

Aliases are normalized to `BAAI/bge-m3` and `BAAI/bge-reranker-v2-m3`.
Model files are resolved below:

```text
knowledge_base/models/embedding/BAAI__bge-m3/
knowledge_base/models/reranker/BAAI__bge-reranker-v2-m3/
```

Runtime loading is local-only. A missing or invalid model does not trigger a
network download and does not fall back to the deterministic ranker.

## Model Preparation

Use the registered `knowledge_download_models` tool, or the model asset helper,
to populate the configured cache. The download result validates that weight
files exist and writes `knowledge_base/models/manifest.json`.

The runtime requires `torch`, `transformers`, `huggingface_hub`, and
`qdrant-client`. `sentence-transformers` is not required: the embedding
adapter performs masked mean pooling and L2 normalization directly with
Transformers.

## First Sync and Migration

Documents remain readable in the existing JSON files:

```text
knowledge_base/index/chunks.json
knowledge_base/manifests/manifests.json
```

Each manifest now stores `retrieval_signature` and `indexed_at`. A missing or
changed signature causes the next `knowledge_sync` operation to re-embed the
document. The replacement vectors are prepared first, written to Qdrant with
deterministic point ids, and stale points are removed after the replacement
set is ready.

Run the first migration with:

```text
knowledge_sync({"dry_run": true})
knowledge_sync({})
```

The sync response includes indexed, refreshed, unchanged, failed, and deleted
counts, the active providers, candidate settings, and bounded latency.

## Search Flow

```text
document chunks
  -> BGE-M3 embedding
  -> Qdrant upsert

query
  -> BGE-M3 query embedding -> Qdrant dense candidates
  -> lexical exact-match candidates
  -> RRF fusion
  -> BGE reranker
  -> adjacent context expansion
  -> bounded citations
```

Search score metadata keeps `lexical_score`, `dense_score`, `fusion_score`,
and `reranker_score` separate. Results contain snippets and citations, never
raw vectors or full source documents.

Collection and metadata filters are applied to both candidate paths. The
Qdrant payload contains identifiers and source metadata only; chunk text stays
in the local chunk index used to build bounded citations.

## Failure Behavior

Production indexing and search return a structured `error` object when the
configured model, Qdrant collection, or vector dimension is unavailable. The
main error codes are:

- `backend_unavailable`
- `vector_dimension_mismatch`

The response settings still report the configured stack and mark inactive
components as inactive. This makes an unavailable production backend visible
to callers instead of presenting lexical results as BGE results.

## Deterministic Test Profile

Unit and compatibility tests may use the explicit deterministic profile:

```python
config = load_rag_config({
    "retrieval_profile": "deterministic",
    "vector_backend": "in_memory",
    "embedding_provider": "deterministic",
    "reranker": {"enabled": True, "provider": "local_overlap"},
})
```

Direct construction of `PersonalKnowledgeBase` without a config preserves this
profile for existing unit tests. It is labeled in the response settings and
is not selected after a production backend failure.

## TDD Verification

Run the focused stack tests first:

```powershell
C:/Users/hank.yu3/.conda/envs/env_311/python.exe -m pytest backend/tests/test_rag_retrieval_stack.py -q -p no:cacheprovider
```

Then run the RAG regression set:

```powershell
C:/Users/hank.yu3/.conda/envs/env_311/python.exe -m pytest backend/tests/test_rag_core.py backend/tests/test_rag_project_sync.py backend/tests/test_rag_model_stack.py backend/tests/test_rag_tools.py backend/tests/test_rag_evaluation.py -q -p no:cacheprovider
```

The real cached-model smoke test is intentionally separate because BGE-M3 and
the reranker require substantial local memory. If resources are insufficient,
record the unavailable condition rather than replacing it with a deterministic
score and claiming the production stack is active.
