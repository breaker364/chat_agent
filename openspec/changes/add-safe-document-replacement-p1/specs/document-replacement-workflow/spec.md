## ADDED Requirements

### Requirement: Full document replacement is explicit
The CLI SHALL provide `lark doc replace <token-or-url> --md-file <path>` for replacing the complete writable block content of an existing document. The operation MUST be distinct from append and block edit.

#### Scenario: Replace a populated document
- **WHEN** a caller requests replacement with valid structured source content
- **THEN** the document's previous writable block content is replaced by the rendered source content without appending a second copy

#### Scenario: Repair workflow selects replacement
- **WHEN** an agent plans a document repair, overwrite, or full reformat
- **THEN** its mutation manifest identifies `replace` as the operation and does not select `append`

### Requirement: Replacement is preflighted and version checked
Before dispatching a replacement, the system MUST read the target version, writable child inventory, source hash, and planned block summary. The write MUST require the preflight version to remain current.

#### Scenario: Target changed after preflight
- **WHEN** the target version changes before the replacement is committed
- **THEN** the command returns a conflict and leaves the target without a fallback append mutation

#### Scenario: Source is empty
- **WHEN** the replacement source has no writable structural nodes
- **THEN** the command rejects the request unless the caller explicitly invokes a separately defined clear-document operation

### Requirement: Replacement is verified after writing
After a successful replacement response, the system MUST read back the target and verify document version advancement, canonical content hash, and block-type summary.

#### Scenario: Read-back matches the plan
- **WHEN** the returned document hash and block summary match the planned source
- **THEN** the command reports verified success with before-version and after-version metadata

#### Scenario: Read-back does not match the plan
- **WHEN** the replacement response succeeds but read-back verification differs
- **THEN** the command reports a blocked verification failure and does not issue an automatic corrective append
