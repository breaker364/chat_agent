## Why

Agent runs can currently repeat remote document mutations, become permanently blocked by oversized protected tool payloads, and persist unfinished model narration as chat replies. These failures can corrupt user-visible documents and make a session unusable even when the configured model still has context capacity.

## What Changes

- Add a fail-closed mutation guard for remote document writes, including per-run idempotency and duplicate-result reuse.
- Prevent document-repair and replacement workflows from executing an append operation.
- Separate immutable tool audit storage from the bounded projection supplied to the model so oversized completed tool payloads do not deadlock a session.
- Use one token-based prompt budget for compaction and admission decisions; character size becomes a projection signal rather than a terminal failure condition.
- Persist only terminal answers as assistant messages; record interrupted work as resumable run state instead of model-stream narration.
- Save session files atomically.

## Capabilities

### New Capabilities

- `safe-remote-mutation-guard`: Classify remote document mutations, make them idempotent within a run, and reject unsafe write modes.
- `bounded-tool-history-projection`: Preserve complete tool audit data while supplying a token-bounded, recoverable history projection to the model.
- `terminal-response-persistence`: Persist only completed assistant responses and recover interrupted runs without exposing operational narration.
- `atomic-session-persistence`: Commit session state without exposing partially written JSON to concurrent readers.

### Modified Capabilities

None.

## Impact

- Affects `backend/tools.py`, `backend/skills.py`, `backend/agent.py`, `backend/main.py`, and `backend/session_store.py`.
- Changes runtime behavior for Feishu/Lark document mutations and for sessions with large tool histories.
- Requires focused backend and frontend regression tests; no external dependency is required.
