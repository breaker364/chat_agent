## 1. Remote mutation safety

- [ ] 1.1 Add tests that distinguish read, append, edit, replace, create, and delete Lark document commands from unknown commands.
- [ ] 1.2 Add a normalized remote-mutation envelope and canonical idempotency-key builder at the skill execution boundary.
- [ ] 1.3 Add a per-run mutation ledger that reuses succeeded results and records version, hash, and verification fields.
- [ ] 1.4 Block append dispatch for repair, overwrite, reformat, and replacement workflows while preserving explicit user-requested append.
- [ ] 1.5 Add regression tests proving distinct tool call IDs cannot duplicate an equal remote mutation.

## 2. Bounded context projection

- [ ] 2.1 Add tests for oversized protected tool history that fits the model token budget but exceeds the legacy character threshold.
- [ ] 2.2 Define a token-based prompt budget with a reserved output allocation and use it for compaction and admission.
- [ ] 2.3 Project completed tool history into operation, target, status, summary, hash, and audit-reference records while retaining raw audit data.
- [ ] 2.4 Bound lossless recent protocol history by token footprint and project older completed pairs deterministically.
- [ ] 2.5 Remove the character-count terminal admission path and add recoverable failure coverage for prompts that remain over the token budget.

## 3. Terminal response and session durability

- [ ] 3.1 Add stream tests for client disconnect during model planning and during tool execution.
- [ ] 3.2 Persist blocked run progress and recovery references without persisting non-terminal model narration as an assistant message.
- [ ] 3.3 Update the frontend run state to render interrupted work separately from final chat content.
- [ ] 3.4 Replace in-place session writes with validated temporary-file commits and atomic replacement.
- [ ] 3.5 Add concurrent read/write and failed-replacement tests for session persistence.

## 4. Release safeguards

- [ ] 4.1 Add audit events and metrics for mutation deduplication, unsafe-write blocking, projection size, and context admission outcomes.
- [ ] 4.2 Add a recovery path that guides users from an unrecoverable legacy session to a new session without reissuing recorded mutations.
- [ ] 4.3 Run focused backend and frontend regression suites and document rollout and rollback switches.
