## 1. Backend TDD Coverage

- [x] 1.1 Add failing tests for accepting an append command when a session has an active foreground run.
- [x] 1.2 Add failing tests for rejecting append requests with no active run, stale `run_id`, terminal run state, or empty content.
- [x] 1.3 Add failing tests proving append commands are scoped by `session_id/run_id` and cannot affect another session's active run.
- [x] 1.4 Add failing tests proving multiple append commands for the same run preserve FIFO order.
- [x] 1.5 Add failing tests proving queued append commands are consumed immediately before the next LLM API call and are not reinjected.
- [x] 1.6 Add failing tests proving injected append text includes an explicit marker such as `追加指令` or `用户追加要求`.

## 2. Backend Append Queue And API

- [x] 2.1 Add a run-scoped append command model with `append_id`, `session_id`, `run_id`, `content`, `created_at`, `sequence`, and status.
- [x] 2.2 Add an in-memory FIFO append queue tied to the existing active-run lifecycle.
- [x] 2.3 Add a dedicated append endpoint for the current active run, validating target session, expected run id, terminal state, and non-empty content.
- [x] 2.4 Return structured accepted/rejected responses without mutating ordinary chat history for rejected appends.
- [x] 2.5 Clear or finalize queued append commands when the owning run reaches a terminal state.

## 3. Agent LLM Injection

- [x] 3.1 Add a narrow queue-consumption hook immediately before every LLM API call in the active run.
- [x] 3.2 Convert consumed append commands into user-originated supplemental messages with explicit `追加指令` wording.
- [x] 3.3 Preserve FIFO order when injecting multiple pending append commands.
- [x] 3.4 Mark consumed append commands as injected and ensure injected commands are not consumed again.
- [x] 3.5 Mark queued but uninjected append commands as not applied if the run finishes before another LLM API call.

## 4. Events, Persistence, And Observability

- [x] 4.1 Emit accepted/queued append lifecycle events with `session_id`, `run_id`, and `append_id`.
- [x] 4.2 Emit injected append lifecycle events when the agent consumes queued commands before an LLM API call.
- [x] 4.3 Emit rejected append lifecycle events with user-readable reasons and without exposing internal prompt-construction details.
- [x] 4.4 Persist or expose enough append metadata for session refresh/debug views to show queued, injected, rejected, and not-applied states.
- [x] 4.5 Keep append event payloads compatible with existing SSE/activity/debug parsing.

## 5. Frontend Append UX

- [x] 5.1 Keep the active session input usable while that session has a running foreground run.
- [x] 5.2 Route submit from a running active session to the append endpoint instead of starting `/chat/stream`.
- [x] 5.3 Update button/helper/status copy to make append mode clear, for example `追加到当前任务`.
- [x] 5.4 Show append accepted, injected, rejected, and not-applied states in the running session's activity/status UI.
- [x] 5.5 Preserve existing normal-send behavior for idle sessions and existing concurrent-session behavior when another session is running.
- [x] 5.6 Handle stale-run or no-active-run append errors by keeping the user's text available for normal resend.

## 6. Frontend Tests

- [x] 6.1 Add failing tests for running-session submit using the append path instead of `/chat/stream`.
- [x] 6.2 Add failing tests for idle-session submit preserving the normal new-run path.
- [x] 6.3 Add failing tests for UI copy/status indicating append mode while the active session is running.
- [x] 6.4 Add failing tests for append rejection keeping user text recoverable.
- [x] 6.5 Add tests proving append state updates are scoped to the originating session.

## 7. Verification

- [x] 7.1 Run the backend tests covering active run registry, chat streaming, append queue, and LLM injection.
- [x] 7.2 Run the frontend test/build command available for the project.
- [x] 7.3 Manually verify a long-running task accepts an append, injects it before the next LLM call, and displays accepted/injected status.
- [x] 7.4 Manually verify append rejection for idle, terminal, and stale-run cases.
- [x] 7.5 Manually verify appending in session A does not affect an active run in session B.
