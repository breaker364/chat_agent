## 1. Execution observability

- [ ] 1.1 Define the bounded execution-event schema for mutation, projection, capacity, verification, interruption, and recovery outcomes.
- [ ] 1.2 Emit structured mutation and verification events with target references, hashes, counts, and reason codes.
- [ ] 1.3 Emit projection and context-admission metrics without raw tool payloads.
- [ ] 1.4 Add query helpers and tests for per-run and per-target execution summaries.

## 2. Resilient run experience

- [ ] 2.1 Define frontend typed states for activity, terminal completion, blocked interruption, recovery, and write preview.
- [ ] 2.2 Render activity events outside final chat content and preserve terminal message rendering.
- [ ] 2.3 Implement reload and reconnect restoration from persisted blocked-run progress.
- [ ] 2.4 Add document write-preview and confirmation UI for replacement and recovery mutations.
- [ ] 2.5 Add frontend tests for disconnect, reconnect, terminal completion, and preview-required paths.

## 3. Session and document recovery

- [ ] 3.1 Define and persist a bounded recovery manifest for legacy capacity failures and interrupted runs.
- [ ] 3.2 Implement new-session continuation that carries safe progress references without raw tool payload replay.
- [ ] 3.3 Implement candidate duplicate-write detection using target, operation, versions, hashes, and block summaries.
- [ ] 3.4 Add non-destructive recovery previews and explicit-confirmation boundaries for document cleanup or restore actions.
- [ ] 3.5 Consolidate duplicate session-history projection implementations behind one characterized API.

## 4. Regression and release governance

- [ ] 4.1 Add deterministic backend tests for duplicate mutations, unsafe routing, context projection, atomic persistence, conflicts, and verification mismatch.
- [ ] 4.2 Add end-to-end tests for structured import, replacement, interruption, reconnect, and recovery continuation.
- [ ] 4.3 Add generic structural fixtures that vary content without entity-specific branches or cleanup maps.
- [ ] 4.4 Document incident recovery, metric interpretation, alert thresholds, and rollback procedures.
- [ ] 4.5 Gate rollout on P0 and P1 safety invariants remaining green under the regression suite.
