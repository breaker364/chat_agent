## Context

The agent currently exposes remote document mutations through a generic skill request string. A model can select an append command for a replacement task, and repeated calls with different tool IDs can reach the remote service. The same session also replays complete recent tool payloads, while the admission guard uses a separate character ceiling. Finally, interrupted model streams can be persisted as assistant replies and session JSON is written in place.

The design must preserve full auditability without forcing the model to replay large payloads, and it must fail closed before an unsafe remote mutation. It must remain compatible with existing local sessions and avoid entity-specific logic.

## Goals / Non-Goals

**Goals:**

- Make remote mutations explicit, idempotent, and bounded per run.
- Prevent append from being used for replacement-style document tasks.
- Keep raw tool history available for audit and recovery while sending a bounded projection to the model.
- Use one token budget for model-context admission and compaction.
- Persist only terminal responses and atomically commit session state.

**Non-Goals:**

- Implement document-level replacement semantics; that is P1.
- Reconstruct or repair already-corrupted remote documents automatically.
- Change the model provider or increase the model context window.

## Decisions

### 1. Add a mutation envelope at the execution boundary

The skill executor will parse standardized Lark commands into an internal envelope containing target, operation, mutating flag, canonical arguments, and idempotency key. The existing free-form request remains an input format for compatibility, but no remote mutation proceeds without a recognized envelope.

Alternatives considered: prompt-only instructions are insufficient because they cannot enforce behavior; replacing the skill system is unnecessarily broad for P0.

### 2. Deduplicate by semantic mutation identity

The idempotency key will include provider, resource, target token, operation, and canonical payload hash, but not the model-generated tool call ID. A per-run ledger stores pending, succeeded, and failed outcomes. A repeated succeeded key returns the original result without a second network call.

Alternatives considered: tool-call-ID deduplication fails when the model emits the same action with new IDs; a global deduplication cache would incorrectly suppress legitimate later user actions.

### 3. Project completed tool history

Complete request/result payloads stay in the persisted audit trail. The prompt projection contains operation, target, status, result summary, content hash, and an audit reference. Only the currently open protocol pair may remain lossless, and it is bounded by a token budget.

Alternatives considered: increasing the character limit postpones the failure and still mixes units; deleting raw history breaks auditability and recovery.

### 4. Make token budget the admission authority

Context admission and compaction use model-context tokens with reserved output capacity. Character counts become diagnostics and projection triggers only. If projection cannot meet the budget, the run receives a recoverable context error before any external write.

### 5. Separate terminal response persistence

The stream layer will classify terminal completion separately from activity/model chunks. On disconnect or non-terminal end, it stores run status, progress, and recovery references, never the accumulated operational narration as a final assistant message.

### 6. Commit sessions atomically

Session serialization writes a temporary file in the same directory, validates the serialized JSON, flushes it, and replaces the target atomically. Existing locking remains in place.

## Risks / Trade-offs

- **Risk:** A repeated command with intentionally changed content may receive a different key and execute again. → **Mitigation:** Canonicalize operation and payload; include a user/run scope and expose the ledger in audit events.
- **Risk:** A model may need a detail omitted from the projection. → **Mitigation:** Include a stable audit reference and add an explicit read-back tool path; do not silently invent missing details.
- **Risk:** Terminal-only persistence changes what users see after disconnect. → **Mitigation:** Show a resumable blocked activity state in the frontend and retain the raw run trace for diagnostics.
- **Risk:** Atomic replacement can fail on platform-specific file locks. → **Mitigation:** Keep the temporary file and return a recoverable persistence error rather than overwriting in place.

## Migration Plan

1. Deploy the mutation guard in observe-and-block mode for replacement-like operations, without changing existing audit records.
2. Enable prompt projection and token-based admission for new runs; preserve old sessions as read-only audit inputs.
3. Enable terminal-only assistant persistence and atomic session writes.
4. Provide a new-session/resume path for sessions whose protected history cannot be projected.
5. Roll back by disabling the feature flags, never by re-enabling unrestricted remote append.

## Open Questions

- Which existing tool results require a lossless native replay pair after a completed turn? This must be answered by the protocol tests before rollout.
- What exact output-token reserve should be configured for each supported model context window?
