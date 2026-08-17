## ADDED Requirements

### Requirement: Activity and terminal responses are rendered separately
The frontend MUST render tool activity, planning status, terminal assistant response, blocked state, and recovery state as distinct typed views. It MUST NOT infer a final response from non-terminal stream text.

#### Scenario: Run emits planning activity
- **WHEN** the backend emits non-terminal model or tool activity
- **THEN** the frontend displays it in the activity view and does not append it to the final chat message

#### Scenario: Run completes
- **WHEN** the backend emits a terminal completion
- **THEN** the frontend renders the terminal response as the assistant message and marks activity as completed

### Requirement: Interrupted runs show actionable recovery
The frontend MUST show a blocked or interrupted run with completed stages, safe recovery options, and the explicit reason. It MUST NOT present the interrupted state as a completed assistant answer.

#### Scenario: Client reconnects after interruption
- **WHEN** the user reloads a session with an interrupted latest run
- **THEN** the frontend restores the recovery state from persisted progress and offers continuation or new-session options

### Requirement: External writes have a safe preview
Before a replace, restore, or other non-append document mutation, the frontend MUST be able to display target reference, operation, preflight version, expected block summary, verification mode, and confirmation requirement.

#### Scenario: Low-confidence document plan
- **WHEN** a structured import requires preview or confirmation
- **THEN** the frontend presents the preview state and does not dispatch the mutation until confirmation is recorded
