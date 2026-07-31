## ADDED Requirements

### Requirement: Configurable personal knowledge store
The system SHALL provide a configurable local-first storage location for personal knowledge collections, document manifests, chunk metadata, sparse indexes, vector indexes, and retrieval configuration. The system SHALL keep provider-specific storage details behind backend interfaces.

#### Scenario: Feature is enabled with default paths
- **WHEN** the runtime starts with personal knowledge enabled and no custom knowledge path is configured
- **THEN** the system SHALL create or reuse a default project-local `knowledge_base/` directory under the project root

#### Scenario: Feature is disabled
- **WHEN** personal knowledge is disabled in runtime configuration
- **THEN** the system SHALL not register knowledge-base tools with the agent runtime

### Requirement: Project-local knowledge_base layout
The system SHALL use a project-local `knowledge_base/` directory by default. The directory SHALL contain user-added source files and generated retrieval artifacts in separate, inspectable locations so users can back up, delete, or move the project knowledge base as a unit.

#### Scenario: Knowledge base is initialized
- **WHEN** the personal knowledge feature initializes for a project with no existing knowledge store
- **THEN** the system SHALL create or reuse subdirectories for source documents, generated indexes, manifests, and sync reports under `knowledge_base/`

#### Scenario: Source and generated data coexist
- **WHEN** a document has been synced into the knowledge base
- **THEN** the original or imported source file SHALL remain available under the configured source document area and its vector/sparse index artifacts SHALL remain under the generated index area

#### Scenario: Knowledge base path is customized
- **WHEN** configuration overrides the default knowledge-base path
- **THEN** the same source, index, manifest, and report separation SHALL apply under the configured path

### Requirement: Explicit document ingestion
The system SHALL index only documents explicitly selected by the user or by a caller-provided tool argument. The ingestion pipeline SHALL reject unsupported paths, inaccessible files, files outside allowed roots, and unsupported file types with structured errors.

#### Scenario: User indexes supported files
- **WHEN** `knowledge_index_files` receives a collection name and supported file paths under allowed workspace roots
- **THEN** the system SHALL parse the files, create document manifests, chunk extracted content, index the chunks, and return document ids plus indexing status

#### Scenario: User indexes an unsupported file
- **WHEN** `knowledge_index_files` receives a path that cannot be parsed by any configured parser
- **THEN** the system SHALL return a structured skipped-file result with the path, reason, and no partial document manifest

### Requirement: Frontend and folder-based document import
The system SHALL support adding documents through either direct file placement under the project `knowledge_base/` source document area or frontend-driven import. Frontend import SHALL copy or register user-selected files into the configured knowledge-base source area before vectorization.

#### Scenario: User drags files into the folder
- **WHEN** a user manually adds supported files to the configured `knowledge_base/` source document area
- **THEN** the next sync SHALL detect those files as pending indexing without requiring the user to call a low-level indexing tool

#### Scenario: User imports files from the frontend
- **WHEN** the frontend imports supported user-selected files into a collection
- **THEN** the system SHALL place or register the files under the configured knowledge-base source area and mark them pending sync

#### Scenario: Frontend import rejects unsupported files
- **WHEN** the frontend import receives unsupported, inaccessible, or disallowed files
- **THEN** the system SHALL report bounded per-file errors and SHALL NOT write partial index artifacts for those files

### Requirement: Manual sync vectorization
The system SHALL vectorize and index newly added or changed documents only when the user triggers an explicit sync from the frontend or an equivalent runtime tool. Sync SHALL reconcile source files, manifests, chunks, sparse indexes, and vector indexes atomically.

#### Scenario: User syncs new files
- **WHEN** the user clicks sync after adding new supported documents
- **THEN** the system SHALL parse, chunk, embed, and index only new or changed documents and SHALL return counts for indexed, refreshed, skipped, unchanged, failed, and deleted documents

#### Scenario: User syncs with no changes
- **WHEN** the user clicks sync and source files, manifests, and index settings are unchanged
- **THEN** the system SHALL report no-op status without re-vectorizing unchanged documents

#### Scenario: Sync fails midway
- **WHEN** parsing, embedding, vector storage, or sparse index update fails during sync
- **THEN** the system SHALL keep the previous active index available and SHALL return a structured failure report for affected files

### Requirement: Stable chunk provenance
The system SHALL assign stable chunk ids and SHALL preserve provenance metadata linking every chunk to its document, collection, source reference, heading path, page or section when available, source offsets when available, parser version, chunker version, chunking strategy, boundary method, overlap metadata, and content hash.

#### Scenario: Same document content is re-indexed
- **WHEN** the same source document content is indexed again with the same parser and chunker versions
- **THEN** the system SHALL produce the same document content hash and stable chunk ids for unchanged chunks

#### Scenario: Retrieved chunk is returned
- **WHEN** a knowledge search result includes a chunk
- **THEN** the result SHALL include enough provenance metadata for the agent to cite the source document and chunk without exposing unrelated document contents

### Requirement: Structure-first semantic chunking
The system SHALL chunk indexed documents with a structure-first semantic chunking strategy by default. The chunker SHALL preserve headings, paragraphs, tables, lists, code blocks, pages, and other parser-provided structural boundaries where possible; it SHALL use semantic boundary detection for oversized or weakly structured text when embeddings are configured; and it SHALL use token-window splitting with overlap only as a fallback.

#### Scenario: Structured document is chunked
- **WHEN** a document contains headings, sections, paragraphs, tables, lists, or code blocks
- **THEN** the chunker SHALL prefer those structural boundaries and SHALL store the selected boundary method in chunk metadata

#### Scenario: Oversized text block is chunked
- **WHEN** a parsed block exceeds the configured maximum chunk token count
- **THEN** the chunker SHALL split first by paragraph or sentence boundaries, then by semantic boundaries when configured, and only then by token-window fallback with configured overlap

#### Scenario: Chunking configuration changes
- **WHEN** target chunk size, overlap, semantic boundary threshold, parser version, or chunker version changes for an indexed document
- **THEN** refresh SHALL treat the document as requiring re-chunking and SHALL update affected chunk ids and indexes atomically

### Requirement: Atomic refresh and deletion
The system SHALL refresh changed documents atomically and SHALL delete indexed documents by document id or by collection plus source reference. Refresh and deletion SHALL update manifests, sparse index entries, vector index entries, and chunk metadata consistently.

#### Scenario: Existing document changes
- **WHEN** `knowledge_index_files` is called with refresh enabled and the extracted content hash differs from the stored manifest
- **THEN** the system SHALL build replacement chunks in a staging state and swap them into the active collection only after all indexes are updated successfully

#### Scenario: User deletes an indexed document
- **WHEN** `knowledge_delete_document` is called with an existing document id
- **THEN** the system SHALL remove the document manifest, all chunk metadata, sparse entries, and vector entries for that document from future retrieval results

#### Scenario: User deletes a source file from the folder
- **WHEN** a previously indexed source file is removed from the configured `knowledge_base/` source document area and the user triggers sync
- **THEN** the system SHALL remove the corresponding document manifest, chunk metadata, sparse entries, and vector entries from future retrieval results

#### Scenario: User deletes a document from the frontend
- **WHEN** the frontend deletes a document from a collection and the user triggers sync
- **THEN** the system SHALL remove or tombstone the source reference as configured and SHALL delete corresponding vector/sparse/chunk data during reconciliation

### Requirement: Hybrid knowledge retrieval
The system SHALL support lexical retrieval, vector retrieval over semantic chunks, hybrid rank fusion, metadata filters, score reporting, candidate deduplication, and configurable reranking through a generic retrieval pipeline. Reciprocal Rank Fusion SHALL be the initial default hybrid fusion strategy unless configuration selects another benchmarked strategy.

#### Scenario: User searches a collection
- **WHEN** `knowledge_search` receives a query, collection selector, filters, and top-k value
- **THEN** the system SHALL return ranked chunks with snippets, citation ids, source metadata, score components when requested, and the retrieval settings used

#### Scenario: Query includes exact rare terms
- **WHEN** the query contains exact identifiers, filenames, dates, commands, or uncommon tokens present in indexed text
- **THEN** lexical retrieval candidates SHALL be eligible for the final fused result even when dense retrieval scores are lower

#### Scenario: Reranking is enabled
- **WHEN** reranking is enabled and a configured reranker is available
- **THEN** the retrieval pipeline SHALL rerank fused candidates before final context packing and SHALL include reranker metadata in the search result settings

### Requirement: Citation-bound answer grounding
The system SHALL instruct the agent to answer personal-knowledge questions using retrieved knowledge snippets only when the snippets support the answer. Factual claims derived from personal knowledge SHALL include source citations returned by the retrieval tool.

#### Scenario: Retrieved evidence supports answer
- **WHEN** the agent answers using facts from retrieved personal knowledge chunks
- **THEN** the answer SHALL cite the supporting chunk ids or source references for those facts

#### Scenario: Retrieved evidence is insufficient
- **WHEN** no retrieved chunk passes the configured relevance or support threshold for the user's question
- **THEN** the agent SHALL state that the personal knowledge base does not contain enough evidence and SHALL NOT fabricate an answer from unsupported context

### Requirement: Frontend knowledge answer mode with citations
The system SHALL provide a user-selectable frontend mode for answering with knowledge-base evidence. When this mode is selected, answers SHALL expose structured citations that the frontend can render alongside claims or in an answer source panel.

#### Scenario: User selects knowledge-base answer mode
- **WHEN** the user asks a question with knowledge-base answering enabled
- **THEN** the agent SHALL use knowledge retrieval before answering unless the request explicitly overrides that mode

#### Scenario: Frontend renders citations
- **WHEN** the agent answer uses retrieved personal knowledge chunks
- **THEN** the frontend SHALL display citations including source title or safe source reference, collection, chunk id, snippet, and page, heading, or section metadata when available

#### Scenario: Knowledge answer lacks support
- **WHEN** retrieval does not return enough supporting evidence for the requested answer
- **THEN** the frontend SHALL surface the insufficient-evidence state and SHALL NOT present uncited generated claims as knowledge-base facts

### Requirement: Separate personal knowledge from web evidence
The system SHALL keep personal knowledge retrieval results distinct from web search results in tool output, citation formatting, logs, and answer synthesis.

#### Scenario: Agent uses both evidence sources
- **WHEN** a user explicitly asks for an answer that combines indexed personal documents and web information
- **THEN** the agent SHALL distinguish personal-knowledge citations from web citations in the final answer

### Requirement: Entity-agnostic RAG behavior
The system SHALL NOT implement entity-specific routing, entity-specific domain selection, or entity-specific keyword expansion in personal knowledge RAG code or prompts. Query understanding SHALL be based on the user's text, collection metadata, retrieval scores, and generic authority or provenance signals.

#### Scenario: Query mentions a named entity
- **WHEN** a user query contains a company, school, person, place, brand, product, domain, or other named entity
- **THEN** the RAG pipeline SHALL process that text through the same generic retrieval, filtering, ranking, and citation mechanisms used for any other query

### Requirement: Knowledge management tools
The system SHALL expose structured runtime tools for indexing files, searching knowledge, listing collections, listing documents, deleting documents, and running evaluation. Tool schemas SHALL use typed arguments and SHALL return JSON-compatible structured results.

#### Scenario: Agent tools are loaded
- **WHEN** personal knowledge is enabled and the agent runtime builds its tool list
- **THEN** the knowledge management tools SHALL be present with schemas that accept collection names, paths, filters, query text, and numeric limits without entity-specific arguments

#### Scenario: Sync tool is loaded
- **WHEN** personal knowledge is enabled and the agent runtime builds its tool list
- **THEN** a knowledge sync tool SHALL be available to reconcile project folder changes, frontend imports, frontend deletions, manifests, and vector/sparse index artifacts

#### Scenario: Import tool is loaded
- **WHEN** personal knowledge is enabled and the frontend or agent requests document import
- **THEN** a knowledge import operation SHALL accept user-selected file references and collection metadata without entity-specific arguments

### Requirement: Privacy-aware logging
The system SHALL avoid logging full personal document contents by default. Operational logs and session transcripts SHALL store tool summaries, document ids, collection names, source references, and bounded snippets rather than entire indexed documents.

#### Scenario: Indexing completes
- **WHEN** a document is indexed successfully
- **THEN** runtime logs and tool output SHALL include document metadata and bounded status details without dumping the full extracted document text
