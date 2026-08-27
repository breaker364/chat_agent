## ADDED Requirements

### Requirement: Workspace glob discovery

The `glob` tool SHALL accept a workspace-relative glob pattern and return a deterministic, bounded list of matching workspace-relative files and directories. It MUST reject patterns that resolve outside the approved workspace/read roots and MUST report truncation instead of silently dropping matches.

#### Scenario: Matching files are returned in stable order
- **WHEN** the agent calls `glob` with a valid relative pattern that matches multiple entries
- **THEN** the tool returns normalized workspace-relative paths sorted deterministically and includes the total returned count

#### Scenario: No entries match
- **WHEN** the agent calls `glob` with a valid pattern that matches no approved entries
- **THEN** the tool returns `status: "ok"`, an empty match list, and `truncated: false`

#### Scenario: Pattern escapes the workspace
- **WHEN** the pattern is absolute or traversal resolves outside the approved read scope
- **THEN** the tool returns a structured permission error and performs no directory traversal outside that scope

#### Scenario: Match limit is exceeded
- **WHEN** more entries match than the configured maximum
- **THEN** the tool returns the bounded prefix in deterministic order with `truncated: true` and the configured limit

### Requirement: Bounded regular-expression search

The `grep` tool SHALL search approved text files under a workspace-relative file or directory scope using a caller-provided regular expression. Results MUST include the relative path, 1-based line number, and bounded line text; binary files, unreadable files, oversized files, and internal protected indexes MUST not be read outside existing policy.

#### Scenario: Matching lines are returned
- **WHEN** the agent calls `grep` with a valid expression and an approved scope containing matches
- **THEN** the tool returns line-level matches with relative paths and 1-based line numbers in deterministic order

#### Scenario: No lines match
- **WHEN** the expression is valid and no approved text line matches
- **THEN** the tool returns `status: "ok"`, an empty match list, and bounded scan metadata

#### Scenario: Expression is invalid
- **WHEN** the caller supplies a syntactically invalid regular expression
- **THEN** the tool returns `status: "invalid_input"` with a safe regex error description and does not scan files

#### Scenario: Search output is bounded
- **WHEN** matching lines or scanned bytes exceed configured limits
- **THEN** the tool returns partial deterministic results with `truncated: true` and indicates whether the match, file-size, or time limit caused truncation

### Requirement: Non-interactive bounded Bash execution

The `bash` tool SHALL execute one non-interactive command through an explicitly configured Bash-compatible executable with the working directory inside the approved workspace. It MUST use a closed stdin, sanitized environment, finite timeout, bounded stdout/stderr, and process-tree cleanup on timeout or output-limit breach.

#### Scenario: Command succeeds
- **WHEN** Bash is configured and the command exits with code zero before limits are reached
- **THEN** the tool returns `status: "ok"`, exit code zero, bounded stdout/stderr, duration, cwd, and output-size metadata

#### Scenario: Command exits nonzero
- **WHEN** Bash is configured and the command exits with a nonzero code
- **THEN** the tool returns `status: "command_failed"`, the exit code, bounded stdout/stderr, and an actionable error category without converting the result into a success

#### Scenario: Bash runtime is unavailable
- **WHEN** no approved Bash executable is configured or discoverable
- **THEN** the tool returns `status: "runtime_unavailable"` and does not fall back to another host shell

#### Scenario: Command times out
- **WHEN** the command exceeds the configured timeout
- **THEN** the runtime terminates the process tree and returns `status: "timed_out"` with partial bounded output and timeout metadata

#### Scenario: Output exceeds the configured cap
- **WHEN** stdout or stderr exceeds the configured output limit
- **THEN** the runtime terminates or drains according to the executor policy, returns `status: "output_truncated"`, and marks the affected stream as truncated

#### Scenario: Working directory is outside the workspace
- **WHEN** the caller supplies an absolute or traversal path outside the approved workspace
- **THEN** the tool rejects the call before starting a process and returns a structured permission error

### Requirement: Agent registration and routing

The runtime SHALL expose `bash`, `glob`, and `grep` through the normal agent tool aggregation and wrapper layers. `glob` and `grep` MUST be eligible for bounded read-only workspace evidence routing; `bash` MUST NOT be selected by automatic evidence routing.

#### Scenario: Tools are visible to a built agent
- **WHEN** the agent is built for an approved workspace
- **THEN** the registered tool set contains all three stable tool names with typed schemas and descriptions

#### Scenario: Read-only evidence route uses search tools
- **WHEN** bounded research plans workspace discovery or content search
- **THEN** the coordinator can invoke `glob` and `grep` within workspace budgets and records their source kind as workspace

#### Scenario: Shell is excluded from automatic evidence
- **WHEN** bounded research selects evidence tools
- **THEN** `bash` is absent from the evidence registry and is not invoked as an implicit research step

### Requirement: Policy, audit, and bounded result contracts

All three tools SHALL pass through existing runtime policy, deduplication, and audit handling. Results MUST be JSON-compatible, bounded in size, explicit about status/truncation/errors, and MUST NOT include unredacted environment secrets or unbounded command output.

#### Scenario: Repeated read-only call is deduplicated
- **WHEN** identical `glob` or `grep` arguments are invoked within a run
- **THEN** the wrapper may reuse the prior bounded result and records the deduplication action without changing the tool contract

#### Scenario: Shell invocation is audited
- **WHEN** `bash` starts or rejects a command
- **THEN** the session audit records tool name, command hash, cwd, status/category, duration, and bounded preview metadata without requiring the model result to contain the full command or environment

#### Scenario: Tool input is invalid
- **WHEN** a caller exceeds pattern length, timeout, scope, or other configured validation limits
- **THEN** the tool returns `status: "invalid_input"` or `status: "permission_denied"` with a stable category and does not perform the prohibited operation
