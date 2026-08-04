## 0. BM25 Hybrid Retrieval

- [x] 0.1 Add red tests for BM25 term frequency, IDF rarity, length normalization, zero-match behavior, and candidate-depth limits.
- [x] 0.2 Add a red integration test proving BM25 and Qdrant dense candidates are both passed to RRF and score metadata exposes `bm25_score` separately from dense and reranker scores.
- [x] 0.3 Implement the injectable BM25 index with the shared tokenizer, configurable `k1`/`b`, and filtered-candidate rebuilds.
- [x] 0.4 Wire BM25 into production and deterministic profiles, preserve `lexical_score` as an alias, update runtime/docs, and include the sparse backend in retrieval-signature migration.

## 1. Contracts and Configuration

- [x] 1.1 Add red tests for canonical reference parsing, stable `source_uri`, collection-scoped `doc_id`, and rejection of malformed references.
- [x] 1.2 Add red tests for remote metadata/snapshot/error models and backward-compatible loading of existing local `DocumentManifest` records.
- [x] 1.3 Add red tests proving source-provider configuration contains limits and behavior settings but never serializes session cookies or authorization data.
- [x] 1.4 Implement provider-neutral remote source models, error categories, source registry, and configuration parsing with safe defaults.

## 2. Provider and Content Normalization

- [x] 2.1 Add deterministic fake-provider fixtures for headings, paragraphs, tables, links, code blocks, empty content, and oversized content.
- [x] 2.2 Add red tests proving provider output is normalized to bounded Markdown with a stable content hash and preserved document metadata.
- [x] 2.3 Add red tests for missing session, expired session, permission denial, not-found, rate-limit, transport, and malformed-provider responses.
- [x] 2.4 Implement the Feishu provider adapter over the existing session/document reader, including URL/token resolution, metadata lookup, content fetch, normalization, and structured error mapping.
- [x] 2.5 Ensure the adapter owns its output path or uses in-process content; add a regression test proving relative CLI download-directory changes cannot alter the knowledge snapshot path.

## 3. Remote Manifest, Snapshot, and Indexing

- [x] 3.1 Add red tests for creating a remote manifest with `source_uri`, canonical source URL, snapshot metadata, remote revision, content hash, retrieval signature, and chunk count.
- [x] 3.2 Add red tests proving remote source URIs never enter local filesystem path validation or local-file deletion scans.
- [x] 3.3 Add red tests for first import, unchanged repeat import, forced refresh, changed content, changed retrieval signature, and stable `doc_id` behavior.
- [x] 3.4 Add red tests proving embedding/Qdrant preparation failure leaves the previous manifest, snapshot, chunks, and active vector points intact.
- [x] 3.5 Implement atomic snapshot persistence and external-document indexing in `PersonalKnowledgeBase` using the existing chunker, BGE-M3 embedding provider, Qdrant vector store, and retrieval signature checks.
- [x] 3.6 Reconcile stale Qdrant points and in-memory chunks during remote refresh while preserving existing local-file import/index behavior.

## 4. Remote Synchronization and Search Semantics

- [x] 4.1 Add red tests for sync selection by collection and document IDs, dry-run reporting, unchanged sources, and changed sources.
- [x] 4.2 Add red tests for retryable failures preserving the last good index and confirmed access loss/deletion removing active searchable content.
- [x] 4.3 Implement remote-source sync, bounded retry/backoff, dry-run behavior, and manifest status/latest-error updates.
- [x] 4.4 Update search candidate filtering so only active indexed remote documents are searchable, while transient sync failures retain their last successful content.
- [x] 4.5 Add red and green tests proving imported chunks use the same BGE-M3/Qdrant/hybrid/reranker path and return bounded excerpt, title, source URI, and canonical source URL fields.

## 5. API and Agent Tools

- [x] 5.1 Add red tests for `POST /knowledge/import/feishu` and `POST /knowledge/sync/feishu` request validation, status codes, dry-run behavior, and structured error bodies.
- [x] 5.2 Add red tests for `knowledge_import_feishu_document` and `knowledge_sync_feishu_documents` schemas, bounded outputs, and no provider call on invalid input.
- [x] 5.3 Implement the two HTTP endpoints and two structured tools with shared service methods and consistent error mapping.
- [x] 5.4 Add authentication/status guidance without starting interactive QR login from the request path; verify credentials are absent from responses and logs.
- [x] 5.5 Run local-file API/tool regression tests to prove existing upload, import, sync, delete, and search behavior is unchanged.

## 6. Documentation, Verification, and Rollout

- [x] 6.1 Run focused provider, manifest, indexing, sync, API, and citation tests after each implementation group and make all TDD red tests green.
- [x] 6.2 Run the existing RAG core, project-sync, model-stack, tools, evaluation, and application endpoint test suites.
- [x] 6.3 Run Python compilation and an offline deterministic end-to-end test without network access, model downloads, or a personal session.
- [x] 6.4 Add an explicitly gated authenticated Feishu smoke test for import, repeat-import unchanged, remote update refresh, and access/error reporting.
- [x] 6.5 Update RAG and API documentation with accepted references, authentication prerequisites, snapshot/provenance behavior, sync states, troubleshooting, and the no-secret configuration rule.
- [x] 6.6 Run OpenSpec strict validation and review the final diff for raw document, cookie, token-secret, and vector leakage.
