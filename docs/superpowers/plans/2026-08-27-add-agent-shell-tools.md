# Agent Shell Tools Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:test-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add bounded `glob`, `grep`, and non-interactive `bash` tools to the agent runtime without weakening workspace, policy, audit, or research-safety boundaries.

**Architecture:** Keep the existing aggregation and deduplication wrapper in `backend/tools.py`, while placing the new schemas, path-scoped scanners, bounded subprocess executor, and JSON result helpers in a focused `backend/workspace_shell_tools.py` module. The module will lazily use the existing workspace policy helpers so the tools share the current root, external-read, protected-index, and managed-memory rules without creating a circular import. Research routing will adapt `glob` and `grep` as workspace evidence and will never register `bash` as evidence.

**Tech Stack:** Python 3.12, Pydantic v2, LangChain `StructuredTool`, `pathlib`, `re`, `subprocess`, `pytest`/`unittest`, existing session audit and runtime-context helpers.

## Global Constraints

- All model-visible results are JSON-compatible and bounded by configured character, match, file-size, and timeout limits.
- Relative paths are normalized to workspace-relative POSIX-style paths; absolute and traversal scopes are rejected before traversal or process startup.
- Protected internal indexes remain inaccessible during active agent runs except through the knowledge tool.
- Bash uses only an explicitly configured/discoverable Bash executable, `shell=False`, closed stdin, sanitized environment, workspace cwd, finite timeout, and process-group cleanup.
- Bash has no fallback to PowerShell, `cmd.exe`, or an unconstrained host shell.
- `glob` and `grep` are cacheable read-only evidence tools; `bash` is audited and never conversation-cached or evidence-routed.
- Do not add entity-specific names, domains, presets, command mappings, or emoji.
- Preserve all unrelated existing worktree changes and do not commit them.

---

### Task 1: Contracts, configuration, and bounded-result primitives

**Files:**
- Create: `backend/workspace_shell_tools.py`
- Modify: `backend/config.py`
- Modify: `runtime_config.json`
- Modify: `config.example.json`
- Test: `backend/tests/test_shell_tools.py`

**Interfaces:**
- `BashInput(command: str, working_directory: str = "", timeout_seconds: int | None = None, max_output_chars: int | None = None, executable: str = "")`.
- `GlobInput(pattern: str, scope: str = ".", max_matches: int | None = None)`.
- `GrepInput(pattern: str, scope: str = ".", max_matches: int | None = None, max_file_bytes: int | None = None, timeout_seconds: float | None = None)`.
- `load_shell_tools_config(raw: Mapping[str, Any] | None = None) -> dict[str, Any]` with validated conservative defaults.
- `bash`, `glob`, and `grep` are LangChain tools returning JSON strings.
- Result statuses are `ok`, `invalid_input`, `permission_denied`, `runtime_unavailable`, `timed_out`, `command_failed`, and `output_truncated`.

- [ ] **Step 1: Write the failing contract tests.** Assert default configuration, bounded numeric validation, maximum command/pattern length rejection, stable result status names, and typed tool schemas.
- [ ] **Step 2: Run the focused tests and verify the expected RED state.**

Run: `& '.venv_py312/Scripts/python.exe' -m pytest backend/tests/test_shell_tools.py -q`

Expected: failures because the new configuration loader, schemas, and tools do not yet exist.

- [ ] **Step 3: Implement the minimal schemas, loader, and JSON helper.** Validate booleans, positive integer/float limits, executable/environment configuration, and reject unsupported credential-like configuration keys. Add the shell-tools section to both configuration examples without credentials.
- [ ] **Step 4: Run the focused tests and verify GREEN.**

Run: `& '.venv_py312/Scripts/python.exe' -m pytest backend/tests/test_shell_tools.py -q`

Expected: all contract tests pass.

- [ ] **Step 5: Refactor only after GREEN.** Keep configuration parsing independent from filesystem and subprocess execution, and preserve bounded serialization in one helper.

### Task 2: Shared scope policy and `glob`

**Files:**
- Modify: `backend/workspace_shell_tools.py`
- Test: `backend/tests/test_shell_tools.py`

**Interfaces:**
- `_resolve_workspace_scope(scope: str, *, allow_file: bool = True) -> Path` rejects empty/absolute/traversal scopes, outside roots, protected indexes, and symlink targets outside approved roots.
- `_relative_workspace_path(path: Path) -> str` never returns an absolute path.
- `glob(pattern: str, scope: str = ".", max_matches: int | None = None) -> str` returns `{status, pattern, scope, matches, returned_count, limit, truncated}`.

- [ ] **Step 1: Add failing path-policy tests.** Create a temporary workspace fixture and assert absolute scopes, traversal, protected index scopes, and external symlink targets return `permission_denied` without visiting outside paths.
- [ ] **Step 2: Add failing `glob` behavior tests.** Cover deterministic normalized ordering, files and directories, empty matches, separator normalization, configured match limits, and explicit truncation metadata.
- [ ] **Step 3: Run the focused path/glob tests and verify RED.**

Run: `& '.venv_py312/Scripts/python.exe' -m pytest backend/tests/test_shell_tools.py -k 'scope or glob' -q`

Expected: failures caused by the missing resolver and tool implementation.

- [ ] **Step 4: Implement minimal scope resolution and glob traversal.** Reject unsafe patterns before `Path.glob`, use the existing `backend.tools._ensure_readable`/workspace helpers at runtime, avoid following symlinks whose resolved target is outside the approved root, sort by normalized relative path, and stop at the configured bound.
- [ ] **Step 5: Run the focused tests and verify GREEN.**

Run: `& '.venv_py312/Scripts/python.exe' -m pytest backend/tests/test_shell_tools.py -k 'scope or glob' -q`

Expected: all path and glob tests pass.

### Task 3: Bounded `grep`

**Files:**
- Modify: `backend/workspace_shell_tools.py`
- Test: `backend/tests/test_shell_tools.py`

**Interfaces:**
- `grep(pattern: str, scope: str = ".", max_matches: int | None = None, max_file_bytes: int | None = None, timeout_seconds: float | None = None) -> str` returns `{status, pattern, scope, matches, files_scanned, bytes_scanned, skipped, truncated, truncation_reasons}`.
- Each match contains only `path`, `line_number`, and bounded `line` text.

- [ ] **Step 1: Add failing search tests.** Cover line numbers, deterministic path/line ordering, invalid regex without scanning, no matches, binary files, oversized files, unreadable files, protected indexes, match limits, output limits, and deadline truncation.
- [ ] **Step 2: Run the focused tests and verify RED.**

Run: `& '.venv_py312/Scripts/python.exe' -m pytest backend/tests/test_shell_tools.py -k 'grep' -q`

Expected: failures because the search implementation is absent.

- [ ] **Step 3: Implement minimal bounded line-oriented scanning.** Compile the regex before resolving files, enumerate approved regular files deterministically, detect binary content, skip files that cannot be read, enforce per-file size and aggregate limits, and preserve bounded diagnostics instead of aborting valid scans.
- [ ] **Step 4: Run the focused tests and verify GREEN.**

Run: `& '.venv_py312/Scripts/python.exe' -m pytest backend/tests/test_shell_tools.py -k 'grep' -q`

Expected: all grep tests pass.

### Task 4: Configured non-interactive Bash executor

**Files:**
- Modify: `backend/workspace_shell_tools.py`
- Test: `backend/tests/test_shell_tools.py`

**Interfaces:**
- `_resolve_bash_executable(preferred: str = "") -> str | None` returns only an approved configured/discoverable Bash executable.
- `_execute_bash(command: str, *, cwd: Path, executable: str, timeout_seconds: int, max_output_chars: int) -> dict[str, Any]` returns bounded status, exit, timing, cwd, command hash, stream sizes, partial streams, and truncation/timeout metadata.
- `bash(...)` returns a bounded JSON result and never returns raw environment data.

- [ ] **Step 1: Add failing fake-boundary tests.** Cover success, nonzero exit, missing executable, invalid cwd, closed stdin, sanitized environment, timeout/process cleanup, stdout/stderr caps, partial-output preservation, and command hash metadata.
- [ ] **Step 2: Run the Bash-focused tests and verify RED.**

Run: `& '.venv_py312/Scripts/python.exe' -m pytest backend/tests/test_shell_tools.py -k 'bash' -q`

Expected: failures because the executor is absent.

- [ ] **Step 3: Implement the minimal executor.** Start `[executable, "-c", command]` with `shell=False`, `stdin=DEVNULL`, an allowlisted environment, a new process group/session, workspace-only cwd, finite `communicate` timeout, bounded stream reads, process-tree termination, and a short post-termination wait. Return `runtime_unavailable` without any shell fallback when resolution fails.
- [ ] **Step 4: Run the Bash-focused tests and verify GREEN.**

Run: `& '.venv_py312/Scripts/python.exe' -m pytest backend/tests/test_shell_tools.py -k 'bash' -q`

Expected: all Bash executor tests pass.

### Task 5: Tool aggregation, policy, deduplication, and audit integration

**Files:**
- Modify: `backend/tools.py`
- Test: `backend/tests/test_shell_tools.py`
- Test: `backend/tests/test_tool_history.py` only when an additive audit assertion is needed

**Interfaces:**
- `_EVIDENCE_TOOL_NAMES` and `_source_kind_for_tool` classify `glob` and `grep` as workspace; `bash` is absent from both evidence classification and cacheable tools.
- `_FILE_TOOLS` includes `bash`, `glob`, and `grep` before `_wrap_tool_with_run_dedupe` is applied.
- Existing wrapper behavior remains unchanged for existing tools.

- [ ] **Step 1: Add failing integration tests.** Assert `get_all_tools()` exposes all three names and exact Pydantic schemas, read-only calls are deduplicated/cacheable, Bash calls are not conversation-cached, policy denials happen before execution, and audit payloads include bounded metadata without environment secrets or unbounded output.
- [ ] **Step 2: Run the integration tests and verify RED.**

Run: `& '.venv_py312/Scripts/python.exe' -m pytest backend/tests/test_shell_tools.py -k 'registration or policy or dedupe or audit' -q`

Expected: failures because registration and classification are not yet wired.

- [ ] **Step 3: Implement the minimal registration and classification changes.** Add tool policies/TTL entries only for read-only search tools, preserve `bash` as audited/non-cacheable, and make audit metadata use command hash/cwd/status without expanding model-visible output.
- [ ] **Step 4: Run the integration tests and verify GREEN.**

Run: `& '.venv_py312/Scripts/python.exe' -m pytest backend/tests/test_shell_tools.py -k 'registration or policy or dedupe or audit' -q`

Expected: all integration tests pass.

### Task 6: Research routing and backward compatibility

**Files:**
- Modify: `backend/agentic_research/runtime.py`
- Modify: `backend/agentic_research/adapters.py`
- Modify: `backend/agent.py` only if tool-step classification needs an additive generic branch
- Test: `backend/tests/test_agentic_research_runtime.py`
- Test: `backend/tests/test_agent_owned_research_routing.py`
- Test: `backend/tests/test_agentic_research_adapters.py`

**Interfaces:**
- `EVIDENCE_TOOL_NAMES` includes `glob` and `grep` but not `bash`.
- `WorkspaceEvidenceAdapter` accepts optional `glob` and `grep` callbacks while retaining existing `read_file`, `list_directory`, and `get_file_info` behavior.
- Workspace evidence observations contain bounded citations/excerpts and never expose raw unbounded payloads.

- [ ] **Step 1: Add failing routing/adapter tests.** Assert `glob` and `grep` are bound as workspace evidence, Bash is excluded, source budgets apply, and existing file/Python tools remain visible to ReAct runtime.
- [ ] **Step 2: Run the focused research tests and verify RED.**

Run: `& '.venv_py312/Scripts/python.exe' -m pytest backend/tests/test_agentic_research_runtime.py backend/tests/test_agent_owned_research_routing.py backend/tests/test_agentic_research_adapters.py -q`

Expected: failures for the new evidence callbacks and names.

- [ ] **Step 3: Implement the minimal adapter/runtime integration.** Prefer structured glob/grep results for discovery/search evidence, convert bounded matches into `EvidenceObservation`, and keep the registry restricted to read-only source kinds.
- [ ] **Step 4: Run the focused research tests and verify GREEN.**

Run: `& '.venv_py312/Scripts/python.exe' -m pytest backend/tests/test_agentic_research_runtime.py backend/tests/test_agent_owned_research_routing.py backend/tests/test_agentic_research_adapters.py -q`

Expected: all routing and compatibility tests pass.

### Task 7: Prompt, documentation, and operational diagnostics

**Files:**
- Modify: `backend/prompts/system_prompt.md`
- Modify: `backend/prompts/agent_policy.md`
- Modify: `README.md`
- Modify: `runtime_config.json`
- Modify: `config.example.json`
- Test: `backend/tests/test_shell_tools.py`
- Test: `backend/tests/test_rag_tools.py` if prompt assertions need extension

- [ ] **Step 1: Add failing guidance/configuration tests.** Assert generic guidance prefers `glob` for discovery and `grep` for line-level search, reserves Bash for project-native non-interactive commands, documents unavailable-runtime behavior, and contains no entity-specific mapping.
- [ ] **Step 2: Run the prompt/configuration tests and verify RED.**

Run: `& '.venv_py312/Scripts/python.exe' -m pytest backend/tests/test_shell_tools.py backend/tests/test_rag_tools.py -k 'prompt or config or diagnostic' -q`

Expected: failures for missing guidance and diagnostics.

- [ ] **Step 3: Implement generic documentation and startup-safe diagnostics.** Report only enabled state and executable policy (not environment values or secrets), document limits/statuses/configuration, and avoid project-specific examples.
- [ ] **Step 4: Run the prompt/configuration tests and verify GREEN.**

Run: `& '.venv_py312/Scripts/python.exe' -m pytest backend/tests/test_shell_tools.py backend/tests/test_rag_tools.py -k 'prompt or config or diagnostic' -q`

Expected: all guidance/configuration tests pass.

### Task 8: Full verification and OpenSpec synchronization

**Files:**
- Modify: `openspec/changes/add-agent-shell-tools/tasks.md`
- No production code changes unless a failing verification test identifies a concrete defect.

- [ ] **Step 1: Run focused shell-tool tests.**

Run: `& '.venv_py312/Scripts/python.exe' -m pytest backend/tests/test_shell_tools.py -q`

Expected: all shell-tool contract, safety, executor, registration, audit, and routing tests pass.

- [ ] **Step 2: Run existing backend regression tests.**

Run: `& '.venv_py312/Scripts/python.exe' -m pytest backend/tests/test_rag_tools.py backend/tests/test_agentic_research_*.py backend/tests/test_external_file_access.py backend/tests/test_tool_history.py backend/tests/test_skill_policy.py -q`

Expected: zero failures; any sandbox-only temp-directory issue is rerun with the approved elevated test command and recorded separately.

- [ ] **Step 3: Run compilation and offline smoke checks.**

Run: `& '.venv_py312/Scripts/python.exe' -m compileall -q backend`; then invoke `glob` and `grep` against a temporary local workspace fixture. Invoke `bash` only if a configured approved executable is available; otherwise verify the structured `runtime_unavailable` result.

Expected: compilation succeeds, offline search returns bounded relative results, and Bash either executes through the approved executable or reports unavailability without fallback.

- [ ] **Step 4: Run strict OpenSpec validation and static safety review.**

Run: `openspec validate --change "add-agent-shell-tools" --strict`; then inspect the diff for entity/domain matching, raw environment values, absolute-path results, unbounded output, and unapproved shell fallback.

Expected: strict validation succeeds and the diff contains no prohibited patterns.

- [ ] **Step 5: Mark only verified OpenSpec tasks complete.** Update `tasks.md` checkboxes after the corresponding tests and verification evidence are available; leave any blocked item unchecked and report the exact blocker.
