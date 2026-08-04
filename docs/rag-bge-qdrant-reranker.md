# RAG Retrieval Stack

This project uses a local production retrieval profile with:

- BGE-M3 embeddings through the local Transformers runtime.
- A persistent Qdrant collection at `knowledge_base/qdrant`.
- BM25 sparse retrieval for exact identifiers, filenames, dates, and numeric values.
- Dense retrieval from Qdrant, fused with BM25 by Reciprocal Rank Fusion (RRF).
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
    "sparse_backend": "bm25",
    "bm25": {
      "k1": 1.2,
      "b": 0.75
    },
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
  -> BM25 sparse candidates
  -> BGE-M3 query embedding -> Qdrant dense candidates
  -> RRF fusion of sparse and dense ranks
  -> BGE reranker
  -> adjacent context expansion
  -> bounded citations
```

BM25 uses the shared tokenizer, document frequency, IDF, term frequency and
document-length normalization. `k1` controls term-frequency saturation and
`b` controls length normalization. Search score metadata keeps
`bm25_score`, the compatibility alias `lexical_score`, `dense_score`,
`fusion_score`, and `reranker_score` separate. Results contain bounded
snippets and citations, never raw vectors or full source documents.

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
to callers instead of presenting BM25 results as dense or BGE results.

## Feishu document knowledge import

The knowledge base accepts a document URL or opaque document token through the
same indexing and retrieval pipeline. The existing Feishu session/login flow
must be completed separately; an import or sync request never starts QR login.
Credentials are loaded from the existing local session store. Runtime
configuration contains only provider behavior and limits, not cookies or
authorization values.

Import one document:

```http
POST /knowledge/import/feishu
Content-Type: application/json

{
  "reference": "<document-url-or-token>",
  "collection": "project-notes",
  "refresh": false
}
```

Synchronize previously imported documents:

```http
POST /knowledge/sync/feishu
Content-Type: application/json

{
  "collection": "project-notes",
  "doc_ids": ["<optional-doc-id>"],
  "dry_run": false
}
```

Remote identity is stored as `source_uri` (`feishu://document/<object-token>`)
and `doc_id` is derived from the collection plus that identity. The canonical
display URL is stored as `source_url` for citations. Normalized Markdown is
stored separately under:

```text
knowledge_base/documents/_remote/<collection>/<doc-id>.md
```

The snapshot path is an internal recovery/indexing path and is never used as
the citation identity. Re-importing an unchanged revision returns
`unchanged`; changed content or retrieval signatures return `refreshed` while
preserving the same `doc_id`.

Sync states have the following semantics:

- `indexed` and `refreshed`: the snapshot and active chunks are current.
- `unchanged`: the remote revision/content and indexes are current.
- `sync_failed`: a retryable session, transport, rate-limit, content, or
  backend error occurred; the last good searchable index remains active.
- `access_denied`: confirmed permission loss removed active chunks and vectors.
- `remote_deleted`: confirmed deletion removed active chunks, vectors, and the
  snapshot while retaining minimal identity/error metadata.

HTTP errors use `400` for invalid input/content, `401` for missing or expired
authentication, `403` for permission denial, `404` for a missing source,
`429` for rate limiting, and `503` for provider or RAG backend failures.
Responses and tool results are bounded: they include status, identity,
metadata, counts, settings, and structured errors, but not document bodies,
provider payloads, cookies, or vectors.

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
