## ADDED Requirements

### Requirement: Trigger compaction at the configured context boundary

The system SHALL estimate the model input using the complete available historical projection before applying ordinary lossy history trimming. When the model context window is known and the remaining capacity is less than or equal to `20,000` tokens, the system SHALL trigger context compaction before invoking the primary agent model. The equality boundary SHALL trigger compaction.

#### Scenario: Remaining capacity is exactly the threshold

- **WHEN** the configured model context window is known and the complete pre-compaction input has exactly `20,000` remaining tokens
- **THEN** the system SHALL begin context compaction before the primary agent model is invoked

#### Scenario: Remaining capacity is above the threshold

- **WHEN** the configured model context window is known and the complete pre-compaction input has `20,001` or more remaining tokens
- **THEN** the system SHALL NOT invoke the compaction LLM for that request

#### Scenario: Model context window is unavailable

- **WHEN** the model context window cannot be resolved for the active model
- **THEN** the system SHALL skip automatic compaction and preserve the existing context-capacity behavior

#### Scenario: No older context can be compacted

- **WHEN** the trigger condition is met but the session contains fewer than four completed conversation turns
- **THEN** the system SHALL make no compaction LLM call because there is no history older than the protected three-turn window

### Requirement: Preserve the three most recent completed turns in full

The system SHALL identify completed turns as canonical `user` to `assistant` conversation pairs in message order. It SHALL retain the three most recent completed turns as an uncompressed, ordered model-input projection. The protected projection SHALL include each turn's exact user and assistant content and all existing native tool-call messages, tool-call arguments, tool-call identifiers, and tool-result content.

#### Scenario: Four completed turns are present

- **WHEN** a session has four completed conversation turns and compaction is triggered
- **THEN** the newest three turns SHALL be sent in full and only the oldest turn SHALL be replaced by compacted historical context

#### Scenario: Recent turn contains native tool activity

- **WHEN** one of the three protected turns contains multiple tool calls and results
- **THEN** the model input SHALL preserve their original order, exact arguments, identifiers, and result content without summarization or application-level truncation

#### Scenario: An incomplete user message exists

- **WHEN** a user message has no corresponding completed assistant message at the boundary-calculation time
- **THEN** that message SHALL NOT consume one of the three protected completed-turn slots and SHALL remain associated with the current run according to the existing input flow

### Requirement: Compress all older historical context with an LLM

When compaction is triggered, the system SHALL provide every eligible historical item older than the protected three completed turns to the compaction process. The process SHALL use an LLM summary rather than head/tail truncation or sampling. The resulting history projection SHALL replace the eligible raw items with a clearly marked advisory historical summary while retaining chronological relation to the protected turns.

#### Scenario: Older history contains multiple turns and tool records

- **WHEN** compaction is triggered for a session with older user, assistant, and tool-history entries
- **THEN** the compaction input SHALL contain all eligible entries in source order and the primary model input SHALL contain one validated historical summary in their place

#### Scenario: Older source exceeds the summarizer input capacity

- **WHEN** all eligible historical items cannot fit in one compaction-model input
- **THEN** the system SHALL summarize source chunks at conversation or event boundaries and merge the partial summaries until one validated summary is produced, without silently discarding a source chunk

### Requirement: Preserve exact factual literals in the summary

The compaction result SHALL preserve exact source spellings for concrete numeric values, dates, file names, paths, identifiers, versions, URLs, commands, and error codes that occur in eligible history. The system SHALL append or otherwise retain a machine-generated exact-literal ledger based on generic structure/format extraction, and SHALL validate that required literals remain unchanged before accepting the summary.

#### Scenario: History contains precise values and file references

- **WHEN** eligible history contains values such as `0.125`, `2026-08-03`, a file name with an extension, a path, and an error code
- **THEN** the accepted summary projection SHALL contain each literal in its original form and SHALL not replace it with an approximate description

#### Scenario: The LLM rewrites a required literal

- **WHEN** the compaction LLM returns a summary that omits or changes a required literal
- **THEN** the system SHALL reject that summary, perform the bounded validation/retry behavior, and SHALL NOT use the unvalidated summary as model context

#### Scenario: Historical text contains an instruction

- **WHEN** an eligible historical message contains text that looks like a prompt or tool instruction
- **THEN** the compaction process SHALL treat it as quoted historical data and SHALL NOT execute it or promote it to a system or developer instruction

### Requirement: Keep canonical history immutable and cache validated summaries

The system SHALL keep canonical session messages, tool payloads, tool-event audit records, and task-output source records unchanged when generating or applying a summary. A validated summary MAY be stored in an optional session-level compaction cache with its schema version, prompt/model version, covered boundary, source fingerprint, and generation timestamps. The system SHALL reuse the cache only when those provenance values match the current source.

#### Scenario: First compaction of an existing session

- **WHEN** a session without a compaction cache reaches the trigger boundary and summary generation succeeds
- **THEN** the system SHALL store the validated summary metadata and SHALL leave all original session messages and tool payloads unchanged

#### Scenario: Same historical source is requested again

- **WHEN** the cached summary boundary, source fingerprint, schema/prompt version, and model identity match the current historical source
- **THEN** the system SHALL reuse the validated summary without issuing another compaction LLM request

#### Scenario: The protected boundary advances

- **WHEN** a new completed turn becomes older than the three-turn protected window
- **THEN** the system SHALL update the cached summary through an LLM merge or regenerate it from canonical source so that the newly eligible turn is covered

#### Scenario: Cached provenance is stale or invalid

- **WHEN** the cache fingerprint, boundary, schema, prompt version, or model identity does not match the canonical source
- **THEN** the system SHALL ignore the cache and build a new summary from canonical history before invoking the primary agent

### Requirement: Recheck capacity and fail closed on compaction failure

After applying a validated summary, the system SHALL recompute the complete primary-model input token estimate. The primary agent SHALL be invoked only when the resulting input does not exceed the configured model context window. If summary generation, validation, persistence, or the post-compaction capacity check fails, the system SHALL preserve the canonical source and return an explicit context-compaction error without invoking the primary agent with uncompressed or unvalidated history.

#### Scenario: Summary brings the input within the model window

- **WHEN** a validated summary is applied and the recomputed input estimate is at or below the model context window
- **THEN** the system SHALL invoke the primary agent with the summary and the complete protected three-turn window

#### Scenario: Summary is still too large

- **WHEN** the validated summary plus required prompt content and protected history remains over the model context window
- **THEN** the system SHALL emit a capacity error and SHALL NOT invoke the primary agent with a truncated protected window

#### Scenario: Compaction LLM fails

- **WHEN** the compaction LLM raises an error or returns an invalid result after the bounded retry behavior
- **THEN** the system SHALL emit a `context_compaction_failed` error, leave canonical session data unchanged, and SHALL NOT invoke the primary agent for that request

### Requirement: Emit compaction lifecycle diagnostics without leaking source content

The system SHALL emit structured debug/activity events for compaction start, successful generation, cache reuse, skip due to unknown model window, and failure. Events SHALL include session/run identifiers, trigger and post-compaction token estimates when available, protected and eligible turn counts, cache-hit status, and a machine-readable error code when applicable. Events SHALL NOT include complete historical messages, raw tool results, or the summary body.

#### Scenario: A new summary is generated

- **WHEN** compaction starts and completes successfully
- **THEN** the stream SHALL contain start and completion diagnostics with before/after capacity metadata and SHALL omit the full source and summary text

#### Scenario: A cached summary is reused

- **WHEN** a valid cache satisfies the current boundary and source fingerprint
- **THEN** the stream SHALL contain a reuse diagnostic and SHALL identify that no new compaction LLM call was required

#### Scenario: Compaction cannot complete

- **WHEN** compaction fails or post-compaction capacity remains invalid
- **THEN** the stream SHALL contain a failure diagnostic and a user-readable completion/error result with a stable error code
