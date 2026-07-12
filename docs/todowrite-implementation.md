# TodoWrite 实现机制

> 对 Claude Code TodoWrite 工具的完整逆向分析，涵盖 prompt 工程、工具实现、状态管理、持久化与恢复。目标是使读者可在任何 agent 框架中复现等效机制。

## 目录

1. [概述](#概述)
2. [数据结构](#数据结构)
3. [Prompt 工程](#prompt-工程)
4. [工具实现](#工具实现)
5. [状态管理](#状态管理)
6. [持久化与恢复](#持久化与恢复)
7. [V1 vs V2 对比](#v1-vs-v2-对比)
8. [验证提醒机制](#验证提醒机制)
9. [复现指南](#复现指南)
10. [源码索引](#源码索引)

---

## 概述

TodoWrite 是 Claude Code 的**任务跟踪系统**，用于管理多步骤复杂任务的状态。它通过 prompt 引导模型自主决定何时创建/更新任务列表，并通过工具调用将状态写入内存（V1）或磁盘文件（V2）。

核心设计原则：
- **全量快照**：每次更新是完整列表，不是增量 patch
- **双文本**：每个任务有祈使句（content）和进行时（activeForm）
- **单任务执行**：恰好 1 个任务处于 in_progress 状态
- **间接持久化**：V1 中 task 状态通过 tool_use 块随对话流写入 JSONL，不是独立 entry
- **全完成即清空**：所有任务完成后自动清空列表

---

## 数据结构

### Zod Schema

```typescript
// src/utils/todo/types.ts

import { z } from 'zod/v4'

const TodoStatusSchema = z.enum(['pending', 'in_progress', 'completed'])

const TodoItemSchema = z.object({
  content: z.string().min(1),    // 祈使句，描述要做什么
  status: TodoStatusSchema(),    // pending | in_progress | completed
  activeForm: z.string().min(1), // 现在进行时，执行中展示
})

type TodoItem = {
  content: string
  status: 'pending' | 'in_progress' | 'completed'
  activeForm: string
}

type TodoList = TodoItem[]
```

### 双文本设计

| 字段 | 用途 | 示例 |
|------|------|------|
| `content` | 静态标签，描述目标（祈使句） | `"Run tests"`, `"Build the project"` |
| `activeForm` | 动态展示，当前动作（现在进行时） | `"Running tests"`, `"Building the project"` |

**为什么需要两个字段？** UI 渲染时根据 status 切换显示：pending/completed 显示 `content`，in_progress 显示 `activeForm`。这避免了模型在更新状态时需要同时改写描述文本——两个字段独立变化。

### AppState 存储结构

```typescript
// src/state/AppStateStore.ts:227

todos: { [agentId: string]: TodoList }

// 示例：
todos: {
  "session-abc-123": [                    // 主线程
    { content: "Create router", status: "completed", activeForm: "Creating router" },
    { content: "Setup database", status: "in_progress", activeForm: "Setting up database" },
  ],
  "agent-xyz-789": [                     // 子 agent
    { content: "Run lint", status: "in_progress", activeForm: "Running lint" },
  ]
}
```

按 `agentId` 或 `sessionId` 分区，支持多 agent 并发场景下各自维护独立的任务列表。

---

## Prompt 工程

完整 prompt 位于 [packages/builtin-tools/src/tools/TodoWriteTool/prompt.ts](../packages/builtin-tools/src/tools/TodoWriteTool/prompt.ts)，共 181 行。

### 3.1 模块结构

```
┌──────────────────────────────────────────────┐
│  When to Use (7 条)                          │
│  ├─ 1. 3+ 步骤的复杂任务                     │
│  ├─ 2. 非平凡/需要规划的任务                  │
│  ├─ 3. 用户显式请求 todo list                │
│  ├─ 4. 用户提供多个任务（编号/逗号分隔）       │
│  ├─ 5. 收到新指令后立即捕获为 todos            │
│  ├─ 6. 开始任务前标 in_progress              │
│  └─ 7. 完成后标 completed + 添加 follow-up   │
├──────────────────────────────────────────────┤
│  When NOT to Use (4 条)                      │
│  ├─ 1. 单步骤、直接的任务                    │
│  ├─ 2. 琐碎、无组织价值的任务                 │
│  ├─ 3. 少于 3 个琐碎步骤                     │
│  └─ 4. 纯对话/信息类                         │
├──────────────────────────────────────────────┤
│  Positive Examples (4 个)                    │
│  ├─ 暗色模式切换（多步骤 UI/状态/样式）        │
│  ├─ 全局函数重命名（搜索 → 追踪 → 批量修改）   │
│  ├─ 多个功能实现（注册/分类/购物车/结账）       │
│  └─ React 性能优化（分析 → 多项优化措施）      │
│  每个例子带 <reasoning> 解释为什么用          │
├──────────────────────────────────────────────┤
│  Negative Examples (4 个)                    │
│  ├─ "如何打印 Hello World"（信息类）          │
│  ├─ "git status 是什么"（解释类）             │
│  ├─ 单函数添加注释（单步骤）                   │
│  └─ npm install（单命令）                    │
│  每个例子带 <reasoning> 解释为什么不用         │
├──────────────────────────────────────────────┤
│  Task States and Management                  │
│  ├─ 3 种状态的定义                           │
│  ├─ 实时更新、立即标记、单任务执行的规则        │
│  ├─ 完成要求（禁止提前标记 completed）         │
│  └─ 拆分原则                                │
└──────────────────────────────────────────────┘
```

### 3.2 关键约束文本

```
## Task States and Management

1. **Task States**: Use these states to track progress:
   - pending: Task not yet started
   - in_progress: Currently working on (limit to ONE task at a time)
   - completed: Task finished successfully

   **IMPORTANT**: Task descriptions must have two forms:
   - content: The imperative form describing what needs to be done
   - activeForm: The present continuous form shown during execution

2. **Task Management**:
   - Update task status in real-time as you work
   - Mark tasks complete IMMEDIATELY after finishing (don't batch completions)
   - Exactly ONE task must be in_progress at any time (not less, not more)
   - Complete current tasks before starting new ones
   - Remove tasks that are no longer relevant from the list entirely

3. **Task Completion Requirements**:
   - ONLY mark a task as completed when you have FULLY accomplished it
   - If you encounter errors, blockers, or cannot finish, keep the task as in_progress
   - When blocked, create a new task describing what needs to be resolved
   - Never mark a task as completed if:
     - Tests are failing
     - Implementation is partial
     - You encountered unresolved errors
     - You couldn't find necessary files or dependencies
```

### 3.3 Prompt 中的引导策略

| 策略 | 实现方式 | 目的 |
|------|---------|------|
| **复杂度阈值** | "3+ distinct steps" | 防止琐碎任务污染列表 |
| **Few-shot 推理** | 每个例子带 `<reasoning>` 块 | 教会模型"为什么"而非"是什么" |
| **负例教学** | 4 个不应使用的场景 | 防止过度使用 |
| **积极性鼓励** | "When in doubt, use this tool" | 宁可多用不可漏用 |
| **实时性要求** | "IMMEDIATELY after finishing" | 防止批量更新导致状态滞后 |

---

## 工具实现

### 4.1 工具定义

```typescript
// packages/builtin-tools/src/tools/TodoWriteTool/TodoWriteTool.ts

export const TodoWriteTool = buildTool({
  name: 'TodoWrite',
  searchHint: 'manage the session task checklist',
  maxResultSizeChars: 100_000,
  strict: true,
  shouldDefer: true,  // 延迟加载（按需从工具索引中查找）

  isEnabled() {
    return !isTodoV2Enabled()  // V2 启用时禁用 V1
  },

  isConcurrencySafe() { return false },
  isReadOnly() { return false },

  checkPermissions(input) {
    return { behavior: 'allow', updatedInput: input }  // 无需权限
  },

  renderToolUseMessage() {
    return null  // 不在消息流中渲染（纯内务操作）
  },
})
```

### 4.2 call() 核心逻辑

```
                          ┌──────────────────┐
                          │  TodoWrite.call() │
                          └────────┬─────────┘
                                   │
                    ┌──────────────▼──────────────┐
                    │ 1. 获取 todoKey              │
                    │    = agentId ?? sessionId    │
                    │    区分主线程 vs 子 agent     │
                    └──────────────┬──────────────┘
                                   │
                    ┌──────────────▼──────────────┐
                    │ 2. 读取旧列表                │
                    │    oldTodos = appState       │
                    │      .todos[todoKey] ?? []   │
                    └──────────────┬──────────────┘
                                   │
                    ┌──────────────▼──────────────┐
                    │ 3. 全部完成检测              │
                    │    allDone = todos.every(    │
                    │      _ => _.status           │
                    │        === 'completed'       │
                    │    )                         │
                    │    newTodos = allDone        │
                    │      ? []                    │
                    │      : todos                 │
                    └──────────────┬──────────────┘
                                   │
                    ┌──────────────▼──────────────┐
                    │ 4. 验证提醒注入（条件触发）    │
                    │    IF VERIFICATION_AGENT     │
                    │    AND GrowthBook flag       │
                    │    AND 主线程                │
                    │    AND allDone               │
                    │    AND todos.length >= 3     │
                    │    AND 无 /verif/i 匹配      │
                    │    → nudgeNeeded = true      │
                    └──────────────┬──────────────┘
                                   │
                    ┌──────────────▼──────────────┐
                    │ 5. 写回 AppState（全量替换）  │
                    │    setAppState(prev => ({    │
                    │      ...prev,                │
                    │      todos: {                │
                    │        ...prev.todos,        │
                    │        [todoKey]: newTodos   │
                    │      }                       │
                    │    }))                       │
                    └──────────────┬──────────────┘
                                   │
                    ┌──────────────▼──────────────┐
                    │ 6. 返回 outputSchema         │
                    │    { oldTodos, newTodos,     │
                    │      verificationNudgeNeeded}│
                    └─────────────────────────────┘
```

完整源码：

```typescript
async call({ todos }, context) {
  const appState = context.getAppState()
  const todoKey = context.agentId ?? getSessionId()
  const oldTodos = appState.todos[todoKey] ?? []
  const allDone = todos.every(_ => _.status === 'completed')
  const newTodos = allDone ? [] : todos

  let verificationNudgeNeeded = false
  if (
    feature('VERIFICATION_AGENT') &&
    getFeatureValue_CACHED_MAY_BE_STALE('tengu_hive_evidence', false) &&
    !context.agentId &&
    allDone &&
    todos.length >= 3 &&
    !todos.some(t => /verif/i.test(t.content))
  ) {
    verificationNudgeNeeded = true
  }

  context.setAppState(prev => ({
    ...prev,
    todos: { ...prev.todos, [todoKey]: newTodos },
  }))

  return {
    data: { oldTodos, newTodos: todos, verificationNudgeNeeded },
  }
}
```

### 4.3 Tool Result 渲染

```typescript
mapToolResultToToolResultBlockParam({ verificationNudgeNeeded }, toolUseID) {
  const base =
    `Todos have been modified successfully. Ensure that you continue ` +
    `to use the todo list to track your progress. Please proceed with ` +
    `the current tasks if applicable`

  const nudge = verificationNudgeNeeded
    ? `\n\nNOTE: You just closed out 3+ tasks and none of them was ` +
      `a verification step. Before writing your final summary, spawn ` +
      `the verification agent (subagent_type="verification"). You ` +
      `cannot self-assign PARTIAL by listing caveats in your summary ` +
      `— only the verifier issues a verdict.`
    : ''

  return {
    tool_use_id: toolUseID,
    type: 'tool_result',
    content: base + nudge,
  }
}
```

### 4.4 关键实现细节

**全量替换语义**：每次 TodoWrite 都是完整列表快照。不存在 "add todo"、"remove todo"、"update todo #3" 这样的增量操作。这意味着：
- Resume 时只需读**最后一条** TodoWrite 记录即可恢复完整状态
- 中间态（添加/删除/状态变更）不需要独立持久化
- 简化了并发场景：同 key 的后续写入自动覆盖前序写入

**全部完成自动清空**：当 `todos.every(t => t.status === 'completed')` 时，`newTodos = []`。理由：
- 已完成列表堆积在 prompt 中浪费 context window
- 清空后模型不再看到历史任务，专注新任务

**与 V2 互斥**：`isEnabled()` 返回 `!isTodoV2Enabled()`。启用 Task V2 后，TodoWrite 工具完全不可见——模型只能使用 TaskCreate/TaskUpdate。

---

## 状态管理

### 5.1 AppState 中的 todos

```typescript
// src/state/AppStateStore.ts

type AppState = {
  // ...
  todos: { [agentId: string]: TodoList }   // 按 agent/session ID 分区
  // ...
}

// 默认值
const defaultState: AppState = {
  // ...
  todos: {},
  // ...
}
```

### 5.2 状态更新流程

```
模型生成 tool_use                    React/Ink 渲染 tool_use
  │                                       │
  │  {                                    │  显示 "TodoWrite: 5 items"
  │    "name": "TodoWrite",               │
  │    "input": {                         │
  │      "todos": [...]                   ▼
  │    }                          权限检查（checkPermissions）
  │  }                                    │
  │                                       │  behavior: 'allow'
  │                                       │
  └──────────► call({ todos }) ──────────►│
                                          │
                                   ┌──────▼──────┐
                                   │ setAppState  │
                                   │ (prev => {   │
                                   │   ...prev,   │
                                   │   todos: {   │
                                   │     ...prev  │
                                   │     .todos,  │
                                   │     [key]:   │
                                   │     newTodos │
                                   │   }          │
                                   │ })           │
                                   └──────┬──────┘
                                          │
                                   ┌──────▼──────┐
                                   │ JSONL 写入   │
                                   │ (随 transcript│
                                   │  录制，非独立  │
                                   │  entry)      │
                                   └─────────────┘
```

### 5.3 多 Agent 场景

主线程和子 agent 有独立的 todoKey：

```typescript
const todoKey = context.agentId ?? getSessionId()
```

- **主线程**：`todoKey = sessionId`（如 `"abc-123-def"`）
- **子 agent**：`todoKey = agentId`（如 `"agent-bob-456"`）

这使 swarm 模式下每个 agent 可以独立维护自己的任务列表，互不干扰。

---

## 持久化与恢复

### 6.1 V1 持久化（间接）

TodoWrite 状态**不作为独立 JSONL entry 类型**存在。它通过对话流中的 `tool_use` 块间接持久化：

```jsonl
{"type":"assistant","uuid":"msg-002","parentUuid":"msg-001","message":{"content":[
  {"type":"tool_use","name":"TodoWrite","id":"tu-001","input":{
    "todos":[
      {"content":"Create router","status":"completed","activeForm":"Creating router"},
      {"content":"Setup database","status":"in_progress","activeForm":"Setting up database"},
      {"content":"Run tests","status":"pending","activeForm":"Running tests"}
    ]
  }}
]}}
{"type":"user","uuid":"msg-003","parentUuid":"msg-002","message":{"content":[
  {"type":"tool_result","tool_use_id":"tu-001","content":"Todos have been modified successfully..."}
]}}
```

**JSONL 中不存在的**：
- 单独的 "task-status" entry 类型
- 任务增量更新记录
- 任务历史日志

**JSONL 中存在的**：
- tool_use 块（因为所有工具调用都被录制）
- tool_result 块（同上）

### 6.2 V1 恢复（反向解析）

```typescript
// src/utils/sessionRestore.ts:77-93

function extractTodosFromTranscript(messages: Message[]): TodoList {
  // 从最后一条消息反向扫描
  for (let i = messages.length - 1; i >= 0; i--) {
    const msg = messages[i]
    if (msg?.type !== 'assistant') continue   // 只看 assistant 消息

    // 找第一个（即最后一条）TodoWrite tool_use
    const toolUse = msg.message.content.find(
      block => block.type === 'tool_use' && block.name === 'TodoWrite'
    )
    if (!toolUse) continue

    // 提取 input.todos → Zod 校验 → 返回
    const parsed = TodoListSchema().safeParse(toolUse.input.todos)
    return parsed.success ? parsed.data : []
  }
  return []  // 没找到 → 空列表
}
```

恢复入口在 `restoreSessionStateFromLog()`：

```typescript
// sessionRestore.ts:138-149

if (!isTodoV2Enabled() && result.messages && result.messages.length > 0) {
  const todos = extractTodosFromTranscript(result.messages)
  if (todos.length > 0) {
    const agentId = getSessionId()
    setAppState(prev => ({
      ...prev,
      todos: { ...prev.todos, [agentId]: todos },
    }))
  }
}
```

**恢复特点**：
- 取最后一条 TodoWrite 的完整快照（中间态丢失但不影响最终状态）
- Zod 校验防止格式损坏
- 只在 V2 未启用时执行（V2 从文件系统恢复）
- 空列表时不恢复（避免覆盖可能存在的 V2 状态）

### 6.3 V2 持久化（独立文件）

V2 完全不依赖 JSONL。详见 [src/utils/tasks.ts](../src/utils/tasks.ts)。

```
~/.claude/tasks/
└── {taskListId}/
    ├── .highwatermark
    ├── .highwatermark.lock
    ├── {taskId}.json
    └── {taskId}.json.lock
```

每个 task 一个 JSON 文件，更新时全量覆盖。文件锁保证并发安全。

---

## V1 vs V2 对比

| 维度 | V1: TodoWrite | V2: TaskCreate/TaskUpdate |
|------|-------------|--------------------------|
| **工具名** | `TodoWrite` | `TaskCreate`, `TaskUpdate` |
| **存储位置** | 内存 (AppState.todos) | 磁盘文件 `~/.claude/tasks/{id}/*.json` |
| **JSONL 参与** | 间接（tool_use 块在对话流中） | 不参与（独立文件系统） |
| **更新模式** | 全量快照 | 单文件覆盖 |
| **并发安全** | 无 | 文件锁（lockfile） |
| **恢复方式** | 反向解析 JSONL → 最后一条 TodoWrite | 直接读文件系统 |
| **依赖链** | 不支持 | blocks / blockedBy 字段 |
| **子 agent 归属** | 按 agentId 分区 | 按 owner 字段 |
| **中间状态** | 丢失（只有最后的快照） | 保留（每次更新的文件） |
| **互斥关系** | V2 启用时隐藏 | V2 启用时替代 V1 |
| **适用场景** | 单 agent 会话 | 多 agent swarm |

---

## 验证提醒机制

这是 TodoWrite 中唯一包含条件逻辑的机制，但不是硬编码路由。

### 8.1 触发条件

```
全部满足时才触发:
  ├─ feature('VERIFICATION_AGENT') = true     // feature flag
  ├─ GrowthBook flag 'tengu_hive_evidence' = true  // 远程配置
  ├─ context.agentId == null                  // 主线程（非子 agent）
  ├─ allDone == true                          // 所有 task 均 completed
  ├─ todos.length >= 3                        // 至少 3 个任务
  └─ 无 task.content 匹配 /verif/i             // 用户未自己加验证步骤
```

### 8.2 触发行为

```
触发后 → mapToolResultToToolResultBlockParam 注入 nudge 文本:

"NOTE: You just closed out 3+ tasks and none of them was a
 verification step. Before writing your final summary, spawn
 the verification agent (subagent_type="verification"). You
 cannot self-assign PARTIAL by listing caveats in your summary
 — only the verifier issues a verdict."
```

### 8.3 重要澄清

**这不是强制路由。** 代码中不存在以下模式：
```typescript
// ❌ 不存在的伪代码
if (verificationNudgeNeeded) {
  forceCallAgent('verification')  // 硬编码路由
}
```

实际只是向 `tool_result` 的内容中追加了一段**提醒文本**。模型读到这段文本后**自主决定**是否调用 verification agent。这与整个 Claude Code 的架构一致——所有 agent 路由都是 prompt-driven，不是 code-driven。

---

## 复现指南

### 9.1 最低可行实现（V1 级别）

如果你的 agent 框架只需要基础的任务跟踪：

**Step 1: 定义 Schema**

```python
# 伪代码
class TodoItem:
    content: str      # "Run tests"
    status: Literal["pending", "in_progress", "completed"]
    activeForm: str   # "Running tests"

class TodoWriteInput:
    todos: list[TodoItem]
```

**Step 2: 注入 System Prompt**

将 [prompt.ts](../packages/builtin-tools/src/tools/TodoWriteTool/prompt.ts) 中的完整 prompt 模板翻译为你框架的语言，注入到 system prompt 中。关键点：
- 7 条使用场景 + 4 条跳过场景
- 4 正例 + 4 反例（每个带 reasoning）
- 3 种状态 + 单任务执行规则
- 双文本 (content + activeForm) 要求
- 完成要求（禁止提前标记 completed）

**Step 3: 实现 Tool Call Handler**

```python
def handle_todowrite(todos: list[TodoItem], session_id: str):
    old_todos = get_state(f"todos:{session_id}") or []

    all_done = all(t.status == "completed" for t in todos)
    new_todos = [] if all_done else todos

    set_state(f"todos:{session_id}", new_todos)

    return {
        "oldTodos": old_todos,
        "newTodos": todos,
        "verificationNudgeNeeded": False,
    }
```

**Step 4: 实现 Resume 恢复**

```python
def restore_todos_from_transcript(messages: list[Message]) -> list[TodoItem]:
    for msg in reversed(messages):
        if msg.type != "assistant":
            continue
        for block in msg.content:
            if block.type == "tool_use" and block.name == "TodoWrite":
                return block.input.get("todos", [])
    return []
```

### 9.2 完整实现（V2 级别）

如果需要多 agent 并发 + 依赖链 + 崩溃恢复：

**额外需求**：
- 每个 task 一个磁盘文件（JSON）
- 文件锁（lockfile / flock）
- 依赖链字段（blocks / blockedBy）
- owner 字段用于 agent 归属
- highwatermark 文件用于全局 ID 生成

Schema 扩展：

```python
class TaskV2:
    id: str
    subject: str
    description: str
    activeForm: str
    status: Literal["pending", "in_progress", "completed"]
    owner: str | None          # agentId
    blocks: list[str]           # 阻塞的 taskId 列表
    blockedBy: list[str]        # 被阻塞的 taskId 列表
    metadata: dict              # { priority: "high" }
```

### 9.3 Prompt 翻译要点

翻译 prompt 模板时注意：

| 原文关键短语 | 作用 | 必须保留等效表达 |
|-------------|------|-----------------|
| "3 or more distinct steps" | 复杂度阈值 | 是 |
| "Exactly ONE task must be in_progress" | 单任务约束 | 是 |
| "Mark tasks complete IMMEDIATELY" | 实时更新要求 | 是 |
| "content: imperative form" | 双文本解释 | 是 |
| "activeForm: present continuous" | 双文本解释 | 是 |
| "When in doubt, use this tool" | 积极性鼓励 | 是 |
| "Never mark as completed if: tests failing..." | 完成约束 | 是 |
| `<example>` + `<reasoning>` 块 | Few-shot 引导 | 强烈建议 |

### 9.4 可选增强

| 增强 | 描述 | 优先级 |
|------|------|--------|
| 验证提醒 | 当 3+ 任务全部完成且无验证步骤时，注入提醒文本 | 低 |
| 依赖链 | Task 之间有 blocks/blockedBy 关系 | 中 |
| 并发安全 | 文件锁保护多 agent 同时写 | 中（多 agent 场景） |
| 任务归属 | owner 字段标记哪个 agent 负责 | 低（多 agent 场景） |
| 进度持久化 | 中间状态变更写磁盘（而非等最终快照） | 低 |

---

## 源码索引

| 文件 | 行号 | 内容 |
|------|------|------|
| [packages/builtin-tools/src/tools/TodoWriteTool/prompt.ts](../packages/builtin-tools/src/tools/TodoWriteTool/prompt.ts) | 1-181 | 完整 prompt 模板（使用/不使用场景、示例、任务管理规则） |
| [packages/builtin-tools/src/tools/TodoWriteTool/TodoWriteTool.ts](../packages/builtin-tools/src/tools/TodoWriteTool/TodoWriteTool.ts) | 1-115 | 工具完整实现（call、权限、output schema、tool result 渲染） |
| [packages/builtin-tools/src/tools/TodoWriteTool/constants.ts](../packages/builtin-tools/src/tools/TodoWriteTool/constants.ts) | 1 | 工具名常量 `'TodoWrite'` |
| [src/utils/todo/types.ts](../src/utils/todo/types.ts) | 1-18 | TodoItem + TodoList Zod schema 定义 |
| [src/state/AppStateStore.ts](../src/state/AppStateStore.ts) | 227 | `todos: { [agentId: string]: TodoList }` 类型定义 |
| [src/state/AppStateStore.ts](../src/state/AppStateStore.ts) | 538 | todos 默认值 `{}` |
| [src/utils/sessionRestore.ts](../src/utils/sessionRestore.ts) | 77-93 | `extractTodosFromTranscript()` — 从 JSONL 反向解析恢复 V1 |
| [src/utils/sessionRestore.ts](../src/utils/sessionRestore.ts) | 99-150 | `restoreSessionStateFromLog()` — V1 恢复入口 |
| [src/utils/tasks.ts](../src/utils/tasks.ts) | 229-231 | V2 `getTaskPath()` — 单 task 文件路径 |
| [src/utils/tasks.ts](../src/utils/tasks.ts) | 284-307 | V2 `createTask()` — 文件写入 + 文件锁 |
| [src/utils/tasks.ts](../src/utils/tasks.ts) | 354-368 | V2 `updateTaskUnsafe()` — 文件覆盖更新 |
| [src/types/logs.ts](../src/types/logs.ts) | 369-391 | `Entry` 联合类型 — 20 种 JSONL entry 类型（不含 task 专用类型） |

---

## 附录：完整状态流转示例

```
Session Start
  │
  │  用户: "实现用户认证系统"
  │
  ├─► TodoWrite #1 — 创建任务列表
  │   todos: [
  │     {content:"Create user model",     status:"pending",     activeForm:"Creating user model"},
  │     {content:"Setup auth middleware", status:"pending",     activeForm:"Setting up auth middleware"},
  │     {content:"Build login endpoint",  status:"pending",     activeForm:"Building login endpoint"},
  │     {content:"Write tests",          status:"pending",     activeForm:"Writing tests"},
  │   ]
  │
  ├─► TodoWrite #2 — 开始第一个任务
  │   todos: [
  │     {content:"Create user model",     status:"in_progress", activeForm:"Creating user model"},
  │     {content:"Setup auth middleware", status:"pending",     activeForm:"..."},
  │     {content:"Build login endpoint",  status:"pending",     activeForm:"..."},
  │     {content:"Write tests",          status:"pending",     activeForm:"..."},
  │   ]
  │
  ├─► Bash: npx prisma generate
  ├─► Write: src/models/User.ts
  │
  ├─► TodoWrite #3 — 完成第一个，开始第二个
  │   todos: [
  │     {content:"Create user model",     status:"completed",   activeForm:"..."},
  │     {content:"Setup auth middleware", status:"in_progress", activeForm:"Setting up auth middleware"},
  │     {content:"Build login endpoint",  status:"pending",     activeForm:"..."},
  │     {content:"Write tests",          status:"pending",     activeForm:"..."},
  │   ]
  │
  ├─► ... 继续执行中间任务 ...
  │
  ├─► TodoWrite #5 — 全部完成
  │   todos: [
  │     {content:"Create user model",     status:"completed", ...},
  │     {content:"Setup auth middleware", status:"completed", ...},
  │     {content:"Build login endpoint",  status:"completed", ...},
  │     {content:"Write tests",          status:"completed", ...},
  │   ]
  │   → allDone = true → newTodos = []  ← 自动清空
  │   → 4 tasks >= 3, 无 /verif/i 匹配
  │   → verificationNudgeNeeded = true
  │   → tool_result 注入: "spawn the verification agent..."
  │
  └─► 模型自主决定是否调用 verification agent

Session Crash / Interrupt
  │
  ├─► Resume: loadTranscriptFile("session.jsonl")
  │   → messages = [...]
  │   → extractTodosFromTranscript(messages)
  │     → 反向扫描找到 TodoWrite #5 的 tool_use
  │     → 提取 input.todos
  │     → Zod 校验通过
  │     → 返回 [] (因为 #5 时全部完成)
  │
  └─► AppState.todos[sessionId] = []
      → 模型从空列表继续，重新创建任务
```
