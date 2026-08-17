## Why

P0 and P1 prevent immediate corruption and establish safe document operations, but the system also needs durable visibility, recovery workflows, and regression controls. Without them, repeated mutations, context pressure, stream interruptions, and stale sessions can recur without early detection or a safe operator path.

## What Changes

- Add structured observability for remote mutations, context projection, stream interruption, and verification outcomes.
- Provide a resilient frontend run experience that separates activity from final answers and supports recovery, new-session continuation, and safe write previews.
- Add session and document recovery governance for legacy oversized sessions and duplicate-write incidents.
- Add regression and quality gates covering retries, disconnects, conflicts, large payloads, and generic document structure.
- Consolidate history-projection ownership to remove duplicate session-history implementations.

## Capabilities

### New Capabilities

- `agent-execution-observability`: Emit queryable metrics and structured audit events for safety and capacity behavior.
- `resilient-run-experience`: Present terminal answers, activity, interruption, recovery, and write previews as distinct frontend states.
- `session-and-document-recovery`: Provide controlled recovery, archive, and duplicate-write remediation workflows without automatic destructive cleanup.
- `agent-safety-regression-suite`: Maintain integration scenarios that prevent recurrence across agent, session, skill, and frontend boundaries.

### Modified Capabilities

None.

## Impact

- Affects backend event emission, session management, frontend run state and activity panels, test suites, and operator documentation.
- Builds on the P0 execution guard and the P1 document replacement workflow.
- Introduces no entity-specific behavior or external product dependency.
