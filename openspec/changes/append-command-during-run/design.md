## Context

The project already supports persisted sessions, SSE streaming responses, per-session run state, and isolation between concurrently running sessions. A session can have an active foreground run, but once that run starts the user cannot currently add new instructions into the in-flight agent loop. The only practical options are to wait, stop the run, or start over with a new prompt.

This change adds an explicit append-command path for the current active run in a session. The appended prompt is not a new independent user task. It is a run-scoped supplement that must be queued, made visible to the user, and injected into the agent before the next LLM API call with wording that clearly marks it as an appended instruction, for example `用户追加指令（运行中补充）：...`.

The design assumes the current single-process backend runtime. The append queue is therefore in-memory and tied to the active run registry. If the deployment later moves to multiple backend workers, the queue and active-run registry need a shared store or sticky routing.

## Goals / Non-Goals

**Goals:**

- Allow a user to append one or more prompts to the active run of the current session while that run is still executing.
- Scope appended prompts by `session_id` and `run_id`, so an append in one session cannot affect another session or a replaced run.
- Inject queued append prompts before the next LLM API call, preserving FIFO order.
- Prefix injected content with an explicit append marker such as `追加指令` or `用户追加要求`.
- Surface append accepted/injected/rejected state through API responses and stream/activity/debug events.
- Keep the existing `/chat/stream` task-start behavior compatible for normal new messages.
- Keep same-session terminal-state behavior clear: once a run finishes, new text starts a new task instead of being appended to the old run.

**Non-Goals:**

- Do not support concurrent independent foreground tasks inside the same session.
- Do not guarantee injection into a run that has already made its final LLM API call.
- Do not introduce a distributed queue or cross-worker coordination mechanism in this change.
- Do not reinterpret append commands as hidden system prompts; they remain user-originated supplemental instructions.
- Do not change the existing isolation semantics for different sessions running concurrently.

## Decisions

### Decision 1: Add a dedicated append API for active runs

Introduce a backend endpoint dedicated to appending content to an active run. The recommended shape is one of:

- `POST /sessions/{session_id}/runs/current/append`
- `POST /runs/{run_id}/append` with `session_id` validation

The request body should include the appended prompt content and, when available from the frontend, the expected `run_id`. The backend must validate that the target session currently has an active run and that the expected run id matches the active run before accepting the append.

Alternative considered: overloading `/chat/stream` with a flag such as `append: true`. This would reuse an existing endpoint but makes task-start and run-append semantics easier to mix accidentally. A dedicated endpoint gives cleaner validation and clearer frontend behavior.

### Decision 2: Store append commands in a run-scoped FIFO queue

Extend the active run registry or create a small companion module that stores pending append commands by `session_id/run_id`. Each entry should include:

- `append_id`
- `session_id`
- `run_id`
- `content`
- `created_at`
- `sequence`
- `status`, with values such as `queued`, `injected`, or `rejected`

The queue must be consumed in FIFO order. Multiple append prompts accepted before the next model call may be injected as separate user messages or as one combined user message, but their original order must remain visible and testable.

Alternative considered: writing append prompts directly to persisted chat history first and having the agent reread history. This is more durable but risks confusing ordinary conversation history with in-flight control input. The run queue keeps behavior precise; persistence can record append events separately for auditability.

### Decision 3: Consume pending appends immediately before every LLM API call

`stream_agent_events` or the model-call wrapper should check for pending append commands at the latest safe point before invoking the LLM API. This includes normal model turns and any repair/retry turn that calls the model again.

When commands are found, the agent should inject them into the message context using an explicit user-originated marker, for example:

```text
用户追加指令（运行中补充，第 1 条）：
<append content>
```

The marker is part of the normative behavior because the model must be able to distinguish the new instruction from the original task, historical chat, tool output, or developer/system guidance.

Alternative considered: injecting append commands as a system message. This would make them overly authoritative and blur their origin. A marked user message better matches the user's intent and reduces policy ambiguity.

### Decision 4: Frontend running input enters append mode

When the currently visible session has a running foreground run, the input box should remain available. Sending text in that state should call the append API instead of starting `/chat/stream`. The UI should make the mode clear with button text or helper text such as `追加到当前任务`.

If the active session is idle, the same input path continues to start a normal new run. If another session is running but the current session is idle, existing concurrent-session behavior remains unchanged and the current session can start its own run.

Alternative considered: adding a separate append panel. It avoids ambiguity but costs more UI space and is unnecessary if the input mode is clearly labeled while running.

### Decision 5: Emit append lifecycle events

The backend should emit structured events for append lifecycle transitions:

- accepted/queued after the append API validates and stores the prompt
- injected when the agent consumes the prompt before an LLM API call
- rejected when there is no active run, the run id is stale, the content is empty, or the run is terminal

These events should include `session_id`, `run_id`, and `append_id` where applicable. User-facing activity should summarize the append without exposing unnecessary internal prompt construction details.

### Decision 6: Terminal runs reject append requests

Append requests must not create a new run implicitly. If the target run has completed, failed, stopped, or been cleaned up, the API should reject the append with a user-readable error. The frontend can then offer to send the text as a normal new message.

This prevents a race where the user believes text affected an in-flight run but it instead starts or mutates a different conversation turn.

## Risks / Trade-offs

- [Risk] The run can finish before the next LLM API call, so an accepted append may never influence the final answer. → Mitigation: accept only while the run is active, expose queued/injected status, and mark any remaining queued appends as not injected during cleanup.
- [Risk] In-memory queues are lost if the backend process restarts. → Mitigation: document the single-process assumption and persist append lifecycle events for visibility; move queue storage later if multi-worker/restart durability becomes required.
- [Risk] Users may confuse a normal send with append mode. → Mitigation: change button/helper text while the active session is running and show accepted/injected activity items.
- [Risk] Injecting too much append text could bloat the next model call. → Mitigation: enforce existing prompt/content size limits and reject or truncate only through explicit, user-visible validation.
- [Risk] A stale frontend may append to a run that has already ended. → Mitigation: require backend active-run validation and return a clear terminal/stale-run error.

## Migration Plan

1. Add backend tests for append queue acceptance, rejection, ordering, cross-session isolation, and terminal cleanup.
2. Add the append queue and append API behind the existing active-run lifecycle.
3. Add agent-side consumption immediately before LLM API calls and emit accepted/injected lifecycle events.
4. Update the frontend input flow so active-session running state calls append instead of starting a new stream.
5. Add frontend tests for running-session append mode, idle-session normal send, stale-run rejection, and status display.
6. Run targeted backend and frontend tests, then manually verify a long-running task receives an appended prompt before the next model call.

## Open Questions

- Should multiple queued append commands be injected as separate user messages or combined into one marked user message per LLM call?
- Should append events be persisted as session messages, run activity records, or both?
- What maximum content length should the append endpoint enforce, and should it reuse the normal chat message limit?
- If the run finishes with queued but uninjected commands, should the frontend offer one-click resend as a new task?
