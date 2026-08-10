## ADDED Requirements

### Requirement: Remote mutations have explicit semantics
The execution layer MUST classify every standardized remote command as a read, create, append, replace, edit, delete, or unknown operation before dispatch. Unknown or unclassified mutating commands MUST be rejected without a network request.

#### Scenario: Replacement intent cannot dispatch append
- **WHEN** a run is handling a document repair, overwrite, reformat, or replacement operation
- **THEN** a parsed `append` command is rejected and the run receives a recoverable error identifying the required replacement workflow

#### Scenario: Explicit append remains available
- **WHEN** the user explicitly requests appending content to a target document
- **THEN** the executor MAY dispatch an `append` operation after normal authorization and idempotency checks

### Requirement: Remote mutations are idempotent within a run
The executor MUST derive an idempotency key from provider, resource, target, operation, and canonical arguments, excluding the model-generated tool call ID. A succeeded key MUST NOT cause a second remote mutation in the same run.

#### Scenario: Same mutation has a new tool call ID
- **WHEN** two tool calls have different IDs but equal canonical mutation identity
- **THEN** only the first call reaches the remote service and the second receives the recorded first result

#### Scenario: Mutation arguments differ
- **WHEN** two calls target the same resource but their canonical mutation payloads differ
- **THEN** they receive distinct keys and the executor evaluates each against the run mutation policy

### Requirement: Mutations are verified and bounded
Every successful remote mutation MUST record target, operation, before-version when available, after-version when available, result hash, and verification status. A run MUST stop or require explicit continuation when its mutation budget is exceeded.

#### Scenario: Successful mutation is retried
- **WHEN** the agent enters a retry pass after a mutation already returned success
- **THEN** the ledger result is reused and no second remote request is sent

#### Scenario: Write verification fails
- **WHEN** a remote write succeeds but read-back verification cannot prove the expected result
- **THEN** the run is marked blocked and no corrective append is attempted automatically
