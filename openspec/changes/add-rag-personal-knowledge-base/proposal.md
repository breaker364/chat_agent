## Why

The agent currently answers from session memory, tools, and live web search, but it has no durable, user-managed personal knowledge base. Adding a RAG-backed knowledge capability gives the agent a reusable private memory layer for local documents, notes, exports, and project artifacts while keeping answers grounded in retrievable evidence.

This is needed now because personal knowledge workflows require repeatable ingestion, source traceability, permission-aware retrieval, and objective quality gates before the capability is trusted in daily use.

## What Changes

- Add a personal knowledge ingestion pipeline that can parse supported local files, normalize them into structure-first semantic chunks, attach provenance metadata, and index them for retrieval.
- Add a project-local `knowledge_base/` workspace layout that stores both user-added source files and derived vector/sparse index artifacts in auditable subdirectories.
- Add frontend-driven knowledge-base management flows for manual document drag/import, explicit sync vectorization, document deletion, and post-sync reconciliation of stale vector data.
- Add a retrieval pipeline with lexical and vector retrieval, configurable metadata filters, hybrid rank fusion, optional reranking, and source snippets.
- Add an answer synthesis path that uses retrieved context only when relevant, cites source chunks, and refuses or asks for more context when evidence is insufficient.
- Add a user-facing "use knowledge base" answer mode where frontend responses render citations returned by retrieval, including document title/source reference and chunk-level evidence.
- Add knowledge-base management APIs/tools for listing collections, indexing files, refreshing/deleting documents, querying the corpus, and inspecting citations.
- Add reproducible offline evaluation against public datasets before rollout, covering retrieval quality, answer correctness, multi-hop evidence, faithfulness, latency, and regression thresholds.
- Add privacy and safety constraints so local personal documents are indexed in a configured workspace store and are not silently sent to third-party services except through explicitly configured embedding/model providers.

## Capabilities

### New Capabilities

- `personal-knowledge-rag`: Covers ingestion, storage, retrieval, grounded answer synthesis, provenance, collection management, and runtime integration for a personal knowledge base.
- `rag-benchmark-evaluation`: Covers reproducible evaluation on public datasets, metric reporting, quality gates, and regression checks for the RAG pipeline.

### Modified Capabilities

- None.

## Impact

- Affected code: `backend/tools.py`, `backend/agent.py`, `backend/config.py`, `backend/session_store.py`, `backend/prompts/system_prompt.md`, and new backend modules under a `backend/rag/` package.
- Affected frontend: document import controls, knowledge-base file list, manual sync action, delete action, answer-mode selector, and citation rendering for knowledge-base responses.
- Affected tests: new unit tests for chunking, indexing, retrieval, citation shaping, deletion/refresh behavior, and evaluation harness behavior; integration tests for agent tool use and answer grounding.
- New dependencies may be needed for text extraction, embeddings, vector indexing, sparse retrieval, reranking, and benchmark evaluation. The implementation should keep provider-specific choices behind interfaces and configuration.
- New persistent data: source documents, local knowledge indexes, document manifests, chunk metadata, and sync state under project-local `knowledge_base/`; evaluation reports and benchmark run artifacts remain under separate configured evaluation paths.
