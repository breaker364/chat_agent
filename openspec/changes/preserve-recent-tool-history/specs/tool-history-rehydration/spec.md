## ADDED Requirements

### Requirement: Persist a lossless correlated tool transcript
The system SHALL persist every tool-call and tool-result event belonging to a newly completed assistant turn in `messages[*].tools` in execution order. Each event SHALL include a stable `tool_call_id`, tool name, event type, and sequence position. A tool-call event SHALL retain its complete argument payload, and a tool-result event SHALL retain its complete result content without application-level truncation.

#### Scenario: A turn completes with one tool call
- **WHEN** the agent invokes a tool and receives its result before producing its final response
- **THEN** the persisted assistant message SHALL contain a correlated call event and result event with the same `tool_call_id`, complete arguments, complete result content, and their original execution order

#### Scenario: A turn invokes the same tool more than once
- **WHEN** the agent invokes one tool multiple times during a completed turn
- **THEN** each call and result pair SHALL have a distinct `tool_call_id` and SHALL be persisted without overwriting or merging another pair

### Requirement: Rehydrate recent tool transcript as native messages
Before invoking the model for a new request, the system SHALL restore tool activity from the three most recent completed user-to-assistant turns as native assistant tool-call messages and matching tool-result messages. The rehydrated sequence SHALL preserve original tool-call order, arguments, result content, and call-to-result relationship before the final assistant text for the turn.

#### Scenario: Recent turn is replayed for the next request
- **WHEN** a new request begins after a completed turn containing tool activity
- **THEN** the model input SHALL contain native tool-call and tool-result messages for that turn rather than only a textual summary of the activity

#### Scenario: Multiple tool batches exist in a turn
- **WHEN** a completed turn contains a call-result sequence followed by another call-result sequence
- **THEN** the model input SHALL replay both batches in their persisted execution order before the final assistant response message

### Requirement: Limit lossless tool rehydration to three turns
The system SHALL protect complete tool activity for exactly the three most recent completed conversation turns. It SHALL NOT rehydrate native tool messages from older turns, while continuing to restore their permitted text history under the existing history policy.

#### Scenario: A fourth older tool-bearing turn exists
- **WHEN** a session has tool activity in four completed turns
- **THEN** only the three newest completed turns SHALL contribute native tool-call and tool-result messages to the next model input

### Requirement: Reject protected context overflow without data loss
The system SHALL NOT truncate, summarize, or omit arguments or result content from the protected three-turn tool window. If the complete protected transcript cannot fit the configured application context budget, the system SHALL stop before model invocation and return an explicit context-capacity error.

#### Scenario: Protected tool results exceed the application budget
- **WHEN** the complete native tool transcript from the protected window plus required prompt content exceeds the configured context budget
- **THEN** the system SHALL return a context-capacity error and SHALL NOT invoke the model with a truncated protected transcript

### Requirement: Read legacy tool records deterministically
The system SHALL read existing assistant records that lack `tool_call_id` without rewriting them. It SHALL assign stable synthetic IDs and pair compatible call and result events in persisted sequence order using per-tool FIFO matching. It SHALL preserve all available legacy arguments and result content.

#### Scenario: Legacy record has repeated tool names
- **WHEN** a legacy assistant record contains multiple calls and results with the same tool name but no persisted IDs
- **THEN** the loader SHALL pair each result with the earliest unmatched preceding call of that name and use deterministic synthetic IDs for rehydration

#### Scenario: Legacy record has an unmatched result
- **WHEN** a legacy assistant record contains a result that has no compatible preceding call
- **THEN** the loader SHALL preserve the result in an explicitly marked legacy transcript representation and SHALL NOT silently discard it
