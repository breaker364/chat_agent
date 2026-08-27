## 1. Contracts and Configuration

- [x] 1.1 Define typed `BashInput`, `GlobInput`, and `GrepInput` schemas with pattern/command length, scope, timeout, and output-limit validation.
- [x] 1.2 Add shell-tools runtime configuration loading with conservative defaults, explicit enablement, Bash executable resolution settings, and validated resource limits.
- [x] 1.3 Define shared bounded JSON result helpers and stable status/error categories for success, no-match, invalid input, permission denial, runtime unavailable, timeout, command failure, and truncation.

## 2. TDD Red Tests

- [x] 2.1 Add path-policy tests proving `glob` and `grep` reject absolute/traversal scopes, protected internal indexes, and symlink targets outside approved roots.
- [x] 2.2 Add `glob` tests for stable ordering, empty matches, directory/file patterns, maximum-match truncation, and normalized relative paths.
- [x] 2.3 Add `grep` tests for line numbers, deterministic ordering, invalid regular expressions, binary/oversized file handling, no matches, and bounded result truncation.
- [x] 2.4 Add Bash executor tests using a fake executable/process boundary for success, nonzero exit, unavailable executable, invalid cwd, timeout/process cleanup, and stdout/stderr caps.
- [x] 2.5 Add integration tests proving the three tool names, Pydantic schemas, wrapper behavior, policy denials, deduplication, and audit events are exposed by `get_all_tools()`.
- [x] 2.6 Add research-routing tests proving `glob` and `grep` are workspace evidence tools while `bash` is excluded from automatic evidence collection.

## 3. Read-only Discovery and Search

- [x] 3.1 Implement shared pattern/scope resolution that reuses existing workspace/read-root checks and never returns absolute paths.
- [x] 3.2 Implement `glob` with separator normalization, deterministic sorting, match/file/result limits, protected-root checks, and explicit truncation metadata.
- [x] 3.3 Implement `grep` with bounded regex validation, line-oriented text scanning, binary detection, per-file size limits, deterministic ordering, and explicit scan diagnostics.
- [x] 3.4 Ensure both tools skip or report unreadable files without aborting an otherwise valid bounded scan, and never follow symlinks beyond approved roots.

## 4. Bounded Bash Execution

- [x] 4.1 Implement configurable Bash executable discovery with no fallback to PowerShell, `cmd.exe`, or an unconstrained host shell.
- [x] 4.2 Implement non-interactive subprocess startup with workspace-only cwd, `shell=False`, closed stdin, sanitized environment, process-group/session creation, and no inherited credentials.
- [x] 4.3 Implement timeout enforcement, output-size enforcement, process-tree termination, short post-termination wait, and partial-output preservation.
- [x] 4.4 Return bounded structured metadata for command hash, cwd, exit code, duration, stream sizes, timeout/truncation state, and actionable error categories.
- [x] 4.5 Add redaction and audit preparation so environment values and unbounded command output are never emitted in model-visible results.

## 5. Runtime Integration

- [x] 5.1 Register `bash`, `glob`, and `grep` in the local tool collection before the existing deduplication/policy wrapper is applied.
- [x] 5.2 Extend tool cache/policy/source classification for cacheable read-only glob/grep calls and non-cacheable audited Bash calls.
- [x] 5.3 Extend bounded research adapters and source budgets to use `glob`/`grep` as workspace evidence, while keeping Bash out of the evidence registry.
- [x] 5.4 Preserve backward-compatible existing file/Python tools and ensure managed-memory and protected-index rules continue to apply.

## 6. Prompt, Documentation, and Operational Controls

- [x] 6.1 Update generic agent policy/system guidance to prefer `glob` for discovery and `grep` for line-level search, and reserve Bash for project-native non-interactive commands.
- [x] 6.2 Document configuration, platform prerequisites, limits, result statuses, audit fields, and the explicit Bash-unavailable behavior without entity-specific examples.
- [x] 6.3 Add deployment checks or startup diagnostics that show whether Bash is enabled and which configured executable policy is active, without printing secrets.

## 7. Green Tests and Verification

- [x] 7.1 Run the focused shell-tool tests and fix implementation until all contract, safety, and integration tests pass.
- [x] 7.2 Run existing backend tool, agent, research-routing, session-audit, and policy tests to catch registration or transcript regressions.
- [x] 7.3 Run Python compilation/lint checks and an offline workspace smoke test for glob/grep; run a Bash smoke test only when an approved executable is available.
- [x] 7.4 Run strict OpenSpec validation and review that no hardcoded entity/domain matching, raw environment secrets, or unbounded outputs were introduced.
