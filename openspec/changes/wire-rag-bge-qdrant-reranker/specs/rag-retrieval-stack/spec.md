## ADDED Requirements

### Requirement: BGE-M3 embedding provider

The system SHALL use the configured local BGE-M3 model to encode every indexed chunk and every dense retrieval query when the production retrieval profile is active.

#### Scenario: Chunk embedding during indexing

- **WHEN** a supported document is indexed with `embedding_provider=huggingface_local` and a valid BGE-M3 model cache
- **THEN** the system SHALL encode its chunks with BGE-M3, normalize the vectors, validate the configured dimension, and persist the vectors through the configured vector backend

#### Scenario: Missing embedding model

- **WHEN** the configured BGE-M3 model is unavailable or cannot be loaded locally
- **THEN** indexing and dense search SHALL return a structured backend-unavailable error and SHALL NOT claim that BGE retrieval was used

### Requirement: Persistent Qdrant vector index

The system SHALL persist dense chunk vectors in a local Qdrant collection under the configured knowledge store and SHALL use Qdrant for dense candidate retrieval when `vector_backend=qdrant`.

#### Scenario: Vector upsert and search

- **WHEN** a document is indexed successfully
- **THEN** the system SHALL upsert deterministic point ids and source metadata into Qdrant and SHALL use the query vector to retrieve dense candidates from Qdrant

#### Scenario: Refresh and deletion

- **WHEN** a document is refreshed or deleted
- **THEN** the system SHALL replace or remove all corresponding Qdrant points and SHALL not return stale chunks from dense retrieval

#### Scenario: Vector dimension mismatch

- **WHEN** an existing Qdrant collection dimension differs from the configured embedding dimension
- **THEN** the system SHALL fail with an actionable dimension-mismatch error and SHALL not write incompatible vectors

### Requirement: Hybrid lexical and dense retrieval

The system SHALL combine Qdrant dense candidates with lexical candidates using the configured candidate depths and RRF fusion, while preserving exact lexical matches for rare tokens and numeric values.

#### Scenario: Exact token and semantic candidates

- **WHEN** lexical retrieval finds an exact identifier and BGE-M3 finds a semantically related chunk
- **THEN** both candidates SHALL be eligible for RRF fusion and the result SHALL report lexical and dense score components

#### Scenario: Collection and metadata filters

- **WHEN** a search includes a collection or supported metadata filter
- **THEN** both lexical and Qdrant candidate retrieval SHALL restrict results to matching chunks

### Requirement: Configured BGE reranking

The system SHALL use the configured BGE reranker model to score and reorder the fused candidate set when reranking is enabled.

#### Scenario: Reranker changes ordering

- **WHEN** the configured reranker assigns a higher relevance score to a lower-ranked fused candidate
- **THEN** the final result order SHALL reflect the reranker score and SHALL include reranker metadata

#### Scenario: Reranker unavailable

- **WHEN** reranking is enabled but the configured model cannot be loaded or scored
- **THEN** the search SHALL return a structured backend-unavailable result rather than silently using lexical score as a reranker substitute

### Requirement: Retrieval configuration truthfulness

The system SHALL report the actual embedding provider, vector backend, fusion strategy, and reranker provider used by each search and SHALL not silently fall back from the configured production stack.

#### Scenario: Production stack is active

- **WHEN** a search completes with BGE-M3, Qdrant, and the configured reranker
- **THEN** the response settings SHALL identify those active components and their model ids

#### Scenario: Explicit deterministic test profile

- **WHEN** a caller explicitly selects the deterministic local/test profile or injects test providers
- **THEN** the system MAY use the in-memory backend and SHALL label the response as deterministic local retrieval

### Requirement: Retrieval signature migration

The system SHALL persist a retrieval signature in document manifests and SHALL re-index documents when the embedding model, vector backend, dimension, or relevant retrieval signature changes.

#### Scenario: Existing manifest lacks production vectors

- **WHEN** a document has an existing manifest without the current retrieval signature
- **THEN** the next sync SHALL mark it stale and populate the configured Qdrant index before reporting it current

#### Scenario: Signature is unchanged

- **WHEN** content and retrieval signature are unchanged and all corresponding Qdrant points exist
- **THEN** sync SHALL report the document unchanged without recomputing embeddings

### Requirement: Bounded operational metadata

The system SHALL expose provider status, candidate counts, model identifiers, and bounded embedding/reranker latency without logging full personal document content or raw vectors.

#### Scenario: Search completes

- **WHEN** a knowledge search returns results
- **THEN** the response SHALL include bounded operational metadata and source citations without including raw vectors or full source documents
