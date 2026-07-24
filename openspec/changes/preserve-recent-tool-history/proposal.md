## Why

The session store retains tool activity, but the current history loader only restores plain user and assistant text. The model therefore loses the native tool-call protocol, exact arguments, and full results from previous turns, which makes it more likely to repeat work.

## What Changes

- Persist each tool call and tool result in `messages[*].tools` with a stable correlation ID, complete arguments, complete result content, and execution order.
- Rehydrate tool activity from the three most recent completed conversation turns as native assistant tool-call and tool-result messages before the next model invocation.
- Deduplicate identical tool-call/result pairs when `get_history()` prepares the protected native rehydration window, while keeping canonical `messages[*].tools` storage lossless for audit/debugging.
- Preserve the complete payloads for those three turns without application-level truncation; report an explicit context-capacity failure instead of silently discarding tool data.
- Provide deterministic compatibility handling for legacy tool records that do not contain a persisted correlation ID.
- Change the run completion rule so the agent only stops after a model pass has either produced a final answer or explicitly had the opportunity to continue with more `tool_use`; if more tools are needed, the system continues the agent loop within bounded safety limits.

## Capabilities

### New Capabilities

- `tool-history-rehydration`: Persist and restore the complete native tool-use transcript for the three most recent conversation turns.

### Modified Capabilities

- None.

## Impact

- Affected code: `backend/agent.py`, `backend/main.py`, `backend/session_store.py`, and tool-history tests.
- The internal history contract changes from plain `{role, content}` dictionaries to structured entries that can represent assistant tool calls and tool results.
- The history loader becomes a context projection: it may deduplicate identical protected tool pairs for prompt pressure, but it must not rewrite or drop the canonical stored transcript.
- The streaming run loop may perform an additional bounded continuation pass to let the agent decide whether to call tools again before finalizing.
- Existing session data remains readable through deterministic legacy normalization, although unavailable legacy runtime IDs cannot be recovered verbatim.
