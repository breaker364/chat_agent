## ADDED Requirements

### Requirement: Safety invariants have automated regression coverage
The test suite MUST cover duplicate remote calls with different tool IDs, unsafe append routing, version conflict, verification mismatch, oversized tool history, stream interruption, atomic session commit, and replacement read-back.

#### Scenario: Different IDs repeat equal mutation
- **WHEN** a test agent emits equal remote mutations with distinct tool call IDs
- **THEN** the test proves one remote dispatch and one reused result

#### Scenario: Large history is continued
- **WHEN** a session contains raw completed tool payloads beyond the legacy character threshold
- **THEN** the test proves bounded projection or a recoverable token-capacity result rather than permanent protected-history failure

### Requirement: Document fixtures exercise generic structure
Document import and replacement tests MUST use fixtures that vary headings, paragraphs, lists, tables, metadata, and ambiguous layouts without relying on entity-specific branches or literal cleanup tables.

#### Scenario: Equivalent structure has different content
- **WHEN** two fixtures have different subject matter but equivalent structural nodes
- **THEN** the renderer and replacement planner produce equivalent block-type treatment

### Requirement: History projection has one implementation owner
The session store MUST expose one authoritative history-projection path, and characterization tests MUST cover ordering, native tool pairing, deduplication, and recovery references.

#### Scenario: Session contains mixed legacy and native tool events
- **WHEN** history includes matched, unmatched, duplicate, and legacy tool events
- **THEN** the authoritative projection produces deterministic ordering and bounded prompt records
