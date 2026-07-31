# Personal Knowledge RAG Implementation Notes

## Current Local Backend

- Text extraction supports Markdown, plain text, CSV, and HTML with structured skipped-file results for unsupported files.
- Chunking uses a structure-first semantic strategy: headings, paragraphs, tables, lists, code blocks, and pages are preserved before sentence/semantic fallback and token-window fallback.
- Sparse retrieval uses an in-process lexical scorer suitable for deterministic tests.
- Dense retrieval uses a local hashing/vector scorer suitable for deterministic tests and offline smoke runs.
- Hybrid retrieval uses Reciprocal Rank Fusion as the default fusion strategy.
- Search expands adjacent chunks after rank fusion so split paragraphs, pages, and stage tables can return nearby evidence without increasing document-wide snippets.
- Reranking is configurable and currently implemented with a local overlap reranker for deterministic smoke verification.

## Selected Local Model Stack

- Runtime configuration now selects `qdrant` as the vector backend and `qdrant_sparse_or_bm25` as the sparse backend.
- Embeddings use the Hugging Face local model alias `bge-m3`, normalized at runtime to `BAAI/bge-m3`, with 1024 output dimensions.
- Hybrid retrieval uses Reciprocal Rank Fusion (`rrf`) and returns 8 final chunks by default.
- Reranking uses the Hugging Face local model alias `bge-reranker-v2-m3`, normalized at runtime to `BAAI/bge-reranker-v2-m3`.
- Reranking is configured but disabled by default; when enabled it reranks the top 50 fused candidates before final packing.
- Model assets are stored under `knowledge_base/models/`, with separate embedding and reranker cache directories plus a download manifest.
- The model download command pulls only runtime-relevant Hugging Face assets and validates that snapshots contain complete model weight files before marking them as successful; stale non-model image or asset leftovers do not block model readiness.

## Production Dependency Options

- Text extraction: `pypdf`, `python-docx`, `beautifulsoup4`, and table-aware CSV/HTML parsers.
- Sparse retrieval: BM25-compatible libraries such as `rank-bm25` or an indexed SQLite/full-text backend.
- Embeddings: local embedding models or an explicitly configured OpenAI-compatible embedding provider.
- Vector indexing: FAISS for local-first deployments, or pgvector, Qdrant, Milvus, or another configured vector backend for larger deployments.
- Reranking: local cross-encoder, configured hosted reranker, or an LLM reranker behind the `Reranker` interface.
- Evaluation: BEIR-compatible loaders, MS MARCO Passage, Natural Questions Open, HotpotQA, and optional RAGAS-compatible judge metrics.

## Evaluation Notes

- The smoke evaluator is deterministic and does not download large corpora.
- Public benchmark datasets are represented through a data-driven registry and can be marked blocked when manual download or license acceptance is required.
- Release gating should use full public benchmark runs before enabling the personal knowledge tools by default.
- Evaluation artifacts are written separately from personal knowledge indexes.

## Reproduction Notes

The git commit intentionally excludes local runtime artifacts under `knowledge_base/`:

- `knowledge_base/models/`: Hugging Face model weights and cache files for `BAAI/bge-m3` and `BAAI/bge-reranker-v2-m3`.
- `knowledge_base/documents/`: user-imported source documents, including private PDFs used for local smoke tests.
- `knowledge_base/index/`, `knowledge_base/manifests/`, and `knowledge_base/reports/`: generated chunk, index, manifest, and sync report state.

To reproduce the local setup:

1. Keep the `runtime_config.json` RAG section enabled with `knowledge_store_path` set to `knowledge_base`.
2. Ensure `pypdf` is installed for PDF extraction.
3. Download local Hugging Face assets through the configured `knowledge_download_models` tool or an equivalent Hugging Face download command for `BAAI/bge-m3` and `BAAI/bge-reranker-v2-m3`.
4. Add documents under `knowledge_base/documents/<collection>/` or import them from the frontend.
5. Trigger the frontend `Sync` action or call `POST /knowledge/sync` to parse, chunk, and index documents.
6. Use the frontend knowledge mode or `knowledge_search` to retrieve chunks with citations.
