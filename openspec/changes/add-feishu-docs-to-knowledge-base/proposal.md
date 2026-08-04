## Why

The knowledge base currently indexes local files, while the existing Feishu session and document tooling can read remote documents but cannot register them as knowledge sources. Users must download a document manually, which loses the canonical source link and makes repeat imports and updates difficult. The existing BGE-M3, Qdrant, and reranker pipeline should be reused so remote documents have the same retrieval and citation behavior as local documents.

## What Changes

- Add a provider-neutral remote document source contract and a Feishu-backed implementation that accepts a document URL or object token.
- Add a real BM25 sparse retrieval branch and fuse it with BGE-M3/Qdrant dense candidates through the existing RRF stage.
- Add a knowledge-base import operation and agent tool for one Feishu document, with idempotent behavior and bounded result metadata.
- Store a stable remote `source_uri` and `doc_id`, the canonical document link, title, owner/update metadata when available, and a local Markdown snapshot for reproducible indexing.
- Add explicit synchronization for previously imported Feishu sources, including unchanged, refreshed, deleted, permission-lost, authentication-failed, and rate-limited outcomes.
- Route imported content through the existing chunking, BGE-M3 embedding, Qdrant upsert, BM25/dense hybrid retrieval, and BGE reranking path.
- Preserve canonical Feishu links in search citations without returning full documents or raw vectors from tools.
- Add configuration for the source provider, session/auth behavior, fetch limits, and retry policy without storing credentials in runtime configuration.
- Add TDD coverage using deterministic fake source clients, including parsing, normalization, idempotence, sync failure semantics, API/tool contracts, and end-to-end retrieval.

## Capabilities

### New Capabilities

- `feishu-knowledge-import`: Import, index, cite, and synchronize Feishu documents as remote knowledge-base sources.

### Modified Capabilities

- None.

## Impact

- Affected backend areas: `backend/rag/models.py`, `backend/rag/service.py`, `backend/rag/retrieval.py`, the new BM25 module, `backend/rag/tools.py`, `backend/main.py`, new source-provider modules, and RAG tests.
- Affected runtime configuration: Feishu source-provider settings and existing knowledge-base storage paths; credentials remain in the existing session store.
- Affected persisted data: backward-compatible manifests gain remote-source metadata and snapshots; Qdrant points use the existing retrieval signature and collection.
- Affected user behavior: users can import a document by URL/token and later synchronize it without manually downloading files. Existing local-file import and search behavior remains unchanged.
