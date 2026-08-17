## Context

P0 makes individual runs safe and resumable; P1 introduces safe document replacement. The remaining risk is operational: users and maintainers need to see why a run stopped, recover without repeating mutations, identify emerging context or duplicate-write pressure, and prevent regressions across the backend/frontend boundary. Session history currently has duplicate ownership paths, which increases the chance that future safety changes apply to only one implementation.

## Goals / Non-Goals

**Goals:**

- Make safety decisions, capacity pressure, mutation outcomes, and recovery paths observable without leaking raw tool payloads into the UI.
- Give users a clear distinction between activity, final answer, interrupted work, and external write previews.
- Provide non-destructive recovery for legacy sessions and duplicate-write incidents.
- Establish automated regression coverage for the failure modes discovered in this incident.
- Consolidate history-projection ownership.

**Non-Goals:**

- Automatically delete remote duplicate content or restore versions without user confirmation.
- Build a general-purpose analytics platform.
- Relax P0 mutation or P1 replacement safeguards for convenience.

## Decisions

### 1. Emit bounded structured execution events

Backend events will use stable fields for run ID, event type, operation, target reference, disposition, reason code, elapsed time, and sizes/tokens. Raw arguments, document bodies, and credentials remain in audit storage and are represented by hashes or references.

### 2. Treat the frontend as a run-state viewer

The frontend will render activity, terminal answer, blocked state, and recovery options from typed run state rather than inferring state from message text. Write previews expose operation, target, version, and expected structural delta without displaying full sensitive payloads by default.

### 3. Use non-destructive recovery manifests

A blocked legacy session receives a compact recovery manifest that identifies safe reusable outputs, failed stage, and next permitted action. Document duplicate detection produces a preview with version references and hashes; only an explicit confirmed action can change remote content.

### 4. Enforce regression scenarios at boundaries

Tests are organized around externally observable invariants: exactly-once remote mutation, bounded prompt projection, terminal-only messages, atomic persistence, replacement verification, disconnect recovery, and generic structural import. Fixtures vary content rather than embedding source-specific logic.

### 5. Maintain one history-projection implementation

The duplicate `get_history` implementations will be consolidated behind one tested projection path so audit, prompt history, and recovery all agree on event ordering and protection rules.

## Risks / Trade-offs

- **Risk:** More run states can complicate the frontend. → **Mitigation:** use a small typed state model and retain one terminal message contract.
- **Risk:** Metrics can expose oversized or sensitive values. → **Mitigation:** emit counts, hashes, and references rather than raw payloads.
- **Risk:** Recovery guidance can be mistaken for automatic mutation. → **Mitigation:** label preview, recovery, and confirmed-write actions distinctly and require confirmation for destructive choices.
- **Risk:** Broad regression suites become slow. → **Mitigation:** keep deterministic unit fixtures for every invariant and a smaller end-to-end smoke suite.

## Migration Plan

1. Add event schema and backend emission with dashboard-free local inspection first.
2. Update frontend rendering to consume typed states while retaining compatibility with existing messages.
3. Add recovery manifests and non-destructive duplicate detection for legacy sessions.
4. Consolidate history projection after characterization tests cover existing sessions.
5. Gate releases on the regression matrix and monitor safety metrics before enabling automated recovery helpers.

## Open Questions

- Which event retention period balances debugging value with local storage growth?
- Which user confirmation pattern is appropriate for a document-version restore action?
