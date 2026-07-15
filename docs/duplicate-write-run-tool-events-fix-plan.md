# Duplicate `write_file` / `run_python_file` Events 修复方案

## 1. 背景

用户在 session `sessionss/session-1784092258641` 中观察到两个明显问题：

1. `write_file` 经常连续出现两次相同文件写入，第二次参数里多了 `overwrite: true`。
2. `run_python_file` 经常连续出现两次，第二次参数里多了默认参数，例如 `cli_args: ""`、`timeout_seconds: 60`、`working_directory: ""`、`python_executable: ""`。

这些现象会造成三个负面影响：

- UI 上看起来像 agent 反复执行同一动作，降低可信度。
- `Persistent Progress Summary` 的 tool count 被放大，误导用户判断执行成本。
- 对真实 side-effect 工具，参数默认值不一致可能绕过去重，存在实际重复执行风险。

## 2. 现场证据

对 `sessionss/session-1784092258641/tool_events.jsonl` 统计结果：

```text
total events: 464

event counts:
- tool_call: 173
- tool_result: 172
- tool_runtime_executed: 107
- tool_runtime_deduped: 5
- tool_runtime_reused: 2
- tool_runtime_failed: 1
- turn_result: 4

tool event counts:
- run_python_file: 147
- write_file: 117
- use_skill: 83
- list_directory: 34
- read_file: 28
```

进一步按事件类型拆分：

```text
write_file:
- UI/tool_call: 46
- runtime executed: 25

run_python_file:
- UI/tool_call: 58
- runtime executed: 29
- runtime deduped: 2
```

结论：大多数重复是“事件层重复显示”，不是全部都是真实执行两次。但因为去重 key 不一致，确实存在部分真实重复执行和跨 turn 重跑。

## 3. 典型重复形态

### 3.1 `write_file` 重复形态

同一文件、同一内容，会出现两条 `tool_call`：

```json
{
  "tool_name": "write_file",
  "arguments": {
    "path": "tmp/extract_tables.py",
    "content": "..."
  }
}
```

随后又出现：

```json
{
  "tool_name": "write_file",
  "arguments": {
    "path": "tmp/extract_tables.py",
    "content": "...",
    "overwrite": true
  }
}
```

两条事件对应的业务动作相同，差别只是 `overwrite` 默认参数是否显式出现。

### 3.2 `run_python_file` 重复形态

同一个脚本运行，会出现：

```json
{
  "tool_name": "run_python_file",
  "arguments": {
    "path": "tmp/extract_tables.py"
  }
}
```

随后又出现：

```json
{
  "tool_name": "run_python_file",
  "arguments": {
    "path": "tmp/extract_tables.py",
    "cli_args": "",
    "timeout_seconds": 60,
    "working_directory": "",
    "python_executable": ""
  }
}
```

两条事件语义相同，差别只是 schema/default 参数是否补齐。

## 4. 根因分析

### 4.1 外层 wrapper 与内层原始工具都会产生 LangGraph 事件

当前工具加载流程会对工具做一层 runtime dedupe wrapper：

```python
return [_wrap_tool_with_run_dedupe(item) for item in tools]
```

wrapper 使用 `StructuredTool.from_function(...)` 包装原始工具。

LangGraph 事件流中可能同时出现：

- wrapper 层 `on_tool_start` / `on_tool_end`
- 原始工具层 `on_tool_start` / `on_tool_end`

如果两层事件参数完全一致，当前 `stream_agent_events()` 会尝试通过 `runtime_tool_call_key()` 抑制重复显示。

但实际参数不完全一致，因此抑制失败。

### 4.2 默认参数未归一化

当前去重 key 依赖 `_normalize_tool_payload_for_key(tool_name, payload)`。

该函数已经对部分工具做了特殊归一化，例如：

- `use_skill` 的 bitable batch write
- `update_task_plan`
- `record_primary_result.record_count`

但尚未对 `write_file` / `run_python_file` 做默认参数归一化。

因此下面两组参数被当成不同调用：

```json
{"path": "tmp/a.py", "content": "..."}
```

```json
{"path": "tmp/a.py", "content": "...", "overwrite": true}
```

以及：

```json
{"path": "tmp/a.py"}
```

```json
{
  "path": "tmp/a.py",
  "cli_args": "",
  "timeout_seconds": 60,
  "working_directory": "",
  "python_executable": ""
}
```

### 4.3 路径未统一规范化

以下路径语义相同，但当前可能生成不同 key：

```text
tmp/foo.py
./tmp/foo.py
D:\NoobhekProject\my_agents\chat_agent\tmp\foo.py
```

对 `run_python_file` 来说，还要考虑：

```text
path = tmp/foo.py, working_directory = ""
path = ./tmp/foo.py, working_directory = ""
path = foo.py, working_directory = tmp
```

它们可能指向同一文件，也可能不是。当前 key 没有统一解析目标脚本路径，因此 UI 去重和 runtime 去重都不稳定。

### 4.4 `run_python_file` 存在真实重复执行

除了事件重复显示，`run_python_file` 还存在真实重复执行，原因包括：

1. 模型并行发起多个工具调用，`write_file(path)` 尚未完成就调用 `run_python_file(path)`。
2. 第一次 `run_python_file` 报 `File not found`，随后 agent 又重新写文件并运行。
3. 跨 turn 继续任务时，同一脚本会被重新运行，因为 side-effect 工具默认不复用历史结果。
4. 路径变体或默认参数差异绕过去重 key。
5. PDF 表格提取任务本身多轮试错，agent 写了大量一次性脚本，例如 `extract_tables.py`、`extract_tables_v2.py`、`scan_tables.py`、`manual_table_extraction.py`。

## 5. 修复目标

### 5.1 必须修复

- 同一次实际工具调用，不应在 UI 中显示两次 `tool_call` / `tool_result`。
- 缺省参数与显式默认参数必须生成同一个 dedupe key。
- `write_file(path, content)` 与 `write_file(path, content, overwrite=True)` 必须视为同一调用。
- `run_python_file(path)` 与补齐默认参数后的调用必须视为同一调用。
- `record_count` 这类数值字段必须保持类型一致，避免 `"5"` 和 `5` 造成重复 side-effect 调用。

### 5.2 应该修复

- `tmp/foo.py` 与 `./tmp/foo.py` 应视为同一路径。
- 对同一 run 内同一 `run_python_file` 调用，应尽量复用 pending/result，而不是重复执行。
- 对 `write_file` 后立即 `run_python_file` 的场景，避免并行抢跑导致 `File not found`。

### 5.3 暂不强制修复

- agent 为解决任务写多个不同脚本，这是策略问题，不完全是 runtime bug。
- 跨 turn 重新执行脚本是否允许，需要按工具类型和用户意图区分，不宜一刀切禁止。

## 6. 修复方案

### 6.1 增加文件工具参数归一化

在 `backend/tools.py` 的 `_normalize_tool_payload_for_key()` 中新增：

```python
if tool_name == "write_file":
    return _normalize_write_file_payload(payload)

if tool_name == "run_python_file":
    return _normalize_run_python_file_payload(payload)
```

#### 6.1.1 `write_file` 归一化规则

输入：

```json
{
  "path": "./tmp/a.py",
  "content": "...",
  "overwrite": true
}
```

输出：

```json
{
  "path": "tmp/a.py",
  "content_hash": "sha256:...",
  "overwrite": true
}
```

规则：

- `overwrite` 缺省时按 `true` 处理。
- 路径统一成 workspace 相对 POSIX 风格。
- `content` 可以保留原文，也可以转 hash。推荐 key 内使用 hash，避免 call key 太长。
- 不要忽略 `overwrite=false`，因为它和默认覆盖语义不同。

建议实现：

```python
def _normalize_workspace_path_for_key(path: Any) -> str:
    raw = str(path or "").strip()
    if not raw:
        return ""
    try:
        root = _workspace_root()
        candidate = Path(raw).expanduser()
        target = candidate.resolve() if candidate.is_absolute() else (root / candidate).resolve()
        try:
            return target.relative_to(root).as_posix()
        except ValueError:
            return str(target)
    except Exception:
        return raw.replace("\\", "/").lstrip("./")


def _normalize_write_file_payload(payload: dict[str, Any]) -> dict[str, Any]:
    overwrite = payload.get("overwrite", True)
    return {
        "path": _normalize_workspace_path_for_key(payload.get("path")),
        "content_hash": _arguments_hash(payload.get("content") or ""),
        "overwrite": bool(overwrite),
    }
```

### 6.2 增加 `run_python_file` 参数归一化

#### 6.2.1 归一化规则

输入：

```json
{
  "path": "tmp/a.py"
}
```

与：

```json
{
  "path": "tmp/a.py",
  "cli_args": "",
  "timeout_seconds": 60,
  "working_directory": "",
  "python_executable": ""
}
```

输出应一致：

```json
{
  "path": "tmp/a.py",
  "cli_args": "",
  "timeout_seconds": 60,
  "working_directory": "",
  "python_executable": ""
}
```

规则：

- `cli_args` 缺省等同于 `""`。
- `timeout_seconds` 缺省等同于 `_PYTHON_RUN_TIMEOUT_SECONDS`，当前常见值为 `60`。
- `working_directory` 缺省等同于 `""`。
- `python_executable` 缺省等同于 `""`。
- 路径统一成 workspace 相对 POSIX 风格。
- 如果指定 `working_directory`，必须保留，因为同一脚本在不同 cwd 下行为可能不同。

建议实现：

```python
def _normalize_run_python_file_payload(payload: dict[str, Any]) -> dict[str, Any]:
    timeout = payload.get("timeout_seconds", _PYTHON_RUN_TIMEOUT_SECONDS)
    try:
        timeout = int(timeout)
    except Exception:
        timeout = _PYTHON_RUN_TIMEOUT_SECONDS
    return {
        "path": _normalize_workspace_path_for_key(payload.get("path")),
        "cli_args": str(payload.get("cli_args") or ""),
        "timeout_seconds": timeout,
        "working_directory": _normalize_workspace_path_for_key(payload.get("working_directory") or ""),
        "python_executable": str(payload.get("python_executable") or "").strip(),
    }
```

注意：`working_directory=""` 不应被规范化成 workspace root 字符串，否则会改变 key 的可读性。可以让 `_normalize_workspace_path_for_key("")` 返回 `""`。

### 6.3 修复 UI 事件去重

当前 `stream_agent_events()` 已经使用：

```python
display_key = runtime_tool_call_key(name, input_data)
is_duplicate_display_call = display_key in emitted_tool_call_keys
```

其中 `runtime_tool_call_key()` 调用 `_normalize_tool_payload_for_key()`。

因此只要第 6.1 / 6.2 的归一化正确，wrapper 层和原始工具层事件会生成同一个 `display_key`，第二个 UI `tool_call` 会被抑制。

但建议进一步修复 result suppression 队列。

当前结构：

```python
tool_result_suppression_queue: dict[str, list[bool]]
```

它只按 `tool_name` 排队。如果同名工具并发多次，结果顺序可能错位。

建议改为按 display key：

```python
tool_result_suppression_by_key: dict[str, bool]
tool_call_id_to_display_key: dict[str, str]
```

如果 LangGraph event 中能拿到 tool call id，使用 tool call id 映射最稳；如果拿不到，至少在 `on_tool_end` 用同样参数计算 key。

短期可保留队列，因为默认参数归一化已经能解决当前主要重复。

### 6.4 修复 runtime 去重

runtime wrapper 的 `_dedupe_key(tool_name, kwargs)` 也使用 `_normalize_tool_payload_for_key()`。

因此第 6.1 / 6.2 会同时修复：

- UI 重复显示。
- 同 run 内重复调用 key 不一致。
- 部分 conversation cache key 不一致。

建议为 `run_python_file` 增加 pending 保护。

当前 async wrapper 有 pending map，但 sync wrapper 的 `cached_func()` 没有 pending 保护。LangGraph 对同步工具并发执行时，可能两个线程同时进入：

```python
if cache_key not in cache:
    execute()
```

建议：

- 对 sync `cached_func()` 也使用 per-run pending lock。
- 或者将 `run_python_file` 标为不可并发的工具，串行执行同 key。

简化实现：

```python
_TOOL_DEDUPE_PENDING_SYNC: dict[str, dict[str, threading.Event]]
```

但要注意保存异常和结果，避免等待方拿不到内容。更安全的方式是统一把同步工具执行包装进 async path，或者在现有 pending map 中保存 `{"event": Event, "result": ..., "error": ...}`。

### 6.5 修复 `write_file` 抢跑 `run_python_file`

当前 session 中有这种模式：

```text
run_python_file tmp/install_pdfplumber.py -> File not found
write_file tmp/install_pdfplumber.py
run_python_file tmp/install_pdfplumber.py -> success
```

这不是单纯去重问题，而是依赖顺序问题。

修复选项：

#### 方案 A：新增原子工具 `write_and_run_python_file`

输入：

```json
{
  "path": "tmp/script.py",
  "content": "...",
  "cli_args": "",
  "timeout_seconds": 60,
  "working_directory": ""
}
```

内部顺序：

1. 写文件。
2. 编译或校验文件存在。
3. 运行脚本。
4. 返回写入结果 + 运行结果。

优点：

- 从根上避免模型把 write/run 并行发出。
- 减少 UI event 数。
- 适合临时验证脚本。

缺点：

- 增加一个新工具，模型需要学会优先用它。
- 对只想写文件不运行的场景仍需 `write_file`。

#### 方案 B：runtime 自动依赖屏障

当同一 batch 中存在：

```text
write_file(path=X)
run_python_file(path=X)
```

runtime 让 `run_python_file` 等待 `write_file` 完成。

优点：

- 对模型透明。

缺点：

- 实现复杂，需要跨工具 pending dependency。
- 如果 `run_python_file` 的 `path` 是相对 `working_directory` 的不同表达，匹配容易出错。

建议优先做方案 A。

### 6.6 避免 `install_xxx.py` 这类安装脚本

session 中出现：

```text
write_file tmp/install_pdfplumber.py
run_python_file tmp/install_pdfplumber.py
```

这类行为不理想：

- 安装依赖是环境操作，不应该通过临时 Python 文件执行。
- 容易触发重复执行。
- 输出很长，污染 session。

建议增加专门策略：

- 如果要检测库是否存在，使用短脚本可以。
- 如果要安装依赖，应走明确的 shell/approval 流程，而不是 `write_file + run_python_file`。
- 在 prompt 或工具说明中禁止“写一个 pip install 脚本再运行”。

## 7. 具体修改点

### 7.1 `backend/tools.py`

新增 helper：

- `_normalize_workspace_path_for_key`
- `_normalize_write_file_payload`
- `_normalize_run_python_file_payload`

修改：

- `_normalize_tool_payload_for_key`
- 可选：`_TOOL_CACHEABLE_TTL_SECONDS` 增加 `analyze_images`
- 可选：`run_python_file` 增加对 `write_file` pending 的依赖等待

### 7.2 `backend/agent.py`

短期无需大改，只要 `runtime_tool_call_key()` 使用改进后的 `_normalize_tool_payload_for_key()` 即可。

建议后续优化：

- `tool_result_suppression_queue` 从按 tool name 改成按 display key 或 tool call id。
- 对 suppressed duplicate 事件不要计入 `tool_call_names`，否则 summary 仍然放大。

当前逻辑：

```python
tool_call_names.append(name)
```

建议改成：

```python
if not is_duplicate_display_call:
    tool_call_names.append(name)
```

否则即使 UI 不展示重复，`Persistent Progress Summary` 仍会统计重复。

### 7.3 `backend/session_store.py`

已修复方向：

- `record_count` 入库时规范化成 int。
- artifact 注册时保留 `record_count`。
- identity 相同 artifact 更新时，如果新 artifact 没有 count，不覆盖旧 count。

建议补充：

- 对 tool event arguments 可选存 `normalized_arguments`，便于审计时区分“原始参数”和“去重参数”。

### 7.4 tests

新增或扩展：

- `tools/test_tool_semantic_dedupe.py`
- `tools/test_result_registry.py`

重点测试：

1. `write_file` 默认参数归一化。
2. `run_python_file` 默认参数归一化。
3. 路径 `tmp/a.py` 与 `./tmp/a.py` 归一化。
4. `record_primary_result(record_count="5")` 与 `record_primary_result(record_count=5)` key 一致。
5. duplicate UI event 不计入 summary tool count。

## 8. 推荐测试用例

### 8.1 `write_file` key 一致

```python
def test_write_file_default_overwrite_key_is_stable():
    base = {
        "path": "tmp/a.py",
        "content": "print(1)",
    }
    explicit = {
        "path": "./tmp/a.py",
        "content": "print(1)",
        "overwrite": True,
    }
    assert _dedupe_key("write_file", base) == _dedupe_key("write_file", explicit)
```

### 8.2 `write_file overwrite=false` 不应等同默认

```python
def test_write_file_overwrite_false_is_distinct():
    base = {
        "path": "tmp/a.py",
        "content": "print(1)",
    }
    no_overwrite = {
        "path": "tmp/a.py",
        "content": "print(1)",
        "overwrite": False,
    }
    assert _dedupe_key("write_file", base) != _dedupe_key("write_file", no_overwrite)
```

### 8.3 `run_python_file` 默认参数 key 一致

```python
def test_run_python_file_default_args_key_is_stable():
    minimal = {
        "path": "tmp/a.py",
    }
    expanded = {
        "path": "./tmp/a.py",
        "cli_args": "",
        "timeout_seconds": 60,
        "working_directory": "",
        "python_executable": "",
    }
    assert _dedupe_key("run_python_file", minimal) == _dedupe_key("run_python_file", expanded)
```

### 8.4 `run_python_file` 不同 cli_args 应区分

```python
def test_run_python_file_cli_args_are_distinct():
    a = {
        "path": "tmp/a.py",
        "cli_args": "one",
    }
    b = {
        "path": "tmp/a.py",
        "cli_args": "two",
    }
    assert _dedupe_key("run_python_file", a) != _dedupe_key("run_python_file", b)
```

### 8.5 summary 不重复计数

构造两个等价 `on_tool_start`：

```python
write_file(path="tmp/a.py", content="x")
write_file(path="tmp/a.py", content="x", overwrite=True)
```

期望：

- 前端只收到一条 `tool_call`。
- summary 只统计 `write_file x1`。

## 9. 验收标准

### 9.1 UI 验收

在类似 session 中：

- 不再出现相邻两条相同 `write_file`，其中第二条仅多 `overwrite: true`。
- 不再出现相邻两条相同 `run_python_file`，其中第二条仅多默认参数。
- `Persistent Progress Summary` 的 `write_file` / `run_python_file` 数量接近 runtime 实际执行次数。

### 9.2 runtime 验收

对同一 run：

```text
write_file(path=tmp/a.py, content=x)
write_file(path=tmp/a.py, content=x, overwrite=true)
```

只应真实执行一次，第二次应 dedupe 或 side-effect block。

对同一 run：

```text
run_python_file(path=tmp/a.py)
run_python_file(path=tmp/a.py, cli_args="", timeout_seconds=60, working_directory="", python_executable="")
```

只应真实执行一次，第二次应复用或 dedupe。

### 9.3 数据验收

- `record_primary_result.record_count` 永远以 number/int 形式存储。
- `record_count: "5"` 和 `record_count: 5` 不会生成两个 side-effect calls。

## 10. 风险和注意事项

### 10.1 不能过度合并不同语义的调用

以下调用不能被误合并：

- `overwrite=true` 与 `overwrite=false`
- 不同 `content`
- 不同 `cli_args`
- 不同 `working_directory`
- 不同 `python_executable`
- 不同 `timeout_seconds`，如果 timeout 影响任务行为

### 10.2 path 归一化要尊重 sandbox

对于 workspace 内路径，可以归一化为相对路径。

对于 workspace 外绝对路径：

- 不应强行相对化。
- Windows 下可以统一大小写或 `Path.resolve()`，但要避免改变用户指定路径语义。

### 10.3 side-effect 工具不能随意跨 turn 复用

`write_file`、`run_python_file` 都可能有 side effect。

同 run 内等价调用应去重；跨 turn 是否复用需要谨慎：

- `write_file` 跨 turn 重复写同内容通常可以提示已存在，但不应自动执行。
- `run_python_file` 跨 turn 重跑可能是用户明确要求继续验证，不能一律阻止。

## 11. 分阶段落地计划

### Phase 1：修 UI 重复和 key 不一致

修改：

- `_normalize_tool_payload_for_key`
- `_normalize_write_file_payload`
- `_normalize_run_python_file_payload`
- summary 计数跳过 duplicate display calls

预期收益：

- 大幅减少 UI 重复。
- 同 run dedupe key 稳定。
- 实现简单，风险低。

### Phase 2：修真实并发重复执行

修改：

- sync wrapper 增加 pending map。
- 或将工具执行统一走 async pending。

预期收益：

- 真正避免同一 key 并发执行两次。

风险：

- 需要处理异常传播和等待者返回。
- 并发工具较多时要避免 deadlock。

### Phase 3：新增原子工具

新增：

- `write_and_run_python_file`

预期收益：

- 避免模型把写脚本和跑脚本并行拆开。
- 降低工具事件数量。
- 提升任务稳定性。

### Phase 4：策略层减少脚本试错

调整 prompt / tool docs：

- 优先直接用已有读取工具。
- 禁止通过临时脚本安装依赖。
- 对 PDF/表格解析提供更高层工具，减少重复写脚本。

## 12. 推荐最终实现顺序

1. 实现 `write_file` / `run_python_file` payload 归一化。
2. 修改 `tool_call_names.append(name)`，只统计未 suppress 的 display call。
3. 增加 dedupe 单测。
4. 重新跑 `session-1784092258641` 的事件归类脚本，确认 UI 重复数下降。
5. 再做 sync pending，解决真实并发重复。
6. 最后考虑 `write_and_run_python_file`。

## 13. 当前结论

本次 session 的两个问题不是单纯“agent 笨”或“模型重复调用”：

- `write_file` 主要是 wrapper/original 双层事件 + 默认参数未归一化导致的 UI 重复。
- `run_python_file` 既有 UI 重复，也有真实重复；真实重复来自并行抢跑、路径变体、默认参数差异和跨 turn 继续执行。

最小有效修复是：

- 对 `write_file` 和 `run_python_file` 做语义级参数归一化。
- 让 UI 去重和 runtime 去重使用同一个归一化 key。
- 对 summary 计数排除 duplicate display calls。

这能直接解决用户看到的“连续两条相同工具事件”和“tool count 膨胀”问题，并降低真实重复执行概率。
