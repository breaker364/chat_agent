## ADDED Requirements

### Requirement: Legacy blocked sessions have recovery manifests
The system MUST produce a bounded recovery manifest for a blocked session that identifies the failure reason, completed outputs, safe references, and next permitted actions.

#### Scenario: Protected history cannot fit a prompt budget
- **WHEN** a legacy session cannot be projected within the configured prompt budget
- **THEN** the system records a recovery manifest and offers a new-session continuation without replaying prior remote mutations

#### Scenario: User continues in a new session
- **WHEN** the user chooses new-session continuation from a recovery manifest
- **THEN** the new session receives only bounded progress references and does not inherit raw oversized tool payloads

### Requirement: Duplicate-write remediation is non-destructive
The system MUST detect candidate duplicate document writes using target, operation, version, content hash, and block summary, and it MUST present a recovery preview before any remote cleanup action.

#### Scenario: Candidate duplicate is detected
- **WHEN** repeated writes have equal target and content hash
- **THEN** the system records the candidate duplicate and exposes version-history or replacement references without deleting blocks

#### Scenario: Cleanup is not confirmed
- **WHEN** no explicit recovery action is confirmed
- **THEN** the system leaves the remote document unchanged

### Requirement: Session lifecycle is explicit
The system MUST support archive, recover, and fresh-continuation states for sessions and retain audit references according to configured retention rules.

#### Scenario: Session is archived
- **WHEN** a user archives a completed or unrecoverable session
- **THEN** its chat and audit references remain distinguishable from active run context
