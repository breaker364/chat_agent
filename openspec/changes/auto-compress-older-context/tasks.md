## 1. Context Model and Configuration

- [x] 1.1 Define the optional context-compaction configuration contract with an inclusive `20,000` token trigger, a three-completed-turn retention window, summary output limits, chunk limits, and backward-compatible defaults.
- [x] 1.2 Add pure helpers to partition canonical history into eligible older items and the three most recent completed turns, including incomplete-turn handling and stable chronological ordering.
- [x] 1.3 Add canonical serialization, source-boundary metadata, and a deterministic source fingerprint that do not mutate session messages or tool payloads.
- [x] 1.4 Add generic exact-literal extraction for numeric values, dates, file names, paths, identifiers, versions, URLs, commands, and error codes without entity-specific allowlists.

## 2. Summary Generation and Validation

- [x] 2.1 Implement a dedicated no-tool compaction LLM call using the active model configuration, deterministic generation settings, explicit historical-data delimiters, and a fixed summary schema.
- [x] 2.2 Implement source chunking at conversation or tool-event boundaries and hierarchical merging for eligible history that exceeds the compaction model input capacity.
- [x] 2.3 Append the machine-preserved exact-literal ledger to the generated summary and validate required literals, non-empty output, schema sections, and output budget before accepting it.
- [x] 2.4 Add bounded retry and structured failure handling for invalid, incomplete, or provider-error summary responses; do not return an unvalidated summary.

## 3. Session Summary Cache

- [x] 3.1 Add optional `context_compaction` session metadata containing schema/prompt/model versions, covered boundary, source fingerprint, summary, timestamps, and token estimates.
- [x] 3.2 Implement atomic cache write and read validation, including cache invalidation when canonical history, the protected boundary, prompt version, schema version, or model identity changes.
- [x] 3.3 Implement rolling summary updates when a previously protected turn becomes eligible, using the existing summary plus the newly eligible canonical source rather than appending unprocessed raw history.
- [x] 3.4 Serialize summary generation and cache commit per session and preserve the last valid cache when a new compaction attempt fails.

## 4. Agent Integration and Events

- [x] 4.1 Integrate pre-trim full-history capacity estimation into `stream_agent_events()` and trigger compaction only when the model window is known and remaining capacity is at or below `20,000` tokens.
- [x] 4.2 Assemble the primary model input from the validated historical summary, the complete protected three-turn native tool history, required runtime context, and the current user message in chronological order.
- [x] 4.3 Recompute post-compaction token capacity and prevent the primary agent call when the summary is invalid, persistence fails, or the assembled input exceeds the configured model window.
- [x] 4.4 Emit structured start, completed, cache-reused, skipped-unknown-window, and failed compaction events with capacity metadata and stable error codes, excluding source history and summary bodies.
- [x] 4.5 Preserve existing ordinary history trimming, tool-history rehydration, run append injection, and session event behavior for requests that do not require compaction.

## 5. Verification

- [x] 5.1 Add unit tests for the inclusive threshold, above-threshold skip, unknown-window skip, no-eligible-history skip, and post-compaction capacity calculation.
- [x] 5.2 Add history partition tests proving that exactly the three newest completed turns and their full native tool payloads remain unchanged while all older items enter the summary source.
- [x] 5.3 Add summary tests for exact literal preservation, prompt-injection-as-data handling, chunk/merge behavior, invalid output rejection, bounded retry, and provider failure.
- [x] 5.4 Add cache tests for first write, provenance-matched reuse, rolling boundary updates, stale-cache regeneration, canonical-data immutability, and per-session concurrency serialization.
- [x] 5.5 Add agent-stream tests proving lifecycle events, no primary-model call on compaction failure or overflow, and successful primary invocation with the summary plus protected history.
- [x] 5.6 Run the existing context-capacity, tool-history, session-store, and full backend test suites; resolve regressions before implementation is marked complete.
