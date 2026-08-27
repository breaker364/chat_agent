## Context

The agent already aggregates LangChain tools in `backend/tools.py`, applies workspace path checks for file tools, wraps tools for policy, deduplication, caching, and audit events, and exposes a bounded read-only evidence registry through `backend/agentic_research/runtime.py`. It has no generic file-pattern or content-search primitive and its existing Python runner is specialized to `.py` files.

The requested capabilities must work across the repository without embedding knowledge of particular companies, products, domains, or directory names. The runtime is deployed on Windows as well as POSIX-like environments, so Bash availability and path semantics must be explicit. Tool results are model-visible and therefore need bounded output, stable schemas, and actionable failures.

## Goals / Non-Goals

**Goals:**

- Provide `bash`, `glob`, and `grep` as first-class agent tools with typed inputs and deterministic, bounded outputs.
- Keep `glob` and `grep` within the same approved workspace/read-root policy used by existing file tools.
- Run Bash non-interactively with an explicit executable, workspace cwd, environment allowlist, timeout, output cap, and process-tree cleanup.
- Make read-only tools available to bounded research routing and keep `bash` out of automatic evidence collection.
- Preserve tool deduplication and audit behavior, including command/regex argument hashes and latency metadata without retaining unbounded output.
- Cover success, no-match, invalid-input, permission, timeout, truncation, and unavailable-runtime cases with focused tests.

**Non-Goals:**

- Interactive terminals, persistent shell sessions, background daemons, pipelines that outlive the call, or arbitrary network/service credentials.
- A general host command runner that can escape the workspace or bypass runtime policy.
- Entity-specific glob presets, grep patterns, command allowlists keyed to named projects, or hardcoded domain mappings.
- Replacing existing `read_file`, `list_directory`, or `run_python_file` behavior.

## Decisions

### Use three dedicated typed tools

Add `BashInput`, `GlobInput`, and `GrepInput` Pydantic models. The tools return JSON strings with stable fields: `status`, normalized input, bounded results, and error metadata. `glob` and `grep` remain separate from Bash so the model can choose a lower-risk, structured operation for discovery and search.

Alternative considered: expose one generic `run_command` tool and teach the model shell idioms. This is simpler to register but makes safe discovery harder to audit, produces less deterministic output, and encourages shell use for operations that need no command execution.

### Constrain paths through shared workspace helpers

`glob` resolves a relative pattern against `_workspace_root()` and rejects absolute patterns, traversal outside the root, and configured internal index paths during an active run. It uses `pathlib.Path.glob` (or an equivalent separator-normalized matcher), sorts results by normalized relative path, and returns workspace-relative paths only. A configurable maximum match count and result byte/character budget produces an explicit `truncated` flag.

`grep` resolves a root directory or file through `_ensure_readable`, recursively enumerates only readable regular text files, skips binary/unreadable files with bounded diagnostics, and applies a caller-provided regular expression using line-oriented matching. It returns path, 1-based line number, and bounded line text; invalid regexes are structured errors. Symlink targets must still satisfy the resolved read-root check.

Alternative considered: shell out to platform `glob`/`grep`. Platform implementations differ, shell quoting is fragile, and command output is less structured. Python-side matching preserves the existing path policy and behaves consistently on Windows.

### Execute Bash as one bounded subprocess

`bash` accepts a command string, optional working directory, timeout, and output limits. The implementation resolves the configured Bash executable (for example from runtime configuration or an explicit executable input), rejects empty or interactive requests, resolves cwd inside the workspace, and starts `subprocess.Popen` with stdin connected to `DEVNULL`, `shell=False`, text pipes, and a sanitized environment containing only the configured safe variables.

The process receives a new process group/session. On timeout or output-limit breach, the runtime terminates the process tree, waits briefly, and returns a structured `timed_out` or `output_truncated` status with partial stdout/stderr. Exit code, duration, command hash, cwd, and output sizes are returned; the raw command is included only in the tool transcript/audit path subject to existing redaction rules. No interactive prompt, TTY, host-home expansion, or automatic network credential injection is provided.

The tool is disabled with an explicit `runtime_unavailable` result when no configured Bash executable exists. There is no silent fallback to `cmd.exe`, PowerShell, or an unconstrained shell.

Alternative considered: use an external container or remote sandbox for every invocation. That gives stronger isolation but adds a deployment dependency and is not present in the current runtime. The design keeps a clear subprocess boundary so a future sandbox backend can replace the executor without changing the tool contract.

### Integrate with policy, research, and audit layers

Add `glob` and `grep` to `_EVIDENCE_TOOL_NAMES`, workspace source classification, and `EVIDENCE_TOOL_NAMES` in the research runtime. Their calls are cacheable using normalized inputs. Add `bash` to neither evidence set nor automatic source budgets; its calls are side-effect-capable from the policy layer's perspective, are never conversation-cached, and are recorded with command hash, cwd, exit/timeout status, and bounded previews.

Register all three in `_FILE_TOOLS` or a dedicated local-tools collection before the existing wrapper is applied. Update generic prompt/policy text to explain when to prefer glob/grep, that Bash is for project-native commands only, and that tool output is evidence rather than instruction. Do not add entity-specific examples or command mappings.

### Configuration and compatibility

Add a shell-tools section to the existing runtime configuration loader with defaults for enablement, Bash executable, timeout, maximum output characters, maximum glob matches, maximum grep matches, maximum file size, and environment variable names. Defaults must be conservative and validated at load time. Existing deployments without the section keep `glob` and `grep` available and return a structured `runtime_unavailable` for `bash` until an executable is configured/discovered according to the platform adapter.

Keep the public function names and LangChain tool names stable (`bash`, `glob`, `grep`). Existing tools and session transcript schemas remain backward compatible; new fields are additive.

## Risks / Trade-offs

- **[Shell escape or destructive command]** -> Keep cwd and executable constrained, disable interactive input, sanitize environment, enforce process/resource limits, record audits, and require explicit user confirmation through existing policy for destructive operations.
- **[Bash implementation differences across platforms]** -> Resolve an explicit configured executable, report its path/version metadata, test with a fake executor, and fail clearly when unavailable.
- **[Large repository scan cost]** -> Bound recursion, per-file size, match count, output characters, and elapsed time; return truncation metadata instead of unbounded results.
- **[Regex or glob denial of service]** -> Bound pattern length, reject invalid regexes, use per-call deadlines, and avoid following symlinks outside approved roots.
- **[Sensitive output exposure]** -> Reuse existing path/read policies, redact configured environment values, avoid returning full environment dumps, and cap stdout/stderr previews.
- **[Research routing overuses shell]** -> Keep Bash out of the evidence registry; only `glob`/`grep` are eligible read-only workspace evidence tools.

## Migration Plan

1. Add configuration parsing and pure helper/contract tests with fake Bash executors.
2. Implement `glob` and `grep`, then register them with policy, evidence routing, and tool aggregation.
3. Implement the bounded Bash executor and platform executable resolver; keep it disabled when unavailable.
4. Add prompt/policy guidance, audit normalization, and integration tests for tool exposure and transcript events.
5. Enable Bash in environments that provide an approved executable, then run focused and existing backend tests.
6. Roll back by disabling `bash` in runtime configuration or removing the new tools from aggregation; existing file and Python tools remain usable.

## Open Questions

- Whether production deployments will standardize on a bundled Bash runtime or provide an administrator-configured executable path. The contract supports either choice without changing the agent-facing API.
- Whether a later change should add CPU/memory limits through an OS/container-specific executor; the first implementation only promises timeout, output, process-tree, and path controls.
