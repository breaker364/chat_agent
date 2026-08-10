## Why

The available document edit command only replaces one known text block, while append always adds new blocks. A whole-document format repair therefore has no first-class safe operation and can be incorrectly implemented as title updates followed by append. Raw PDF text extraction also loses structural meaning before it reaches the document writer.

## What Changes

- Add an explicit document-level replacement operation for structured Markdown content.
- Define distinct append, localized block edit, and full replacement semantics, with mandatory preflight and read-back verification.
- Replace opaque skill-command dispatch with a typed mutation interface that declares operation, target, mutability, and idempotency inputs.
- Introduce a generic structured document-import pipeline that produces a block-oriented intermediate representation before remote writing.
- **BREAKING**: Repair and overwrite workflows must use document replacement; they can no longer fall back to append.

## Capabilities

### New Capabilities

- `document-replacement-workflow`: Safely replace an existing document's complete block content with version and hash verification.
- `typed-skill-mutation-interface`: Expose normalized operation metadata from CLI-backed skills to the agent execution layer.
- `structured-document-import`: Convert imported documents into a generic structural representation before rendering document blocks.

### Modified Capabilities

None.

## Impact

- Affects `skills/feishu-personal-cli/lark_tools/commands/doc_write.py`, `skills/feishu-personal-cli/lark_tools/cli.py`, skill execution integration, and PDF import helpers.
- Adds a CLI command and structured execution result fields.
- Depends on the P0 mutation guard and bounded-history protections for safe rollout.
