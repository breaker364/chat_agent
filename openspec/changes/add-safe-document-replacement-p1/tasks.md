## 1. Structured command contract

- [x] 1.1 Add command-parser tests for document read, append, block edit, replacement, create, and delete manifests.
- [x] 1.2 Define the typed skill command-manifest model and compatibility adapter for CLI-backed skills.
- [x] 1.3 Return manifests and structured verification fields from document command execution.
- [x] 1.4 Connect the P0 mutation guard to the typed manifest as its authoritative input.

## 2. Document replacement command

- [x] 2.1 Add failing tests for replacing an empty document, a paragraph document, and a mixed heading/list document.
- [x] 2.2 Implement replacement preflight that captures target version, writable child inventory, source hash, and planned block summary.
- [x] 2.3 Implement a complete version-checked root-content replacement mutation without an append fallback.
- [x] 2.4 Implement read-back verification of version, canonical content hash, and block-type summary.
- [x] 2.5 Add conflict, empty-source, and verification-mismatch tests proving no corrective append is sent.

## 3. Structural import pipeline

- [x] 3.1 Define generic intermediate node types and canonical serialization for title, heading, paragraph, list, quote, code, table, and metadata.
- [x] 3.2 Add fixtures covering multi-page text, headings, lists, tables, page metadata, and ambiguous reading order.
- [x] 3.3 Implement source extraction into the intermediate representation without content-specific cleanup maps.
- [x] 3.4 Render high-confidence structures into replacement-ready Markdown and block summaries.
- [x] 3.5 Require preview or explicit confirmation for low-confidence replacement plans.

## 4. Integration and rollout

- [x] 4.1 Add end-to-end tests that route repair requests to replace and explicit add-content requests to append.
- [x] 4.2 Add document-version-history and rollback-reference reporting to replacement results.
- [x] 4.3 Release `doc replace` in dry-run mode, validate representative documents, then enable it behind the P0 safety guard.
