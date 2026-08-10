## ADDED Requirements

### Requirement: CLI-backed skills return operation manifests
Every standardized CLI-backed skill command MUST expose a normalized manifest containing provider, resource kind, target, operation, mutating flag, canonical argument representation, idempotency input, and verification mode.

#### Scenario: Parse document replacement
- **WHEN** the request contains a valid document replacement command
- **THEN** the manifest identifies a document resource, a replace operation, a mutating action, the resolved target, and read-back verification

#### Scenario: Parse document read
- **WHEN** the request contains a valid document read command
- **THEN** the manifest identifies a read operation with `mutating` set to false

### Requirement: Mutation policy consumes manifests
The mutation guard MUST make authorization, idempotency, retry, and audit decisions from the normalized manifest rather than from the wrapper tool name or a literal entity name.

#### Scenario: Wrapper name is generic
- **WHEN** a mutating command is invoked through a generic skill execution tool
- **THEN** the guard still applies remote-mutation policy using the command manifest

#### Scenario: Command cannot be normalized
- **WHEN** a command has mutating behavior but no valid manifest
- **THEN** the executor rejects it before dispatch and returns a structured validation error

### Requirement: User-visible command outcomes are structured
Successful and failed document commands MUST return the normalized manifest, verification status, and stable result reference in addition to human-readable output.

#### Scenario: Verified replacement succeeds
- **WHEN** a replacement completes and verifies
- **THEN** the result includes target, operation, before-version, after-version, content hash, and verified status
