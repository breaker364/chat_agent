## Context

The chat endpoints call `SessionStore.get_history()` before each run. The loader currently returns only `{role, content}` records, while the completed assistant record stores its tool events separately in `messages[*].tools`. The agent then converts all restored entries into plain `HumanMessage` or `AIMessage` objects, so prior tool calls, arguments, results, and call-to-result linkage are unavailable to the model as native tool-use context.

`task_outputs` and the conversation cache are bounded operational memory, not a lossless conversation transcript. They remain useful for audit and runtime reuse, but they cannot be the source of truth for this feature because their contents are compacted, limited, and not protocol-shaped.

## Goals / Non-Goals

**Goals:**

- Preserve a lossless, ordered tool transcript in each newly completed assistant turn.
- Restore the complete transcript for the three most recent completed turns as native LangChain assistant tool-call and tool-result messages.
- Preserve all tool arguments and result content for those protected turns without application-level truncation.
- Fail explicitly when protected history cannot fit the configured context budget rather than silently removing protected tool content.
- Read legacy session records deterministically without changing their on-disk data.

**Non-Goals:**

- Reconstruct original runtime IDs that legacy sessions never persisted.
- Preserve native tool messages for turns older than the three-turn retention window.
- Replace the tool-result cache, task-output audit log, or current per-run duplicate-call protections.
- Change tool behavior, result content, or external tool APIs.

## Decisions

### Store a versioned, correlated tool-event transcript in assistant messages

Newly persisted `messages[*].tools` entries will use a versioned normalized shape containing an event type, stable `tool_call_id`, tool name, execution sequence, complete arguments for calls, and complete content for results. The stream adapter will propagate the LangGraph tool-run identifier into both SSE events; it will generate a stable per-turn fallback identifier only when the upstream event has no identifier.

This keeps the canonical transcript beside the user-visible final assistant response, where it already has turn ownership. `append_tool_event` may keep its compact audit payloads, but `get_history` will no longer use it as the source for native rehydration.

Alternative considered: infer call/result pairs from tool names at load time. This fails with repeated or concurrent calls of the same tool, so IDs must be captured while executing new turns.

### Rehydrate protocol messages, not a textual transcript

The history contract will be expanded to structured entries. When assembling the prompt, the agent will replay each protected turn in execution order: an `AIMessage` containing the pending tool calls, its matching `ToolMessage` results, and the persisted final assistant text as a following `AIMessage`. Multiple batches of calls and results within a turn will retain their original order.

Older turns remain normal user/assistant text entries. The current user message is appended only after the reconstructed history.

Alternative considered: serialize tool activity into assistant text. It is simpler but does not give the provider the native tool-call/result relationship and does not meet the required protocol fidelity.

### Apply a three-turn protected window with strict capacity handling

The loader will identify the three latest completed user-to-assistant turns. Tool data from exactly those turns is protected from history compaction. Text-only history outside the window can still follow existing bounded-history behavior.

Before invoking the model, the agent will account for the complete protected tool payload plus system and current-user messages. If it exceeds the configured application context budget, it will return a clear context-capacity error without invoking the model. If the provider rejects the assembled messages for a stricter provider limit, error reporting will preserve the same no-silent-truncation guarantee.

Alternative considered: reduce the protected window or truncate the oldest result dynamically. Both violate the stated requirement.

### Normalize legacy records deterministically

Legacy records have name, arguments, and content but no correlation ID. The loader will create stable synthetic IDs and pair calls and results in persisted sequence order, using per-tool FIFO matching. It will preserve all available legacy fields. A result with no compatible prior call will remain visible in a clearly marked legacy transcript entry rather than being silently dropped; it cannot be re-created as an exact native pair because the original call was not stored.

## Risks / Trade-offs

- [Large session files and model prompts] → Only three turns receive lossless tool retention; reject oversized protected context explicitly and retain existing compact audit data separately.
- [Provider protocol validation] → Create provider-shape integration tests for native call/result pairing and ensure every emitted `ToolMessage` references a preceding `AIMessage` call ID.
- [Parallel or repeated tool names] → Propagate upstream run IDs and cover repeated-name/interleaved-result tests; use FIFO only for legacy data.
- [Malformed or partially written sessions] → Validate the stored event schema, preserve readable text history, and report malformed protected transcripts explicitly rather than silently omitting them.
- [Frontend compatibility] → Keep existing SSE `name`, `arguments`, and `content` fields while adding the correlation ID so existing display code continues to work.

## Migration Plan

1. Add the normalized event schema and correlation-ID propagation for all new turns.
2. Implement legacy normalization in the read path; do not rewrite existing session JSON automatically.
3. Deploy with tests covering new and legacy records, then monitor explicit context-capacity failures and malformed-legacy diagnostics.
4. Roll back by disabling native rehydration while retaining the new stored fields; the prior plain-text history path remains readable.

## Open Questions

- None. The three-turn scope and no-silent-truncation behavior are fixed by the approved requirement.
