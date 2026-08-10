## ADDED Requirements

### Requirement: Audit history and model history are separate
The system MUST preserve complete tool requests and results in audit storage while supplying the model with a bounded projection for completed turns. The projection MUST include operation, target reference, status, concise result summary, content hash when available, and an audit reference.

#### Scenario: Large completed tool result is replayed
- **WHEN** a new run loads a completed turn whose raw tool payload exceeds the prompt projection budget
- **THEN** the model receives the bounded projection and the raw payload remains available through the audit reference

#### Scenario: Projection preserves failure state
- **WHEN** a prior tool result contains an error
- **THEN** the projection identifies the failed operation and its sanitized error summary without replaying arbitrary raw payload text

### Requirement: Context admission uses a single token budget
The system MUST use model-context token estimates, including a reserved output budget, for both compaction triggering and model-call admission. Character counts MUST NOT independently reject a prompt that fits the configured token budget.

#### Scenario: Character count exceeds the legacy threshold
- **WHEN** prompt characters exceed the legacy character threshold but the projected token count fits the input budget
- **THEN** the model call proceeds or performs normal token-based compaction instead of returning a protected-context capacity error

#### Scenario: Projected tokens exceed the input budget
- **WHEN** the bounded projection still exceeds the input token budget
- **THEN** the system performs stronger projection or returns a recoverable context-capacity error before invoking tools or remote writes

### Requirement: Protected history has a bounded prompt footprint
Recent native tool protocol records MUST be retained losslessly only while they fit the configured protected prompt-token budget. Completed records older than the active protocol boundary MUST be eligible for deterministic projection.

#### Scenario: Recent history is oversized
- **WHEN** protected records exceed their prompt-token budget
- **THEN** the oldest completed records are projected while raw audit records remain unchanged

#### Scenario: Active tool pair is incomplete
- **WHEN** a tool call/result pair is still required to repair the current protocol message sequence
- **THEN** it is retained or represented by a validated protocol-safe placeholder, and the run does not silently construct malformed native history
