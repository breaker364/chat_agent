## 1. Storage Schema

- [x] 1.1 Define a normalized versioned tool-event shape for assistant `messages[*].tools`, including event type, sequence, tool name, `tool_call_id`, complete arguments, and complete result content.
- [x] 1.2 Propagate or generate stable `tool_call_id` values during `on_tool_start` and `on_tool_end` so each result can be paired with the exact originating call.
- [x] 1.3 Persist complete tool arguments and result content in the assistant turn transcript without applying task-output or preview truncation rules.

## 2. History Rehydration

- [x] 2.1 Change `SessionStore.get_history()` to return structured history entries that can represent text messages, assistant tool-call batches, and tool-result messages.
- [x] 2.2 Identify the three most recent completed user-to-assistant turns and mark only their tool transcripts as protected for native rehydration.
- [x] 2.3 Convert protected tool transcripts into native `AIMessage(tool_calls=...)` and matching `ToolMessage(tool_call_id=...)` objects before the next model invocation.
- [x] 2.4 Keep older conversation text history under the existing history policy while excluding native tool messages from turns outside the protected window.

## 3. Legacy Compatibility

- [x] 3.1 Normalize existing `messages[*].tools` records that lack `tool_call_id` into deterministic synthetic IDs without rewriting session files.
- [x] 3.2 Pair legacy calls and results in persisted sequence order with per-tool FIFO matching, preserving all available arguments and result content.
- [x] 3.3 Preserve unmatched legacy results in an explicitly marked representation instead of silently dropping them.

## 4. Capacity and Error Handling

- [x] 4.1 Add protected-history size accounting before model invocation that includes system messages, current user input, text history, and the complete protected tool transcript.
- [x] 4.2 Return an explicit context-capacity error when protected tool history exceeds the configured application budget, without truncating or summarizing protected payloads.
- [x] 4.3 Surface malformed protected transcript diagnostics while still preserving readable text history when possible.

## 5. Verification

- [x] 5.1 Add tests for a single tool call/result pair being persisted and rehydrated with complete arguments and result content.
- [x] 5.2 Add tests for repeated same-name tool calls, interleaved tool sequences, and exact call/result ID pairing.
- [x] 5.3 Add tests proving only the latest three completed turns rehydrate native tool messages while older turns keep text history only.
- [x] 5.4 Add tests for legacy records with missing IDs, repeated names, and unmatched results.
- [x] 5.5 Add tests for protected context overflow returning an explicit error without invoking the model.
