## ADDED Requirements

### Requirement: Different sessions can run concurrently
The system SHALL allow foreground agent runs from different sessions to execute at the same time.

#### Scenario: Start a run after switching sessions
- **WHEN** session A has an active foreground run and the user switches to session B
- **THEN** the system SHALL allow the user to start a foreground run in session B without stopping session A

#### Scenario: Concurrent runs complete independently
- **WHEN** session A and session B are both running foreground tasks
- **THEN** each run SHALL continue receiving, processing, and finalizing its own stream independently

### Requirement: Same session foreground runs are serialized
The system SHALL allow at most one active foreground run per session.

#### Scenario: Duplicate send in same session
- **WHEN** a foreground run is already active for a session and another foreground request is submitted for the same session
- **THEN** the system SHALL reject the second request with a user-readable active-run error
- **AND** the system MUST NOT append the rejected request as a new user message

#### Scenario: Send after terminal state
- **WHEN** a session's active run reaches completed, failed, blocked, or stopped state
- **THEN** the system SHALL allow a new foreground run to be started for that session

### Requirement: Runtime context is isolated per run
The system SHALL bind current `session_id` and `run_id` to the active request/run context rather than process-global mutable state.

#### Scenario: Concurrent tool calls in different sessions
- **WHEN** two sessions execute tool calls concurrently
- **THEN** each tool call SHALL read the `session_id` and `run_id` for the run that invoked it
- **AND** tool audit events, tool result cache entries, task plan updates, script stages, artifacts, and subagent task records SHALL be written only to that run's session

#### Scenario: Run cleanup after failure
- **WHEN** a run exits because of completion, error, cancellation, or client disconnect
- **THEN** the system SHALL reset the run context and clear per-run dedupe state for that run

### Requirement: SSE events are attributable to the correct run
The system SHALL route stream events to the session/run that created the stream.

#### Scenario: Stream event arrives while another session is visible
- **WHEN** the frontend receives an SSE event for a run whose session is not currently active in the UI
- **THEN** the frontend SHALL update that run's session-scoped state
- **AND** the frontend MUST NOT append the event output to the currently visible session

#### Scenario: Structured event payload includes attribution
- **WHEN** the backend emits structured SSE payloads for progress, activity, debug, tool, error, or done events
- **THEN** the payload SHALL include enough attribution for the frontend to associate the event with the originating session/run

### Requirement: Frontend run state is session-scoped
The frontend SHALL maintain loading, streaming output, tool events, activity events, debug events, usage, error state, and abort controller ownership per session.

#### Scenario: Active session rendering
- **WHEN** the user views a session with an active run
- **THEN** the UI SHALL render that session's streaming text, tool events, activity timeline, debug events, and stop control from that session's run state

#### Scenario: Inactive session continues running
- **WHEN** the user switches away from a session with an active run
- **THEN** the run SHALL continue unless explicitly cancelled
- **AND** the UI SHALL preserve that session's run state for display when the user returns

### Requirement: Sending controls are scoped to the active session
The frontend SHALL disable send only when the currently active session has an active foreground run.

#### Scenario: Another session is running
- **WHEN** session A is running and the user is viewing idle session B
- **THEN** the send control in session B SHALL remain available

#### Scenario: Current session is running
- **WHEN** the user is viewing a session that already has an active foreground run
- **THEN** the send control for that session SHALL be disabled until the run reaches a terminal state

### Requirement: Cancellation is session-scoped
The system SHALL cancel only the active run for the session targeted by the user action.

#### Scenario: Stop current session
- **WHEN** session A and session B are both running and the user stops session A
- **THEN** session A's active stream SHALL be aborted or marked stopped
- **AND** session B's stream SHALL continue unaffected

#### Scenario: Stop without active run
- **WHEN** the user invokes stop for a session with no active run
- **THEN** the system SHALL leave all other session runs unchanged

### Requirement: Session history remains isolated during concurrent runs
The system SHALL persist user messages, assistant messages, tool transcripts, usage, task progress, and terminal status to the session that owns the run.

#### Scenario: Background completion after UI switch
- **WHEN** a run completes after the user has switched to another session
- **THEN** the completed assistant message SHALL be persisted to the originating session
- **AND** the currently visible session's message list MUST NOT be modified by that completion

#### Scenario: Reload session after concurrent completion
- **WHEN** the user reloads or reopens a session whose run completed while it was inactive
- **THEN** the session history SHALL include the correct user message, assistant response, tools, activities, usage, and terminal progress for that session

### Requirement: Session list exposes concurrent run status
The frontend SHALL present enough session-level status for users to identify sessions with active or recently completed runs.

#### Scenario: Sidebar shows running session
- **WHEN** a session has an active foreground run
- **THEN** the session list SHALL show that the session is running without requiring the user to open it

#### Scenario: Sidebar updates after terminal state
- **WHEN** a session run reaches completed, failed, blocked, or stopped state
- **THEN** the session list SHALL update that session's status after the next local stream update or session refresh
