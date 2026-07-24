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

### Requirement: Deduplicate protected tool-history projection
Before invoking the model for a new request, the system SHALL deduplicate identical duplicate tool-call/result pairs inside the protected three-turn native rehydration window. Deduplication SHALL apply only to the history projection returned by `get_history()` and SHALL NOT mutate the canonical `messages[*].tools` transcript. Duplicate identity SHALL be based on stable normalized tool name, arguments, and result content; the first occurrence SHALL be retained.

#### Scenario: Duplicate emitted tool pair exists in a protected turn
- **WHEN** a persisted assistant turn contains two tool-call/result pairs with the same tool name, equivalent arguments, and equivalent result content
- **THEN** `get_history()` SHALL return one native replay pair for that duplicate identity and SHALL preserve the original stored events unchanged

#### Scenario: Repeated calls have different results
- **WHEN** a persisted assistant turn contains two calls with the same tool name and equivalent arguments but different result content
- **THEN** `get_history()` SHALL retain both pairs in the native replay projection

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

### Requirement: Continue until agent decides no further tool use is needed
The system SHALL NOT emit a normal completion solely because the graph ended when the agent has not produced a final answer or has not had an explicit final opportunity to decide whether more `tool_use` is required. It SHALL continue the same agent run with a bounded continuation instruction that asks the agent to either call any remaining tools or finalize if no further tools are needed.

#### Scenario: Graph ends before a final answer
- **WHEN** a model pass ends without a usable final assistant answer and without a terminal capacity or tool-budget failure
- **THEN** the system SHALL append a continuation instruction and invoke the agent again so it can either perform more `tool_use` or provide the final answer

#### Scenario: Agent provides a usable final answer
- **WHEN** the agent produces a usable final assistant answer and no bounded repair condition remains
- **THEN** the system SHALL emit normal completion without adding another continuation pass

### Requirement: Read legacy tool records deterministically
The system SHALL read existing assistant records that lack `tool_call_id` without rewriting them. It SHALL assign stable synthetic IDs and pair compatible call and result events in persisted sequence order using per-tool FIFO matching. It SHALL preserve all available legacy arguments and result content.

#### Scenario: Legacy record has repeated tool names
- **WHEN** a legacy assistant record contains multiple calls and results with the same tool name but no persisted IDs
- **THEN** the loader SHALL pair each result with the earliest unmatched preceding call of that name and use deterministic synthetic IDs for rehydration

#### Scenario: Legacy record has an unmatched result
- **WHEN** a legacy assistant record contains a result that has no compatible preceding call
- **THEN** the loader SHALL preserve the result in an explicitly marked legacy transcript representation and SHALL NOT silently discard it
