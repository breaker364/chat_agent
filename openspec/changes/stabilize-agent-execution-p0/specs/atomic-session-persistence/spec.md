## ADDED Requirements

### Requirement: Session writes are atomic and validated
The session store MUST serialize a complete candidate document, validate its JSON representation, and atomically replace the target file while holding the existing process lock.

#### Scenario: Concurrent reader observes a write
- **WHEN** a reader loads a session while another process commits an update
- **THEN** the reader sees either the previous complete JSON document or the new complete JSON document, never a partial document

#### Scenario: Serialization fails
- **WHEN** session serialization or validation fails
- **THEN** the existing session file remains unchanged and the caller receives a persistence error

### Requirement: Failed atomic commits remain recoverable
The store MUST retain a uniquely named temporary candidate on replacement failure or report its path in diagnostics, without deleting the last known-good session file.

#### Scenario: Target replacement is blocked
- **WHEN** an operating-system file lock prevents atomic replacement
- **THEN** the last known-good session remains readable and the failure includes enough information for controlled recovery
