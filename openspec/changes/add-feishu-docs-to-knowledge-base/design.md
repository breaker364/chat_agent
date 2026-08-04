## Context

The knowledge base is implemented by `PersonalKnowledgeBase`. Local imports copy supported files into `knowledge_base/documents/<collection>`, `sync()` scans that directory, manifests identify documents by a stable hash of collection and `source_uri`, and the retrieval path uses the configured BGE-M3 embedding provider, Qdrant vector store, BM25/dense fusion, and BGE reranker.

The repository also contains a personal-session Feishu CLI skill. Its document commands accept a document URL or object token and can read Markdown, inspect metadata, and download a document. The CLI entry point changes its working directory to its default download directory, so a relative download path is not a safe integration contract. The knowledge-base feature must therefore own the final snapshot path and must not treat a CLI download directory as the document source of truth.

The change must preserve existing local-file behavior, keep credentials out of runtime configuration, avoid network access in deterministic tests, and return bounded metadata rather than a complete remote document or vector data.

## Goals / Non-Goals

**Goals:**

- Import one Feishu document from a URL or object token into an existing knowledge collection.
- Preserve a stable remote identity, canonical citation link, title, and available ownership/update metadata.
- Reuse the existing chunking, BGE-M3, Qdrant, hybrid retrieval, and BGE reranking pipeline.
- Use a real BM25 sparse branch with document-frequency and length normalization, then fuse it with Qdrant dense candidates.
- Make repeated imports idempotent and provide an explicit sync operation for imported remote sources.
- Keep a local Markdown snapshot so an indexed source can be inspected, re-indexed, and recovered without changing its remote identity.
- Distinguish retryable authentication/transport/rate-limit failures from confirmed permission loss or remote deletion.
- Make the source client injectable so unit and integration tests use deterministic fakes.

**Non-Goals:**

- Crawling a Feishu knowledge-space tree or importing all linked/embedded documents.
- Importing spreadsheets, bitable records, images, audio, or video in the first version.
- Implementing a second vector index or a Feishu-specific retrieval/reranking path.
- Automatically starting QR login from an import or background sync request.
- Introducing application credentials or replacing the existing personal-session authentication flow.
- Returning a full document body from an agent tool response.

## Decisions

### 1. Add BM25 as the sparse retrieval branch

Replace the current query-token overlap scorer with an in-memory BM25 index built from the already loaded candidate chunks. The index SHALL tokenize with the existing shared tokenizer, calculate per-term document frequency and inverse document frequency, and apply standard `k1` and `b` length normalization. Candidate depth remains controlled by `lexical_candidate_depth`; the result metadata exposes `bm25_score` and keeps `lexical_score` as a backward-compatible alias.

The BM25 index is rebuilt from filtered candidate chunks for each search instance/query. This keeps collection and metadata filters correct without introducing a second persistence format; persisted chunks remain the source of truth. The BGE-M3/Qdrant branch remains independent, and RRF combines the BM25 and dense ranked lists.

Alternative considered: Qdrant sparse vectors. They would add a second vector representation and migration path before the current local knowledge corpus needs it. A deterministic local BM25 implementation provides exact-match quality now and keeps the sparse provider injectable for later replacement.

### 2. Add a provider-neutral remote source boundary

Introduce a small source-provider contract rather than adding Feishu branches to local-file parsing:

```text
RemoteDocumentProvider.inspect(reference) -> RemoteDocumentMetadata
RemoteDocumentProvider.fetch(metadata) -> RemoteDocumentSnapshot
```

`RemoteDocumentMetadata` contains the provider name, opaque object token, canonical source URI, canonical citation URL when available, title, optional owner/update fields, and an optional remote revision. `RemoteDocumentSnapshot` contains normalized Markdown, a content hash, the metadata, and the content format. The provider does not return vectors and does not decide collection membership.

The first implementation is `FeishuDocumentProvider`. It uses URL path/token parsing and the existing local-session document reader behind the adapter. It calls metadata before content when possible, resolves wiki references to their underlying document token, and maps provider responses to generic error categories. The adapter may call the bundled reader in-process; if a process boundary is required, it must pass an absolute output path and validate the returned path. The top-level CLI download directory is never used as an implicit source path.

Alternative considered: call `knowledge_import_files` after downloading to a temporary file. This loses the remote identity in `source_uri`, couples synchronization to a local filename, and makes permission/deletion semantics ambiguous.

### 3. Use stable remote identity and a separate snapshot path

For a canonical object token `T` and collection `C`, store:

```text
source_uri = feishu://document/T
doc_id     = existing stable document-id function(C, source_uri)
```

The provider returns the display URL as metadata, for example `source_url`, instead of the service constructing a vendor host from a hard-coded entity mapping. The source URI is used for identity and deletion; the display URL is used for citations.

Persist the normalized content at a generated path such as:

```text
knowledge_base/documents/_remote/<collection>/<doc_id>.md
```

The exact path is generated from safe collection and document identifiers. It is stored in manifest metadata as `snapshot_path`; it is an implementation detail and is not used as `source_uri`. Local-file sync ignores this reserved remote snapshot area, while remote sync enumerates manifests whose metadata identifies the configured provider.

Alternative considered: use the snapshot path as `source_uri`. This would make citations point at an internal file and would make identity depend on local storage layout. Keeping the two values separate preserves both provenance and local recovery.

### 4. Reuse the existing indexing and retrieval pipeline

Add an external-document indexing method to `PersonalKnowledgeBase` that accepts `source_uri`, title, normalized content, source type, and metadata. It follows the same sequence as local indexing:

1. Compute the content hash and compare content, chunking signature, retrieval signature, and optional remote revision with the existing manifest.
2. Chunk the Markdown with the existing structure-first semantic chunker.
3. Prepare BGE-M3 embeddings and Qdrant records through the existing provider/vector-store instances.
4. Atomically refresh the document's Qdrant points and in-memory chunks.
5. Persist the snapshot and manifest only after indexing succeeds.

Search remains a single hybrid path. Imported chunks participate in BM25 sparse retrieval, BGE-M3 dense retrieval, RRF fusion, and BGE reranking exactly like local chunks. Search results expose the existing bounded excerpt plus `source_uri`, title, provider, and the canonical `source_url` when present.

The deterministic profile continues to use injected deterministic providers and an in-memory vector store. It must not contact Feishu implicitly. Production configuration continues to select the already configured BGE-M3/Qdrant/reranker stack.

### 5. Make import idempotent and sync explicit

Expose one-document operations:

```text
POST /knowledge/import/feishu
{
  "reference": "<document-url-or-token>",
  "collection": "<collection>",
  "refresh": false
}

POST /knowledge/sync/feishu
{
  "collection": "<optional-collection>",
  "doc_ids": ["<optional-doc-id>"],
  "dry_run": false
}
```

Add matching structured tools named `knowledge_import_feishu_document` and `knowledge_sync_feishu_documents`. A repeated import resolves the same canonical source URI. If the remote revision/content hash and retrieval signature are unchanged, it returns `unchanged` without re-embedding. `refresh=true` forces a content fetch and re-index when the content is available.

The import and sync result contains counts and per-source status, title, `doc_id`, `source_uri`, `source_url`, revision/hash indicators, chunk count, and bounded settings/error metadata. It never includes the full Markdown body, raw provider response, cookies, or raw vectors.

### 6. Define remote sync states and failure behavior

Use the existing manifest status and `latest_error` fields with the following semantics:

| Remote outcome | Manifest/index behavior | Retryable |
| --- | --- | --- |
| New or changed content | Replace snapshot, chunks, vectors, and metadata; status `indexed` | No |
| Same revision/hash | Keep all state; status `indexed` or `unchanged` result | No |
| Session expired, transport failure, or rate limit | Keep the last good snapshot and active vectors; record `sync_failed` and structured error | Yes |
| Confirmed permission denied | Remove active vectors/chunks, retain minimal manifest for recovery, status `access_denied` | After access is restored |
| Confirmed remote deletion/not found | Remove active vectors/chunks and snapshot, retain identity metadata, status `remote_deleted` | Only if the source reappears |
| Invalid content or normalization failure | Keep the last good index and record `sync_failed` | Yes after correction |

Search excludes manifests without an active indexed chunk set. A transient failure therefore preserves the last known good answer, while confirmed access loss and deletion cannot continue to return stale remote content. A later successful import of the same source URI reuses the same `doc_id`.

### 7. Reuse existing authentication without automatic login

The provider reads the existing session store through the configured Feishu session integration. Runtime configuration may select the provider, session-file location, timeout, maximum content size, and retry policy, but never contains a session cookie or other secret. An import request reports an actionable `auth_required` result when no valid session is available. QR login remains a separate existing flow.

Provider errors are normalized into stable categories such as `invalid_reference`, `auth_required`, `permission_denied`, `not_found`, `rate_limited`, `provider_unavailable`, and `content_invalid`. HTTP handlers map these to appropriate 4xx/5xx responses while preserving the structured error in the JSON body. The agent tools return the same structured payload instead of throwing an opaque exception.

### 8. Add configuration without changing the production retrieval contract

Add a small remote-source configuration section to the existing RAG/Feishu configuration loader. It includes:

- enabled provider name;
- session integration mode and session-file reference;
- request timeout and retry count/backoff;
- maximum accepted document characters/bytes;
- optional sync concurrency/batch limit.

The provider name is configuration-driven and passed into the source registry. No source-specific entity names or URL allowlists are added to agent logic. The source parser validates resource shape and token syntax; the provider owns provider-specific resolution.

### 9. Use TDD with deterministic fakes first

Tests are written before implementation in these layers:

- reference canonicalization and stable source URI/document ID;
- metadata/content normalization for paragraphs, headings, tables, links, and empty/oversized content;
- provider error mapping and session behavior;
- idempotent import and refresh with a fake provider;
- Qdrant/vector refresh and deletion through the existing injectable doubles;
- transient failure, access loss, and remote deletion state transitions;
- API and agent-tool schemas with bounded response assertions;
- end-to-end deterministic search proving imported chunks use the same retrieval stack and citation fields.
- BM25 formula/ranking tests proving exact terms, term rarity, length normalization, candidate depth, and RRF participation.

A live Feishu smoke test is optional and explicitly gated by an environment/configuration flag. The default test suite must not require network access, a personal session, model downloads, or external credentials.

## Risks / Trade-offs

- **Remote content can change between metadata and fetch** -> compute and persist a content hash from the fetched snapshot; treat the fetched content as the indexing version and retain the remote revision as advisory metadata.
- **A personal session can expire during a multi-step read** -> map auth errors consistently, keep the last good index, and never retry indefinitely or launch QR login in the request path.
- **Permission loss can expose stale cached text if handled as a normal failure** -> remove active vectors and chunks on confirmed permission denial; do not apply that destructive transition to ambiguous network/auth failures.
- **Existing local-file code assumes `source_uri` is a filesystem path** -> route remote documents through dedicated external-source methods and guard all path operations with an explicit local/remote source check.
- **Large or malformed documents can exhaust memory/model limits** -> enforce configurable fetch/content limits before chunking and return `content_invalid` without replacing a previously good index.
- **Provider-specific output may lose structure** -> normalize from block/Markdown output, keep heading paths and source title metadata, and add fixture tests for tables and links.
- **Qdrant refresh can leave mixed points on failure** -> prepare all embeddings before replacing points and commit the manifest only after the vector refresh succeeds, matching the existing retrieval-signature migration behavior.
- **Provider implementation may accidentally log sensitive data** -> log only source URI hash/doc ID, status, timing, and bounded error categories; never log cookies, full content, raw provider payloads, or vectors.

## Migration Plan

1. Add red tests and the provider-neutral data/error contracts without changing local-file behavior.
2. Implement the fake provider and external indexing path, then make deterministic tests green.
3. Implement the Feishu adapter over the existing session/document reader and add configuration parsing.
4. Add the agent tools and HTTP endpoints with structured error mapping and bounded responses.
5. Add remote sync, status transitions, snapshot cleanup, and search citation fields.
6. Run the existing RAG test suites plus the new focused tests. Run an optional authenticated smoke test only when explicitly enabled.
7. Deploy with the feature disabled or provider unavailable-safe by default, then enable it for users with an existing valid session. Existing local manifests and Qdrant points require no migration.

Rollback removes or disables the new endpoints/tools and deletes only remote-source manifests, snapshots, and vectors created by this change. Existing local documents and their retrieval indexes remain intact.

## Open Questions

- Should a later version use an official application credential provider in addition to the existing personal-session provider? The adapter boundary leaves room for it, but it is outside this change.
- Should the first UI expose per-document sync controls, or is the agent tool and API sufficient for the initial release?
- What provider-specific remote revision field is reliably available for all supported document URL forms? Content hashes remain the correctness fallback when it is absent.
