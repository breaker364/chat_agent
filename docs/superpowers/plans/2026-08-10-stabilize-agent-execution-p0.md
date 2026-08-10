# Stabilize Agent Execution P0 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Follow the repository TDD workflow task-by-task. Each checklist item must be red, green, and verified before the next item.

**Goal:** Make remote skill mutations safe and idempotent, keep model history within one token budget, persist only terminal responses, and commit sessions atomically.

**Architecture:** Add a provider-agnostic command manifest and per-run mutation ledger at the skill boundary. Keep full tool events in session audit storage while constructing a deterministic token-bounded model projection. Treat stream completion and interruption as separate state transitions, and make all session JSON writes validated temporary-file replacements.

**Tech Stack:** Python 3.12, FastAPI/SSE, LangChain/LangGraph, pytest, React 19, Vite/Vitest.

## Global Constraints

- Production command handling MUST remain entity-agnostic; tests may use concrete provider examples.
- Unknown or unclassified mutating commands fail closed before dispatch.
- Raw tool requests/results remain available for audit even when the model receives a projection.
- A single token budget, including reserved output capacity, controls compaction and admission.
- Only validated terminal responses become assistant chat messages.
- Session replacement failures preserve the last good session and expose the temporary candidate path.

## Implementation Map

### Task 1: Remote Mutation Guard

**Files:** Create `backend/mutation_guard.py`; modify `backend/skills.py`, `backend/tools.py`; test `backend/tests/test_mutation_guard.py`.

Define `RemoteMutationEnvelope`, a generic command parser, a canonical identity builder, and a per-run ledger. The parser accepts standardized provider/resource/operation commands and emits `read`, `create`, `append`, `edit`, `replace`, `delete`, or `unknown`; it must not branch on a named entity. The skill executor validates the envelope before invoking a runner, rejects conflicting replacement workflows, and records successful results with versions, content hash, and verification status. The ledger key excludes model tool-call IDs and reuses a succeeded result.

TDD sequence: add classification and unknown-command tests; add canonical-key and changed-payload tests; add duplicate-call tests with different IDs; run each test to observe the expected failure; implement the smallest parser/ledger/dispatch adapter; then run the focused module and existing skill-policy tests.

### Task 2: Bounded History Projection

**Files:** Modify `backend/token_counter.py`, `backend/context_compaction.py`, `backend/session_store.py`, `backend/agent.py`; test `backend/tests/test_history_projection.py` and extend `backend/tests/test_context_capacity.py`.

Represent completed tool records as operation, target, status, sanitized summary, content hash, and audit reference while leaving `tool_events.jsonl` unchanged. Retain only recent complete native pairs while their token footprint fits the protected budget; project older pairs deterministically. Replace character admission with `model_context_window - reserved_output_tokens` and return a recoverable capacity error if the projection still cannot fit. Add tests for character-heavy but token-fitting prompts, deterministic projection, incomplete pairs, and unrecoverable overflow.

### Task 3: Terminal Runs and Atomic Sessions

**Files:** Modify `backend/main.py`, `backend/session_store.py`; test `backend/tests/test_terminal_response_persistence.py` and extend `backend/tests/test_session_store.py`.

On disconnect during planning or tool execution, cancel pending stream work, persist blocked progress, completed outputs, ledger references, and a bounded recovery summary, and leave no partial assistant message. On a validated `done` event, persist exactly that terminal response. Serialize a candidate session, parse it back for validation, flush it, and atomically replace the target while preserving the candidate on replacement failure. Add concurrent reader/writer and failed-replacement tests.

### Task 4: Frontend Recovery and Safeguards

**Files:** Modify `frontend/src/sessionRunState.js`, `frontend/src/App.jsx`; test `frontend/src/sessionRunState.test.js` and add focused App tests.

Represent `interrupted`/`blocked` runs separately from chat messages, hydrate that state from session progress, render a continuation action, and never promote stream narration to a final message after a disconnect. Add audit/metric fields for guard decisions, projection size, admission outcome, and recovery routing. Update OpenSpec task checkboxes after each completed behavior and run backend plus frontend regression suites and build.

## Verification

- Backend focused: `\.venv_py312\\Scripts\\python.exe -m pytest backend/tests/test_mutation_guard.py backend/tests/test_history_projection.py backend/tests/test_terminal_response_persistence.py backend/tests/test_session_store.py -q`
- Backend full: `\.venv_py312\\Scripts\\python.exe -m pytest backend/tests -q`
- Frontend tests: `npm --prefix frontend exec -- vitest run --reporter=dot`
- Frontend build: `npm --prefix frontend run build`
