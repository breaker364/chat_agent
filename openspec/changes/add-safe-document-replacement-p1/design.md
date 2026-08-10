## Context

The current CLI exposes `append` for adding Markdown blocks and `edit --replace` for changing the text of one known block. Neither represents a full-document replacement. Existing repair scripts can therefore mistake a title update for content clearing and append an incomplete regenerated body to an unchanged document. The import path also converts PDF pages into physical text before Markdown parsing, which loses document structure.

P0 establishes the safety boundary that blocks unsafe append and duplicates. P1 supplies the correct domain operations that P0 can authorize.

## Goals / Non-Goals

**Goals:**

- Provide a first-class document replacement command with explicit preflight, version control, and verification.
- Preserve distinct append, local edit, and replacement meanings end to end.
- Expose structured skill operation metadata rather than relying on a free-form command string.
- Produce remote document blocks from a generic structural intermediate representation.

**Non-Goals:**

- Guarantee perfect reconstruction of every PDF visual layout.
- Automatically overwrite a document when structural extraction confidence is insufficient.
- Add entity-specific cleanup dictionaries or source-specific formatting rules.

## Decisions

### 1. Replacement is one logical, version-checked operation

`lark doc replace <target> --md-file <path>` will preflight the root document version, child block inventory, and canonical source-content hash. It will construct a complete replacement mutation against that version and verify the post-write document hash and block summary. It MUST NOT call append as a fallback. If the remote API cannot complete the version-checked mutation, it fails before a partial replacement is reported as successful.

Alternatives considered: deleting blocks one by one followed by append exposes a partially empty document and is not retry-safe; creating a separate replacement document is a useful recovery path but does not preserve the requested target identity.

### 2. Use a block-oriented intermediate representation

Import produces typed nodes such as document title, heading, paragraph, ordered list item, unordered list item, quote, code, table, and metadata. The renderer converts nodes to supported remote block operations. Source-page boundaries, local paths, and extraction diagnostics are metadata, not ordinary body text.

Alternatives considered: regex-cleaning extracted strings is dataset-specific, cannot recover lost hierarchy, and violates the project's generalization constraint when it depends on literal terms.

### 3. Typed command manifests are the public execution contract

The CLI parser will return a command manifest containing provider, resource kind, target, operation, mutating flag, input reference, and expected verification mode. The agent and P0 guard consume this manifest; they do not infer mutation semantics from the tool wrapper name.

### 4. Verification compares canonical structure, not rendered bytes

Before and after checks use canonical block summaries and normalized content hashes. The check records document version, block count by type, and source hash. It does not require byte-for-byte equality with a remote rich-text serialization.

## Risks / Trade-offs

- **Risk:** A remote document may change between preflight and replacement. → **Mitigation:** require version match and return a conflict with no append fallback.
- **Risk:** A PDF has ambiguous reading order or unsupported layout. → **Mitigation:** attach confidence and diagnostics to the intermediate representation and require preview or explicit confirmation for low-confidence replacement.
- **Risk:** A full replacement can remove user edits. → **Mitigation:** show target/version/content-diff summary before dispatch and retain a rollback reference through document version history.
- **Risk:** Typed manifests add integration work across the skill boundary. → **Mitigation:** retain the command-string interface as a compatibility adapter while making the manifest authoritative for mutations.

## Migration Plan

1. Add `doc replace` in dry-run mode with manifest and verification output.
2. Test it against empty, paragraph, list, heading, code, and table documents.
3. Enable replace for repair workflows behind the P0 mutation guard.
4. Migrate PDF import to the structural renderer and require preview for low-confidence documents.
5. Keep existing append and block-edit commands for their explicit, narrower use cases.

## Open Questions

- Which remote change-map operation provides the safest root-child replacement on all supported document versions?
- What confidence threshold should switch an automated import from replace eligibility to preview-only mode?
