## ADDED Requirements

### Requirement: Only terminal responses become assistant messages
The backend MUST distinguish activity/model-stream chunks from the terminal final response. Non-terminal model text MUST NOT be persisted as the assistant's final chat message.

#### Scenario: Client disconnects during tool planning
- **WHEN** the client disconnects before a terminal completion event
- **THEN** the session stores blocked or interrupted run state and does not store the accumulated planning narration as the assistant reply

#### Scenario: Run completes normally
- **WHEN** the agent emits a validated terminal completion
- **THEN** the session stores exactly the terminal response and its tool summary

### Requirement: Interrupted runs are resumable
An interrupted run MUST persist status, completed task outputs, tool ledger references, and a bounded recovery summary without claiming completion.

#### Scenario: User reloads an interrupted session
- **WHEN** the frontend loads a session whose latest run is interrupted
- **THEN** it displays the interrupted state and offers continuation from persisted progress rather than replaying raw stream text

#### Scenario: Capacity failure occurs before tool execution
- **WHEN** context admission fails before any tool call
- **THEN** the persisted run records zero executed tools and a recoverable context-capacity reason
