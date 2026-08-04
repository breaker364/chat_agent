## ADDED Requirements

### Requirement: Accept and canonicalize a document reference

The system SHALL accept a supported Feishu document URL or opaque document token and SHALL resolve it to one canonical remote source identity before reading content. URL parsing SHALL be based on resource shape and provider configuration, not on a hard-coded entity-to-domain mapping.

#### Scenario: Import by document URL

- **WHEN** an authenticated user submits a supported document URL and collection
- **THEN** the system resolves the document token, creates a stable canonical `source_uri`, and proceeds with metadata/content retrieval

#### Scenario: Import by object token

- **WHEN** an authenticated user submits a valid opaque document token
- **THEN** the system resolves the token through the configured provider and produces the same canonical `source_uri` that the equivalent URL would produce

#### Scenario: Invalid reference

- **WHEN** the submitted reference has no supported token or URL shape
- **THEN** the system returns an `invalid_reference` error without creating a manifest, snapshot, or vector point

### Requirement: Retrieve and normalize document content

The system SHALL retrieve document metadata and content through an injectable remote document provider. The provider SHALL normalize supported block/Markdown/HTML/plain-text output into bounded UTF-8 Markdown while preserving meaningful headings, paragraphs, tables, links, and code blocks. The system SHALL reject content that exceeds the configured limit or cannot be normalized.

#### Scenario: Structured document is normalized

- **WHEN** the provider returns headings, paragraphs, a table, a link, and a code block
- **THEN** the snapshot preserves those structures in normalized Markdown and records the content format and content hash

#### Scenario: Empty or oversized document

- **WHEN** the provider returns empty content or content beyond the configured character/byte limit
- **THEN** the system returns `content_invalid` and does not replace an existing successful index

#### Scenario: Provider is unavailable

- **WHEN** the provider cannot contact the remote service or the session is expired
- **THEN** the system returns a structured retryable error such as `provider_unavailable`, `auth_required`, or `rate_limited` and preserves the last successful index

### Requirement: Persist stable remote identity and provenance

The system SHALL persist a remote manifest with a stable `source_uri`, stable `doc_id`, title, provider/source type, canonical citation URL when available, content hash, remote revision/update metadata when available, and the configured retrieval signature. The normalized content SHALL be stored as a local snapshot whose path is separate from `source_uri`.

#### Scenario: First import creates a manifest

- **WHEN** a document is successfully fetched and indexed into collection `C`
- **THEN** the manifest contains a stable identity derived from `C` and the canonical remote source, the snapshot path, source metadata, and the indexed chunk count

#### Scenario: Same document is imported into two collections

- **WHEN** the same remote source is imported into collections `C1` and `C2`
- **THEN** each collection has an isolated document identity and retrieval scope while both manifests retain the same canonical remote source identity

#### Scenario: Existing local manifests are loaded

- **WHEN** the knowledge base loads manifests created before this feature
- **THEN** local filesystem sources remain readable and searchable without requiring remote-source fields

### Requirement: Use BM25 as the sparse hybrid retrieval branch

The system SHALL use a real BM25 sparse scorer for candidate retrieval when the configured sparse backend is BM25-compatible. The scorer SHALL use the shared tokenizer, document frequency, inverse document frequency, term frequency, average document length, and configurable `k1`/`b` parameters. Its ranked candidates SHALL be fused with Qdrant dense candidates by RRF. The system SHALL expose `bm25_score` and retain `lexical_score` as a compatibility alias.

#### Scenario: Rare exact term is retrieved

- **WHEN** a query contains a rare identifier present in one indexed chunk
- **THEN** the BM25 branch returns that chunk with a positive score and includes it in the RRF candidate set

#### Scenario: Document length is normalized

- **WHEN** two chunks contain the same query terms but one is substantially longer
- **THEN** BM25 applies its configured length normalization before ranking and does not reduce the branch to raw token overlap

#### Scenario: BM25 and dense candidates are fused

- **WHEN** BM25 and Qdrant return overlapping or disjoint candidate sets
- **THEN** RRF combines both ranked lists and the result reports separate BM25, dense, fusion, and reranker score components

### Requirement: Index remote content with the configured RAG stack

The system SHALL pass normalized remote content through the existing chunking and indexing path. In the production profile, imported chunks SHALL use the configured BGE-M3 embedding provider, local persistent Qdrant vector backend, BM25/dense hybrid fusion path, and configured reranker at query time. In the deterministic test profile, the system SHALL use injected deterministic doubles and SHALL NOT contact the remote provider implicitly.

#### Scenario: Successful production-profile import

- **WHEN** a valid document is fetched under the production RAG configuration
- **THEN** the system creates chunks, prepares embeddings, refreshes the document's Qdrant points, persists the manifest, and reports the active retrieval settings without returning raw vectors

#### Scenario: Vector preparation fails

- **WHEN** embedding, Qdrant, or configured reranker preparation fails before replacement is committed
- **THEN** the system reports a structured backend error and retains the previous manifest, snapshot, chunks, and active vectors

#### Scenario: Imported chunk is searched

- **WHEN** a query matches content from an imported document
- **THEN** the result participates in the same hybrid retrieval and reranking path as local chunks and includes bounded excerpt, title, source URI, and canonical source URL metadata

### Requirement: Make repeated import idempotent

The system SHALL identify an imported document by collection and canonical remote `source_uri`. It SHALL avoid re-embedding when the content/revision and retrieval/chunking signatures are unchanged, and SHALL refresh the snapshot, chunks, vectors, and manifest when content or relevant signatures change.

#### Scenario: Unchanged repeated import

- **WHEN** the same source is imported again and its remote revision/content hash and retrieval signatures are unchanged
- **THEN** the result is `unchanged`, the original `doc_id` remains stable, and no replacement vectors are written

#### Scenario: Remote content changed

- **WHEN** the same source has a changed remote revision or fetched content hash
- **THEN** the system re-chunks and re-indexes the document under the same `doc_id`, removes stale chunk points, and reports `refreshed`

#### Scenario: Retrieval signature changed

- **WHEN** the configured embedding/vector/chunking signature differs from the manifest
- **THEN** the system re-indexes the remote snapshot before returning it as current

### Requirement: Synchronize imported remote sources

The system SHALL provide an explicit synchronization operation for all or selected previously imported sources, filtered by collection and optional document IDs. Sync SHALL support dry-run reporting and SHALL never treat remote source URIs as filesystem paths.

#### Scenario: Sync all sources in a collection

- **WHEN** a caller requests Feishu sync for collection `C` without document IDs
- **THEN** the system inspects all Feishu manifests in `C`, reports per-source outcomes, and updates only sources that changed or require re-indexing

#### Scenario: Dry-run sync

- **WHEN** a caller requests sync with `dry_run=true`
- **THEN** the system reports would-index, would-refresh, unchanged, and would-delete outcomes without changing snapshots, manifests, chunks, or vectors

#### Scenario: Remote source is deleted

- **WHEN** the provider confirms that a previously imported source no longer exists
- **THEN** the system removes its active chunks/vector points and snapshot, retains minimal identity/error metadata with status `remote_deleted`, and excludes it from search

### Requirement: Handle access and authentication failures safely

The system SHALL distinguish retryable authentication, transport, and rate-limit failures from confirmed permission denial. It SHALL preserve the last good searchable index for retryable failures, remove active searchable content for confirmed permission denial, and SHALL not initiate interactive login during import or sync.

#### Scenario: Session is expired

- **WHEN** the provider reports an expired or missing session
- **THEN** the operation returns `auth_required`, retains the last good index, and provides a separate login/status action as the remediation

#### Scenario: Access is denied

- **WHEN** the provider confirms that the current user cannot access the document
- **THEN** active chunks/vector points are removed, the manifest status becomes `access_denied`, and the document is excluded from search until re-import succeeds

#### Scenario: Rate limit is returned

- **WHEN** the provider returns a rate-limit response
- **THEN** the operation applies the configured bounded retry policy or returns `rate_limited`, preserves the last good index, and reports that the result is retryable

### Requirement: Expose bounded API and agent-tool results

The system SHALL expose import and sync through HTTP endpoints and structured agent tools. Results SHALL include collection, document/source identity, status/counts, title, chunk count, source URL metadata, retrieval settings, and bounded structured errors. Results MUST NOT include session cookies, raw provider payloads, full document content, or raw embedding/reranker vectors.

#### Scenario: Successful API import

- **WHEN** a caller posts a valid reference and collection to the Feishu import endpoint
- **THEN** the endpoint returns a JSON result containing stable identity, status, title, source link, chunk count, and retrieval settings

#### Scenario: Tool schema validation

- **WHEN** an agent invokes the import tool without a reference or with an invalid field type
- **THEN** schema validation fails before a provider request is made

#### Scenario: Structured failure response

- **WHEN** import or sync fails due to auth, permission, provider, content, or RAG backend conditions
- **THEN** the HTTP and tool surfaces return the same stable error category and bounded remediation metadata

### Requirement: Keep credentials and sensitive content out of configuration and logs

The system SHALL read Feishu credentials only from the existing session/auth integration. Runtime configuration SHALL contain provider behavior and limits but no cookie or token secret. Logs and tool responses SHALL use document IDs/source URI hashes and bounded status/error metadata rather than full document content or credentials.

#### Scenario: Runtime configuration is inspected

- **WHEN** the configured RAG and source settings are serialized for diagnostics
- **THEN** no session cookie, authorization header, or raw credential value is included

#### Scenario: Import is audited

- **WHEN** an import or sync operation is logged
- **THEN** the log contains operation, source identity/doc ID, status, duration, and error category only
