## ADDED Requirements

### Requirement: Safety and capacity outcomes are observable
The backend MUST emit structured events for remote mutation classification, deduplication, verification, context projection, context admission, interruption, and recovery disposition.

#### Scenario: Duplicate mutation is blocked
- **WHEN** a mutation is suppressed by an idempotency key
- **THEN** an event records the operation, target reference, deduplicated disposition, original-result reference, and run identifier

#### Scenario: Context projection is applied
- **WHEN** completed tool history is projected to meet the prompt budget
- **THEN** an event records raw size, projected token size, number of projected records, and the admission outcome

### Requirement: Observability does not expose raw payloads by default
Structured events and metrics MUST use hashes, references, counts, and sanitized summaries rather than complete tool arguments, document bodies, authentication material, or arbitrary tool output.

#### Scenario: Write payload is large
- **WHEN** a document replacement includes a large source body
- **THEN** the emitted event includes a source hash and size but not the complete source body

### Requirement: Execution metrics are queryable per run and target
The system MUST retain bounded metrics for mutation count, verification failures, projection pressure, context admission failures, interrupted runs, and recovery outcomes with run and target references.

#### Scenario: Repeated failures target one resource
- **WHEN** multiple runs fail verification for the same target reference
- **THEN** an operator can identify the count and latest reason without inspecting raw session payloads
