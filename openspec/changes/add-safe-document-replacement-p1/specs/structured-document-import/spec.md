## ADDED Requirements

### Requirement: Imported source is represented structurally
The import pipeline MUST convert supported source documents into a generic intermediate representation of document structure before rendering Markdown or remote document blocks.

#### Scenario: Source contains headings and lists
- **WHEN** the extractor identifies headings, paragraphs, and list items with adequate confidence
- **THEN** the intermediate representation preserves their node types and hierarchy for rendering

#### Scenario: Source contains extraction metadata
- **WHEN** the extractor observes page boundaries, source paths, timestamps, or diagnostics
- **THEN** those values are stored as metadata and are not emitted as ordinary document body paragraphs by default

### Requirement: Structural rendering is generic
The renderer MUST map intermediate node types to document blocks without content-specific replacement tables, entity-specific rules, or static source-domain mappings.

#### Scenario: Source terms vary
- **WHEN** two documents contain different names, terminology, or domains but equivalent structure
- **THEN** the renderer produces equivalent structural treatment without source-specific branches

### Requirement: Ambiguous imports require safe handling
The pipeline MUST attach extraction confidence and diagnostics to rendered plans. A low-confidence full replacement MUST require preview or explicit confirmation before remote mutation.

#### Scenario: Reading order is ambiguous
- **WHEN** the source parser cannot establish reliable structural order
- **THEN** the pipeline returns a preview-required plan and does not automatically replace an existing remote document

#### Scenario: High-confidence import is written
- **WHEN** structural confidence meets the configured replacement threshold
- **THEN** the renderer supplies a canonical source hash and block summary to the document replacement workflow
