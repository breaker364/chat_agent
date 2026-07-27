## ADDED Requirements

### Requirement: Active run accepts appended commands
The system SHALL allow a user to append prompt content to the active foreground run of the current session.

#### Scenario: Append while current session is running
- **WHEN** the current session has an active foreground run and the user submits text in append mode
- **THEN** the system SHALL accept the text as an append command for that active run
- **AND** the system SHALL return or emit an append identifier and queued status

#### Scenario: Append does not start a second same-session run
- **WHEN** the current session has an active foreground run and the user submits an append command
- **THEN** the system MUST NOT start another foreground run for that session
- **AND** the system MUST NOT append the command as a normal new user task message

### Requirement: Append commands are run-scoped
The system SHALL scope every append command to the `session_id` and `run_id` of the active run that accepted it.

#### Scenario: Append cannot affect another session
- **WHEN** session A and session B both have active runs and the user appends a command to session A
- **THEN** only session A's active run SHALL be eligible to receive that appended command
- **AND** session B's model context, activity, and persisted history MUST remain unchanged by that append

#### Scenario: Stale run id is rejected
- **WHEN** an append request names a `run_id` that is not the current active run for the target session
- **THEN** the system SHALL reject the append with a user-readable stale-run or no-active-run error
- **AND** the system MUST NOT queue the content for any other run

### Requirement: No active run rejects append commands
The system SHALL reject append requests when the target session has no active foreground run.

#### Scenario: Append while session is idle
- **WHEN** the user submits an append request for a session with no active foreground run
- **THEN** the system SHALL reject the request with a user-readable error
- **AND** the response SHALL make clear that the text can be sent as a normal new message instead

#### Scenario: Append after terminal state
- **WHEN** a run has completed, failed, stopped, been cancelled, or been cleaned up
- **THEN** the system SHALL reject new append requests for that run
- **AND** the system MUST NOT silently drop the user's text

### Requirement: Append commands are injected before the next LLM API call
The agent SHALL consume queued append commands immediately before the next LLM API call for the target run.

#### Scenario: Pending append before model call
- **WHEN** one or more append commands are queued for a run and the agent is about to call the LLM API for that run
- **THEN** the agent SHALL add the append command content to the model input before making that LLM API call
- **AND** the system SHALL mark those append commands as injected

#### Scenario: No further model call occurs
- **WHEN** an append command is queued but the run reaches a terminal state before another LLM API call
- **THEN** the system SHALL not claim that the append affected the model output
- **AND** the system SHALL expose that the append was not injected or remained unapplied

### Requirement: Injected append prompt is explicitly marked
The system SHALL include explicit appended-instruction wording in the prompt content sent to the LLM.

#### Scenario: Injected prompt contains append marker
- **WHEN** the agent injects an appended command into the model input
- **THEN** the injected text SHALL include wording such as `追加指令` or `用户追加要求`
- **AND** the marker SHALL appear near the appended user content, not only in debug metadata

#### Scenario: Append remains user-originated
- **WHEN** appended content is injected into the model input
- **THEN** the system SHALL represent it as user-originated supplemental input
- **AND** the system MUST NOT make the appended content indistinguishable from system or developer instructions

### Requirement: Multiple append commands preserve order
The system SHALL preserve FIFO ordering for multiple append commands accepted for the same run.

#### Scenario: Multiple queued appends
- **WHEN** append command A is accepted before append command B for the same run
- **THEN** the agent SHALL inject A before B when both are consumed for a model call
- **AND** lifecycle events SHALL retain enough sequence information to audit that order

#### Scenario: Appends across model turns
- **WHEN** one append is consumed before a model call and another append is accepted later before a subsequent model call
- **THEN** each append SHALL be injected into the next eligible model call after it is accepted
- **AND** previously injected append commands MUST NOT be injected again

### Requirement: Frontend supports append mode during active runs
The frontend SHALL keep the input usable for the active session while that session has a running foreground run and SHALL send that text as an append command.

#### Scenario: Running session input sends append
- **WHEN** the user is viewing a session with an active foreground run
- **THEN** the input control SHALL remain usable
- **AND** submitting text SHALL call the append-command path instead of starting a new `/chat/stream` run

#### Scenario: UI communicates append mode
- **WHEN** the active session is running
- **THEN** the send control, helper text, or status display SHALL indicate that submitted text will be appended to the current task

#### Scenario: Idle session input starts normal run
- **WHEN** the active session has no active foreground run
- **THEN** submitting text SHALL retain the existing normal-send behavior and start a new run

### Requirement: Append lifecycle is visible
The system SHALL expose accepted, injected, and rejected append states through user-visible or developer-visible events.

#### Scenario: Append accepted event
- **WHEN** an append command is accepted and queued
- **THEN** the system SHALL expose an event or status indicating that the append was received for the active run

#### Scenario: Append injected event
- **WHEN** an append command is consumed before an LLM API call
- **THEN** the system SHALL expose an event or status indicating that the append was injected into the active run

#### Scenario: Append rejected event
- **WHEN** an append command is rejected because of empty content, no active run, stale run id, or terminal run state
- **THEN** the system SHALL show a user-readable rejection reason
- **AND** the system MUST NOT expose sensitive internal prompt-construction details in user-facing text

### Requirement: Append command history is auditable
The system SHALL retain enough append metadata to audit what was appended and whether it was injected.

#### Scenario: Session refresh after append
- **WHEN** the user refreshes or reopens a session after an append command was accepted
- **THEN** the session or run activity view SHALL show that an append command was received
- **AND** the view SHALL distinguish whether the append was queued, injected, rejected, or not applied

#### Scenario: Debug trace includes append attribution
- **WHEN** developer/debug events are inspected for a run with append commands
- **THEN** the trace SHALL include `session_id`, `run_id`, and append status attribution for those commands
