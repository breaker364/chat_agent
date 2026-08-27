## Why

The agent can inspect individual paths and execute Python files, but it cannot efficiently discover files by pattern, search workspace text with line-level evidence, or run project-native command-line workflows. Adding these generic capabilities lets the agent investigate and validate work using the same workspace context while keeping file-system access, resource use, and command execution controlled.

## What Changes

- Add a `bash` tool that runs non-interactive Bash commands through an isolated workspace execution backend and returns structured exit, output, timeout, and truncation information.
- Add a `glob` tool that returns deterministic workspace-relative paths matching a supplied glob pattern without exposing paths outside the approved read scope.
- Add a `grep` tool that searches text files with a caller-provided regular expression and returns bounded, line-numbered matches.
- Register the three tools with the LangGraph agent runtime, tool policy/deduplication layer, event transcript, and generic tool-use guidance.
- Add runtime configuration, audit metadata, and focused tests for capability contracts, sandbox boundaries, timeouts, output limits, and error handling.

## Capabilities

### New Capabilities

- `agent-workspace-shell-tools`: Safe, generic workspace command execution, file-pattern discovery, and content search for the agent.

### Modified Capabilities

- None.

## Impact

- Affected code: `backend/tools.py`, `backend/agentic_research/runtime.py`, agent prompt/policy assets, runtime configuration loading, and related backend tests.
- Affected runtime behavior: the model will receive three additional local workspace tools. `glob` and `grep` are read-only evidence tools; `bash` is an explicitly audited command-execution tool with no host fallback when the required sandbox is unavailable.
- Affected operational requirements: deployments that enable `bash` need a configured Bash-compatible sandbox backend and command/resource limits. The glob and grep tools require no entity-specific configuration or external service.
