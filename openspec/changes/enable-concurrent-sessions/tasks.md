## 1. Backend Run Control

- [x] 1.1 Add a per-session active run registry that can register, reject, and release foreground runs by `session_id`.
- [x] 1.2 Ensure `/chat/stream` and `/chat` acquire a run before appending the user message or setting progress.
- [x] 1.3 Return a structured active-run error for duplicate foreground requests in the same session without mutating session history.
- [x] 1.4 Release the active run in all terminal paths, including success, handled error, unhandled exception, cancellation, and client disconnect.

## 2. Backend Runtime Context

- [x] 2.1 Replace request-scoped `os.environ` writes for `CHAT_AGENT_SESSION_ID` and `CHAT_AGENT_RUN_ID` with a `contextvars`-based runtime context.
- [x] 2.2 Update `backend/tools.py` current session/run helpers to read the runtime context, keeping environment fallback only for compatibility paths.
- [x] 2.3 Propagate runtime context through project-owned thread helpers with `contextvars.copy_context()`.
- [x] 2.4 Keep background subagent records tied to the triggering session through explicit `session_id` passing.
- [x] 2.5 Clear per-run tool dedupe cache using the run id captured at run registration.

## 3. Backend Stream Attribution

- [x] 3.1 Add session/run attribution to structured SSE payloads where the payload is JSON.
- [x] 3.2 Ensure `SessionEventHub` events emitted during a run remain scoped to the originating session and include run attribution where available.
- [x] 3.3 Keep existing SSE event names compatible with current frontend parsing.
- [x] 3.4 Ensure sync `/chat` and streaming `/chat/stream` share the same run lifecycle and context isolation semantics.

## 4. Frontend Session-Scoped State

- [x] 4.1 Replace global run state with a `sessionRuns` map keyed by `session_id`.
- [x] 4.2 Replace the single `abortRef` with a ref-backed map of `session_id` to `AbortController`.
- [x] 4.3 Derive active loading, streaming text, tool events, activity events, debug events, and stop availability from the active session's run state.
- [x] 4.4 Scope send disabling to the active session rather than any running session.
- [x] 4.5 Preserve inactive session run state while the user views another session.

## 5. Frontend Message Isolation

- [x] 5.1 Add `messagesBySession` caching or equivalent guarded updates so async stream completion writes only to the originating session.
- [x] 5.2 Update `loadSession` to populate the selected session's cached messages without clearing unrelated session run state.
- [x] 5.3 Update `handleSend` to append user, assistant, error, and abort messages to the originating session only.
- [x] 5.4 Update auto-scroll and polling behavior so active streaming state is not overwritten by unrelated session refreshes.
- [x] 5.5 Show session-level running or terminal status in the session list from local run state and refreshed backend progress.

## 6. Cancellation UX

- [x] 6.1 Update stop handling to abort only the current active session's controller.
- [x] 6.2 Mark only the targeted session run as stopped in local state.
- [x] 6.3 Verify stopping one running session does not abort or clear another session's stream state.

## 7. Backend Tests

- [x] 7.1 Add a concurrent context isolation test where two sessions run overlapping tool calls and write tool/cache/task data to their own sessions.
- [x] 7.2 Add a same-session duplicate run test that verifies the second request is rejected and no extra user message is appended.
- [x] 7.3 Add registry cleanup tests for successful completion, tool error, stream error, and client disconnect.
- [x] 7.4 Add a test proving per-run dedupe cache cleanup still uses the correct run id under concurrent sessions.

## 8. Frontend Tests And Verification

- [x] 8.1 Add reducer/helper tests for updating run state by session id and ignoring stale run updates.
- [x] 8.2 Add tests or focused component coverage for send disabled state when another session is running.
- [x] 8.3 Add tests or focused component coverage for session-scoped stop behavior.
- [x] 8.4 Run the existing backend test suite relevant to chat streaming, tool history, and session store.
- [x] 8.5 Run the frontend build or test command available in the project.
- [ ] 8.6 Manually verify session A long run plus session B concurrent run, independent stop, and correct persisted histories.
