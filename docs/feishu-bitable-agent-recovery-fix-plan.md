# Feishu Bitable Agent Recovery Fix Plan

Date: 2026-07-13

## Scope

This document defines the repair plan for the repeated failure pattern observed
in session:

```text
sessionss/session-1783906015429
```

The concrete task was to write extracted data into a Feishu Bitable. The Agent
called many tools, partially succeeded, then failed to converge and repeatedly
retried invalid write paths.

This plan focuses on:

- Feishu Bitable batch JSON argument robustness.
- Agent recovery after tool errors.
- Correct recognition of successful writes.
- Task-plan convergence.
- Preventing repeated high-token retry loops.

It does not cover unrelated UI, login, search, or multimodal features except
where they interact with this failure pattern.

## Observed Failure

### User-visible failure

The UI showed a final failure summary containing:

```text
JSONDecodeError: Expecting property name enclosed in double quotes
```

The final message also contained contradictory status signals:

```text
执行摘要: failed
Persistent Progress Summary: completed
```

### Tool usage explosion

One run contained high repeated tool counts:

```text
update_task_plan x14
use_skill x46
read_file x4
read_skill_detail x2
run_python_file x2
```

Another follow-up run contained:

```text
update_task_plan x13
use_skill x10
run_python_file x10
write_file x5
read_file x4
list_directory x3
```

This is far above what the task required.

### Important successful signal that was ignored

The session contains a successful write:

```json
{
  "success": true,
  "obj_token": "Pm94bttcYaX6bxstrqrcvqRQnFc",
  "table_id": "tblQ5A2yQAbwrqrQ",
  "records_written": 23,
  "rev": 15
}
```

Later table inspection showed:

```text
recordsNum: 51
fieldCount: 4
```

Despite this, the Agent continued retrying write operations and ended with a
failure summary.

## Root Cause Analysis

### Root Cause 1: JSON batch payload was not passed as one CLI argument

The failing command looked like:

```text
lark bitable add-records-batch <url> <tableId> [{"测点":1,"Flush":1.027,"gap":0.005}, ...]
```

`add-records-batch` expects:

```text
lark bitable add-records-batch <url> <tableId> <records_json>
```

where `records_json` must be a single JSON string argument.

Because the JSON array was not wrapped in quotes, command parsing split or
distorted the JSON, and this line failed:

```python
records = json.loads(raw_json)
```

Current location:

```text
skills/feishu-personal/lark_tools/cli.py
```

Failure:

```text
JSONDecodeError: Expecting property name enclosed in double quotes
```

### Root Cause 2: Natural-language batch routing rebuilds unsafe CLI strings

`skill_runner.py` has a natural-language route that extracts a JSON array and
returns a CLI string:

```python
return f"lark bitable add-records-batch {target} {table_id} {records_json}"
```

This is unsafe because JSON is inserted into a command string without shell-safe
quoting or direct function invocation.

Current location:

```text
skills/feishu-personal/skill_runner.py
```

### Root Cause 3: Batch write command requires field IDs but Agent first used field names

The first failed JSON used field names:

```json
{"测点": 1, "Flush": 1.027, "gap": 0.005}
```

`cmd_bitable_add_records_batch` currently documents that it does no schema
lookup and treats all keys as raw field IDs:

```text
Each record dict maps field ID -> value string.
No schema lookup is performed.
```

This means batch writes should use:

```json
{
  "fld7ZhMnHHhrS": 1,
  "fldNVzlX2ExvL": 1.027,
  "fldOQoqZj3Lxh": 0.005
}
```

The Agent eventually did this correctly, but only after many retries.

### Root Cause 4: Successful write did not terminate the task

After `records_written: 23`, the task should have moved to verification only:

```text
write succeeded -> verify records -> final response
```

Instead, the Agent kept old plan items in progress:

```json
{
  "task_id": "retry_write_operation",
  "status": "in_progress"
}
```

and:

```json
{
  "task_id": "write_to_feishu",
  "status": "in_progress"
}
```

while also having:

```json
{
  "task_id": "write_records_to_bitable",
  "status": "completed",
  "details": "成功写入23条记录到飞书多维表格"
}
```

This created a contradictory plan state.

### Root Cause 5: Finalization marked failure text as completed memory

The final answer contained a traceback and an Agent failure message, but the
persistent summary still recorded:

```text
Status: completed
```

This polluted future session memory and made recovery harder.

### Root Cause 6: Deduplication only catches exact arguments

The runtime dedupe blocked exact repeated calls, but semantically equivalent
write attempts with different wording were treated as new calls:

```text
lark bitable add-records-batch ...
```

versus:

```text
使用批量写入命令，将以下JSON数据写入...
```

This allowed repeated retries of the same task.

## Goals

1. Batch Bitable writes must be robust for large JSON payloads.
2. The Agent must stop after a successful write and required verification.
3. Natural-language skill routing must not regenerate unsafe CLI strings.
4. Failed tool outputs must be classified and used to choose a corrected next
   action.
5. Final session memory must not mark traceback/failure output as completed.
6. The system should prevent semantic retry loops that waste time and tokens.

## Non-Goals

- Replacing all Feishu Bitable write logic.
- Replacing LangGraph or the whole Agent runtime.
- Solving every possible task-planning issue.
- Adding UI features.
- Changing authentication behavior unless needed by tests.

## Proposed Fixes

## Fix 1: Harden `add-records-batch` JSON parsing

### File

```text
skills/feishu-personal/lark_tools/cli.py
```

### Current behavior

```python
raw_json = filtered_args[4] if len(filtered_args) > 4 else '[]'
records = json.loads(raw_json)
```

### Required behavior

Use all remaining arguments as the JSON payload:

```python
raw_json = " ".join(filtered_args[4:]) if len(filtered_args) > 4 else "[]"
raw_json = raw_json.strip()
if len(raw_json) >= 2 and raw_json[0] == raw_json[-1] and raw_json[0] in ("'", '"'):
    raw_json = raw_json[1:-1]
records = json.loads(raw_json)
```

### Error message

Replace the generic error:

```text
Invalid JSON for records: ...
```

with:

```text
Invalid JSON for records. Pass a JSON array of objects as one argument, or use --json-file.
Example:
lark bitable add-records-batch <url> <tableId> '[{"fldXXX":"value"}]'
```

### Acceptance criteria

- Unquoted JSON split across multiple argv segments can be reassembled when
  possible.
- Quoted JSON still works.
- Invalid JSON returns a clear corrective message.
- Existing successful quoted batch writes continue to pass.

## Fix 2: Add `--json-file` support for batch writes

### File

```text
skills/feishu-personal/lark_tools/cli.py
```

### Command

```text
lark bitable add-records-batch <url> <tableId> --json-file tmp/records.json
```

### Behavior

- If `--json-file` is present, read JSON from that file.
- Resolve relative paths against current working directory.
- Reject missing files.
- Validate that the parsed value is a list of objects.

### Why this matters

Large JSON arrays should not be passed through a natural-language command
string. File-based transfer avoids escaping issues entirely.

### Acceptance criteria

- `--json-file tmp/records.json` writes records successfully.
- Invalid file path returns a clear error.
- Invalid JSON file returns a clear error.

## Fix 3: Make natural-language batch route call Python function directly

### File

```text
skills/feishu-personal/skill_runner.py
```

### Current behavior

Natural-language batch route returns:

```python
f"lark bitable add-records-batch {target} {table_id} {records_json}"
```

### Required behavior

Do not construct a CLI string for JSON batch payloads.

Instead:

1. Extract target URL/token.
2. Extract table ID.
3. Extract JSON array.
4. Parse JSON using `json.loads`.
5. Call:

```python
cmd_bitable_add_records_batch(cookies, target, table_id, records)
```

or create a dedicated helper:

```python
_execute_bitable_add_records_batch(skill_root, request_text)
```

### Acceptance criteria

- Natural-language batch requests no longer pass JSON through `shlex`.
- The request used in this session succeeds or returns a clear field-ID error.
- Explicit `lark ...` passthrough behavior remains available.

## Fix 4: Support field-name to field-ID mapping for batch writes

### File

```text
skills/feishu-personal/lark_tools/commands/bitable_write.py
```

### Current behavior

`cmd_bitable_add_records_batch` treats every key as a field ID and writes text
values.

### Proposed behavior

Add an optional schema lookup path:

```python
def cmd_bitable_add_records_batch(..., resolve_field_names: bool = True):
```

If record keys do not start with `fld`, load field map and map field names to
field IDs.

Rules:

- Exact field ID wins.
- Exact field name maps to field ID.
- Unknown key raises a clear error listing known fields.
- Preserve field-type encoding where possible.

### Caution

This should not reintroduce the `tablesv3` CSRF issue. Prefer a resilient
schema source:

1. `fetch_bitable_schema` if working.
2. fallback `clientvars` table payload if `tablesv3` fails.

### Acceptance criteria

- Batch write with field IDs works.
- Batch write with field names works.
- Unknown field name returns actionable error.

## Fix 5: Add write-success terminal condition

### Files

```text
backend/agent.py
backend/main.py
backend/session_store.py
```

### Required behavior

When a tool result contains:

```json
{
  "success": true,
  "records_written": N
}
```

or a `content_preview` with that object, mark the write step completed.

Next allowed action:

```text
verify output
```

Not allowed:

```text
retry same write
reload skill detail
regenerate same JSON
```

### Implementation options

Add a structured success detector:

```python
def detect_bitable_write_success(tool_name: str, output: str) -> dict | None:
    ...
```

If detected:

- add `records_written` to run state;
- mark write task completed;
- add semantic dedupe key:

```text
bitable_write:<obj_token>:<table_id>:<records_hash>
```

### Acceptance criteria

- After `records_written: 23`, the Agent proceeds only to verification/final.
- No further `add-records-batch` call is made for the same records hash.

## Fix 6: Add semantic dedupe for Bitable writes

### File

```text
backend/tools.py
```

### Current behavior

Dedupe key is based on exact tool name and exact arguments.

### Proposed behavior

For `use_skill` requests that contain Bitable write operations:

- Parse target token/table ID if possible.
- Extract operation type.
- Extract JSON or file content hash if available.
- Normalize into a semantic key:

```text
feishu_bitable:add-records-batch:<token>:<table_id>:<records_hash>
```

If a previous successful call with the same semantic key exists in the current
run or session, do not execute again. Return a structured "already succeeded"
result.

### Acceptance criteria

- Reworded natural-language request for the same batch does not re-run write.
- Exact duplicate still uses existing dedupe.
- Different table or different records still executes.

## Fix 7: Repair task-plan convergence

### Files

```text
backend/session_store.py
backend/tools.py
```

### Problem

Plan contained overlapping tasks:

```text
write_records_to_bitable completed
retry_write_operation in_progress
write_to_feishu in_progress
```

### Proposed behavior

Add completion propagation for semantically equivalent write tasks.

When a tool result confirms write success:

- mark matching active write/retry tasks completed;
- preserve audit history;
- record `obsolete_by` or `completed_by` detail.

Example:

```json
{
  "task_id": "retry_write_operation",
  "status": "completed",
  "details": "Superseded by successful write_records_to_bitable: 23 records written"
}
```

### Acceptance criteria

- No completed write task coexists with in-progress retry/write task for same
  target.
- Plan warnings do not block completion propagation.

## Fix 8: Final status must reflect actual failure content

### Files

```text
backend/main.py
backend/agent.py
```

### Current issue

Failure text can still get a persistent summary with:

```text
Status: completed
```

### Proposed detector

Before finalizing as completed, check final text and tool errors for:

```text
Agent execution failed
Traceback
RuntimeError
JSONDecodeError
SystemExit
工具调用失败
```

If found and no later success/verification overrides it:

- status = `failed`
- do not append "completed" persistent memory
- preserve failure category and recommended next action.

### Acceptance criteria

- A final answer beginning with traceback cannot be stored as completed.
- UI status and persistent summary agree.

## Fix 9: Structured tool error classification

### Files

```text
backend/agent.py
backend/tools.py
```

### Add categories

For this failure class:

```json
{
  "category": "invalid_bitable_batch_json_argument",
  "recoverable": true,
  "recommended_action": "quote_json_or_use_json_file",
  "avoid": "retry_same_command"
}
```

### Detection rules

Match:

```text
Invalid JSON for records
JSONDecodeError
Expecting property name enclosed in double quotes
add-records-batch
```

### Acceptance criteria

- Repair pass sees structured category.
- Repair pass uses corrected command path or JSON file path.
- Repair pass does not reread unrelated skill details.

## Fix 10: Limit repair loops by failure signature

### File

```text
backend/agent.py
```

### Proposed behavior

Track recent failure signatures:

```text
tool_name + error_category + target
```

If the same signature appears twice:

- stop retrying same tool route;
- either switch strategy or report blocker.

For batch JSON errors:

Allowed strategy switch:

```text
CLI JSON argument -> JSON file -> direct Python function
```

Not allowed:

```text
CLI JSON argument -> slightly reworded CLI JSON argument -> same failure
```

### Acceptance criteria

- Same JSONDecodeError does not recur more than twice in one run.
- Tool budget is not exhausted by identical semantic failures.

## Implementation Order

### Phase 1: Immediate Bitable robustness

1. Harden `add-records-batch` JSON parsing.
2. Add `--json-file`.
3. Add tests for quoted, unquoted, malformed, and file-based JSON.

### Phase 2: Avoid unsafe natural-language routing

4. Change natural-language batch route to direct Python invocation.
5. Add tests using the exact failed request format from this session.

### Phase 3: Field mapping

6. Add optional field-name to field-ID mapping for batch records.
7. Test both field names and field IDs.

### Phase 4: Agent convergence

8. Add write-success detector.
9. Add semantic dedupe for successful Bitable writes.
10. Add plan completion propagation for equivalent write tasks.

### Phase 5: Finalization correctness

11. Add failure-content detector before marking final status completed.
12. Add structured error categories.
13. Add repair-loop failure signature cap.

## Test Plan

### Unit tests

#### `test_bitable_batch_json_parsing.py`

Cases:

- quoted JSON array:

```text
'[{"fldA":"x"}]'
```

- unquoted JSON array with spaces:

```text
[{"fldA": "x"}, {"fldA": "y"}]
```

- JSON file:

```text
--json-file tmp/records.json
```

- malformed JSON:

```text
[{fldA:"x"}]
```

Expected:

- valid cases parse to list;
- malformed case returns actionable error.

#### `test_bitable_batch_nl_route.py`

Use a natural-language request:

```text
使用批量写入命令，将以下JSON数据写入到飞书多维表格中。URL是 ...，表ID是 ...。JSON数据是：[...]
```

Expected:

- route parses JSON directly;
- does not build unsafe CLI string.

#### `test_bitable_field_mapping.py`

Input:

```json
{"测点": 1, "Flush": 1.027, "gap": 0.005}
```

Expected:

```json
{"fld7ZhMnHHhrS": 1, "fldNVzlX2ExvL": 1.027, "fldOQoqZj3Lxh": 0.005}
```

#### `test_agent_write_success_convergence.py`

Simulate tool result:

```json
{"success": true, "records_written": 23}
```

Expected:

- write step completed;
- retry/write equivalent tasks completed or obsolete;
- no further write tool call allowed for same semantic key.

#### `test_final_status_failure_detection.py`

Input final text:

```text
Agent execution failed: Traceback ...
```

Expected:

- final status = failed;
- persistent summary does not say completed.

### Integration tests

1. Create a temporary Bitable or use a mock `cmd_bitable_add_records_batch`.
2. Run failed session command format.
3. Verify:
   - exactly one write call;
   - verification call after write;
   - final status completed;
   - no extra `read_skill_detail` after write success.

## Acceptance Criteria

The repair is complete when:

1. The exact failed batch JSON format is handled safely.
2. The successful quoted field-ID batch write still works.
3. Natural-language batch writes no longer generate unsafe CLI JSON strings.
4. Field-name batch JSON can be mapped or returns a precise error.
5. A successful `records_written` response stops write retries.
6. Session plan cannot contain completed write and active retry for same target.
7. Failure output is not stored as completed memory.
8. Same semantic write operation cannot execute repeatedly after success.
9. Tool count for this task class stays within expected range:

```text
feishu_login_status <= 1
read_skill_detail <= 1
write_file <= 1 optional
use_skill <= 2
verification read <= 1
update_task_plan <= 3
```

## Rollback Plan

If a change breaks existing flows:

1. Keep explicit `lark ...` passthrough behavior unchanged.
2. Gate new natural-language direct batch route behind a helper function.
3. Allow `--json-file` without removing old `<records_json>` syntax.
4. Keep field-name mapping optional.
5. If semantic dedupe blocks valid writes, restrict it to same run only before
   enabling session-level reuse.

## Notes From Session Analysis

The session proves that Feishu auth and write endpoint were not the root issue:

- Login was valid.
- Table schema could be read.
- Correct batch command wrote 23 records.
- Verification showed the table had records.

The problem was orchestration:

- unsafe JSON transfer through command text;
- failure not converted into a corrected deterministic path;
- success not converted into terminal state.

Fixing the Bitable write path alone will reduce failures. Fixing success
recognition and plan convergence will prevent the large token/time waste seen
in this session.
