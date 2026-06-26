# 父子 Agent 系统 — 技术实现文档与复现方案

> 基于 Claude Code 源码逆向分析，提供可复现的父子 Agent 系统技术方案。

---

## 目录

1. [总体架构](#1-总体架构)
2. [Agent 定义系统](#2-agent-定义系统)
3. [Agent 生命周期管理](#3-agent-生命周期管理)
4. [任务调度：同步/异步/后台自动转换](#4-任务调度)
5. [Agent 间通信](#5-agent-间通信)
6. [工具隔离与权限模型](#6-工具隔离与权限模型)
7. [上下文管理](#7-上下文管理)
8. [工作区隔离](#8-工作区隔离)
9. [恢复与持久化](#9-恢复与持久化)
10. [关键数据结构定义](#10-关键数据结构定义)
11. [分阶段实现路线图](#11-分阶段实现路线图)

---

## 1. 总体架构

### 1.1 核心抽象

父 Agent 的对话循环中有一个专门的工具 `AgentTool`，当模型调用此工具时，系统创建一个新的子 Agent 实例并在独立的上下文中运行。

```
┌─────────────────────────────────────────────────────────┐
│                     Parent query() Loop                  │
│                                                          │
│   User Message → API Call → Tool Use?                    │
│                                │                         │
│                    ┌───────────▼──────────┐              │
│                    │   AgentTool.call()   │              │
│                    │   (中央调度器)        │              │
│                    └───────────┬──────────┘              │
│                                │                         │
│              ┌─────────────────┼─────────────────┐       │
│              ▼                 ▼                  ▼       │
│        Sync Agent        Async Agent        Fork Agent   │
│        (阻塞父循环)      (后台运行)         (继承上下文)  │
│              │                 │                  │       │
│              └─────────────────┼──────────────────┘       │
│                                ▼                         │
│                     runAgent() 统一执行引擎               │
│                     • 独立 system prompt                 │
│                     • 独立工具池                         │
│                     • 独立 query() 循环                  │
│                     • 独立 abort controller              │
└─────────────────────────────────────────────────────────┘
```

### 1.2 核心组件关系

```
AgentDefinition  ──→  AgentTool.call()  ──→  runAgent()  ──→  query()
   (类型定义)          (调度决策)              (执行引擎)        (对话循环)
```

---

## 2. Agent 定义系统

### 2.1 AgentDefinition 数据结构

```typescript
// 基础定义 — 所有 Agent 类型共享
interface BaseAgentDefinition {
  agentType: string;            // 唯一标识符，如 "Explore", "code-reviewer"
  whenToUse: string;           // 何时使用的描述（注入到 AgentTool 的提示中）
  source: 'built-in' | 'userSettings' | 'projectSettings' | 'plugin';

  // 工具控制
  tools?: string[];            // 白名单：['*'] = 全部，['Read', 'Bash'] = 限制
  disallowedTools?: string[];  // 黑名单：排除特定工具

  // 执行控制
  model?: string;              // 模型覆盖（'inherit' = 使用父模型）
  permissionMode?: PermissionMode; // 权限模式覆盖
  maxTurns?: number;           // 最大对话轮次
  background?: boolean;        // 始终后台运行
  isolation?: 'worktree';      // git worktree 隔离

  // 提示词
  getSystemPrompt(context: ToolUseContext): string;  // 动态生成系统提示

  // 可选功能
  skills?: string[];           // 预加载的 skill
  mcpServers?: MCPConfig[];    // Agent 专属 MCP 服务器
  hooks?: HooksSettings;       // 生命周期钩子（SubagentStart/SubagentStop）
  memory?: 'user' | 'project' | 'local';  // 持久化记忆
  omitClaudeMd?: boolean;      // 省略项目上下文以节省 token
  color?: string;              // UI 显示颜色
  initialPrompt?: string;      // 首轮注入的提示
}

// 内置 Agent 定义 — 动态提示词
interface BuiltInAgentDefinition extends BaseAgentDefinition {
  source: 'built-in';
  getSystemPrompt: (context: ToolUseContext) => string;
  callback?: () => void;       // 完成后的回调（如清理）
}

// 自定义 Agent 定义 — 静态提示词（从 .md 文件加载）
interface CustomAgentDefinition extends BaseAgentDefinition {
  source: 'userSettings' | 'projectSettings' | 'policySettings';
  getSystemPrompt: () => string;  // 静态提示词通过闭包持有
  filename?: string;
}
```

### 2.2 Agent 注册与发现

```typescript
// Agent 加载优先级（后加载覆盖前加载）
const SOURCE_PRIORITY = [
  'built-in',         // 最低优先级
  'plugin',
  'userSettings',
  'projectSettings',
  'flagSettings',
  'policySettings',   // 最高优先级（管理员强制）
];

async function loadAllAgents(cwd: string): Promise<AgentDefinition[]> {
  // 1. 加载内置 Agent
  const builtIn = getBuiltInAgents();

  // 2. 加载插件 Agent
  const plugins = await loadPluginAgents();

  // 3. 从 .claude/agents/*.md 加载自定义 Agent
  const custom = await loadMarkdownConfigs('agents', cwd);

  // 4. 按优先级合并，后加载覆盖前加载同名
  return deduplicateByPriority([...builtIn, ...plugins, ...custom]);
}
```

### 2.3 最小内置 Agent 集合

| Agent | 职责 | 模型 | 工具 |
|-------|------|------|------|
| `general-purpose` | 通用任务执行 | 继承父模型 | 全部 |
| `Explore` | 只读代码搜索 | 轻量模型 | Read, Glob, Grep, Bash(readonly) |
| `Plan` | 设计方案 | 继承父模型 | 同 Explore |
| `verification` | 对抗性验证 | 继承父模型 | 全部（不可写项目文件） |

---

## 3. Agent 生命周期管理

### 3.1 状态机

```
                     registerAgent()
                          │
                          ▼
          ┌──────────────────────────────┐
          │         RUNNING               │
          │  (执行 query() 循环)          │
          └────┬────────┬────────┬───────┘
               │        │        │
    ┌──────────▼──┐ ┌──▼───┐ ┌──▼──────────┐
    │  COMPLETED  │ │FAILED│ │  KILLED      │
    │  (正常完成) │ │(错误)│ │  (用户中止)  │
    └─────────────┘ └──────┘ └──────────────┘

    中间转换:
    RUNNING ──(auto-background)──→ BACKGROUNDED
      │                                  │
      └── 继续在后台运行 ←───────────────┘
```

### 3.2 任务状态数据结构

```typescript
// 存储在 AppState.tasks 中
interface AgentTaskState {
  agentId: string;
  agentType: string;
  description: string;
  status: 'running' | 'completed' | 'failed' | 'killed' | 'backgrounded';
  abortController: AbortController;
  startTime: number;
  messages: Message[];          // 完整对话记录
  progress: {
    tokenCount: number;
    toolUseCount: number;
    durationMs: number;
  };
  pendingMessages: string[];    // 积压的 SendMessage 消息
  outputFile: string;           // 持久化输出路径
  transcript: string;           // 对话持久化路径
}
```

### 3.3 注册与注销

```typescript
// 注册同步 Agent（前台运行，可被自动转为后台）
function registerAgentForeground(params: {
  agentId: string;
  description: string;
  selectedAgent: AgentDefinition;
}): {
  taskId: string;
  backgroundSignal: Promise<void>;  // 当用户触发 backgroundAll() 时 resolve
  cancelAutoBackground: () => void; // 用户提前继续时取消自动后台化计时器
}

// 注册异步 Agent（直接后台运行）
function registerAsyncAgent(params: {
  agentId: string;
  description: string;
  selectedAgent: AgentDefinition;
}): { agentId: string; abortController: AbortController }

// 清理
function unregisterAgent(taskId: string): void;
function killAsyncAgent(taskId: string): void;
function completeAsyncAgent(result: AgentResult): void;
function failAsyncAgent(taskId: string, error: string): void;
```

---

## 4. 任务调度：同步/异步/后台自动转换

### 4.1 调度决策树

```typescript
function shouldRunAsync(
  input: AgentToolInput,
  agentDef: AgentDefinition,
  context: ToolUseContext
): boolean {
  // 1. 显式指定后台运行
  if (input.run_in_background === true) return true;

  // 2. Agent 定义要求后台运行
  if (agentDef.background === true) return true;

  // 3. Coordinator 模式下所有 agent 异步
  if (isCoordinatorMode()) return true;

  // 4. Fork 实验强制异步（统一交互模型）
  if (isForkSubagentEnabled()) return true;

  // 5. 助手模式（如 Kairos）下所有 agent 异步
  if (context.kairosEnabled) return true;

  // 6. 默认: 同步运行
  return false;
}
```

### 4.2 同步 Agent 执行流程

```typescript
async function executeSyncAgent(params: AgentParams): Promise<AgentResult> {
  const agentIterator = runAgent(params)[Symbol.asyncIterator]();
  const agentMessages: Message[] = [];
  const startTime = Date.now();

  // 创建后台竞速 Promise
  const backgroundPromise = createBackgroundRacePromise(taskId);

  try {
    while (true) {
      // 竞速: 下一个消息 vs 后台化信号
      const raceResult = await Promise.race([
        agentIterator.next().then(r => ({ type: 'message' as const, result: r })),
        backgroundPromise.then(() => ({ type: 'background' as const })),
      ]);

      if (raceResult.type === 'background') {
        // 用户触发了后台化
        await agentIterator.return();  // 优雅关闭当前迭代器
        // 转入后台执行（见 4.3）
        return await continueInBackground(taskId, params, agentMessages);
      }

      if (raceResult.result.done) break;

      const message = raceResult.result.value;
      agentMessages.push(message);

      // 转发进度到父 Agent 的 UI
      forwardProgressToParent(message, params);

      // 2 秒后显示 "后台运行" 提示
      if (elapsed > 2000 && !backgroundHintShown) {
        showBackgroundHint();
      }
    }
  } finally {
    unregisterAgentForeground(taskId);
  }

  return finalizeAgentResult(agentMessages);
}
```

### 4.3 同步转后台（auto-background）

当用户 120 秒不响应或主动触发 `backgroundAll()` 时：

```typescript
async function continueInBackground(
  taskId: string,
  params: AgentParams,
  existingMessages: Message[],
): Promise<AgentResult> {
  // 1. 获取后台任务的 abortController
  const task = getTask(taskId);
  const abortController = task.abortController;

  // 2. 用新的 isAsync=true 重启动 Agent
  for await (const msg of runAgent({
    ...params,
    isAsync: true,
    override: {
      abortController,
      agentId: taskId,
    },
  })) {
    existingMessages.push(msg);
    updateAsyncAgentProgress(taskId, ...);
  }

  // 3. 发送完成通知
  enqueueAgentNotification({
    taskId,
    status: 'completed',
    finalMessage: extractResult(existingMessages),
  });
}
```

### 4.4 异步 Agent 执行流程

```typescript
async function executeAsyncAgent(params: AgentParams): Promise<AsyncResult> {
  const taskId = generateAgentId();

  // 1. 注册任务到 AppState
  const task = registerAsyncAgent({ agentId: taskId, ... });

  // 2. 如果指定了 name，注册到名称注册表（用于 SendMessage 路由）
  if (params.name) {
    registerAgentName(params.name, taskId);
  }

  // 3. 立即返回 — 不等待完成
  void runWithAgentContext(subAgentContext, async () => {
    try {
      const messages: Message[] = [];
      for await (const msg of runAgent({ ...params, isAsync: true, agentId: taskId })) {
        messages.push(msg);
        updateAsyncAgentProgress(taskId, ...);
      }
      completeAsyncAgent(taskId, finalizeResult(messages));
      enqueueAgentNotification({ taskId, status: 'completed', ... });
    } catch (error) {
      failAsyncAgent(taskId, error.message);
      enqueueAgentNotification({ taskId, status: 'failed', ... });
    } finally {
      unregisterAgentName(params.name);
      cleanupAgentResources(taskId);
    }
  });

  // 4. 返回异步启动信息
  return {
    status: 'async_launched',
    agentId: taskId,
    description: params.description,
    outputFile: getTaskOutputPath(taskId),
  };
}
```

---

## 5. Agent 间通信

### 5.1 通信架构

Agent 间通信通过三种方式：

| 方式 | 延迟 | 方向 | 适用场景 |
|------|------|------|---------|
| SendMessage | 一回合（tool round 边界） | 任意→命名 Agent | 任务委派、进度查询 |
| task-notification | 完成时 | 子→父 | 异步 Agent 完成通知 |
| TaskOutput | 主动轮询 | 父→子 | 检查异步 Agent 状态 |

### 5.2 SendMessage 实现

```typescript
// ── 数据结构 ──

// AppState 中的注册表
interface AppState {
  agentNameRegistry: Map<string, string>;  // name → agentId
  tasks: Record<string, AgentTaskState>;    // agentId → task
}

// 每个 Agent Task 中的消息队列
interface AgentTaskState {
  pendingMessages: string[];  // 积压消息
}

// ── 发送端 ──

async function sendMessageToAgent(
  targetName: string,
  message: string,
  context: ToolUseContext,
): Promise<SendResult> {
  const appState = context.getAppState();

  // 1. 解析目标 — 名称 → agentId
  const agentId = appState.agentNameRegistry.get(targetName);
  if (!agentId) {
    throw new Error(`Agent "${targetName}" not found`);
  }

  const task = appState.tasks[agentId];

  // 2. 如果 Agent 正在运行 — 排队消息
  if (task?.status === 'running') {
    queuePendingMessage(agentId, message, context.setAppState);
    return { success: true, message: `Message queued for ${targetName}` };
  }

  // 3. 如果 Agent 已停止 — 恢复并注入消息
  return await resumeAgentBackground({
    agentId,
    prompt: message,
    toolUseContext: context,
  });
}

// ── 接收端（在 query loop 的 attachment 阶段）──

function drainPendingMessages(agentId: string): string[] {
  const task = getAppState().tasks[agentId];
  if (!task || task.pendingMessages.length === 0) return [];
  const drained = [...task.pendingMessages];
  updateTask(agentId, { pendingMessages: [] });
  return drained;
}

// 在每轮 query loop 中被调用:
// for (const msg of drainPendingMessages(agentId)) {
//   yield createUserMessage({
//     content: `<cross-session-message from="sender">${msg}</cross-session-message>`,
//     isMeta: true,
//   });
// }
```

### 5.3 完成通知实现

```typescript
// 异步 Agent 完成时，通知自动注入到父 Agent 的消息队列
function enqueueAgentNotification(params: {
  taskId: string;
  description: string;
  status: 'completed' | 'failed' | 'killed';
  finalMessage: string;
  usage: { totalTokens: number; toolUses: number; durationMs: number };
}): void {
  enqueue({
    mode: 'task-notification',
    prompt: `
<task-notification>
  <task_id>${params.taskId}</task_id>
  <status>${params.status}</status>
  <description>${params.description}</description>
  <usage>
    total_tokens: ${params.usage.totalTokens}
    tool_uses: ${params.usage.toolUses}
    duration_ms: ${params.usage.durationMs}
  </usage>
</task-notification>
${params.finalMessage}`,
    agentId: undefined,  // undefined = 发送到主线程
    priority: 'next',    // 下一轮处理
  });
}
```

### 5.4 一次完整通信示例

```
1. Main Agent 创建子 Agent:
   Agent({ subagent_type: "code-reviewer", name: "reviewer",
           prompt: "review the last commit" })

2. AgentTool.call() 注册:
   agentNameRegistry: { "reviewer" → "agent-abc123" }
   tasks["agent-abc123"]: { status: "running", pendingMessages: [] }

3. Main Agent 发送消息:
   SendMessage({ to: "reviewer",
                 message: "also check for security issues" })

4. SendMessageTool.call():
   agentId = registry.get("reviewer") → "agent-abc123"
   task = tasks["agent-abc123"]
   status === "running" → queuePendingMessage("agent-abc123", message)
   tasks["agent-abc123"].pendingMessages: ["also check for security issues"]

5. Reviewer Agent 的 query loop 下一轮:
   drainPendingMessages("agent-abc123") → ["also check for security issues"]
   生成 user message: "<cross-session from='main'>also check for security issues"
   注入到 reviewer 的对话中

6. Reviewer 完成:
   completeAsyncAgent("agent-abc123", result)
   enqueueAgentNotification(...) → 通知进入主线程队列

7. Main Agent 收到通知:
   <task-notification status="completed" ...>
   "Here's my review: ..."
```

---

## 6. 工具隔离与权限模型

### 6.1 工具池组装

```typescript
async function assembleAgentTools(
  agentDef: AgentDefinition,
  parentTools: Tool[],
  parentPermissionContext: ToolPermissionContext,
): Promise<Tool[]> {
  // 1. 基础工具池 — 从父工具继承
  let tools = [...parentTools];

  // 2. 应用白名单/黑名单
  if (agentDef.tools && agentDef.tools[0] !== '*') {
    tools = tools.filter(t => agentDef.tools!.includes(t.name));
  }
  if (agentDef.disallowedTools) {
    const blacklist = new Set(agentDef.disallowedTools);
    tools = tools.filter(t => !blacklist.has(t.name));
  }

  // 3. 添加 Agent 专属 MCP 工具
  if (agentDef.mcpServers?.length) {
    for (const mcpConfig of agentDef.mcpServers) {
      const client = await connectMcpServer(mcpConfig);
      const mcpTools = await fetchToolsForClient(client);
      tools = [...tools, ...mcpTools];
    }
    // 按名称去重
    tools = uniqBy(tools, t => t.name);
  }

  return tools;
}
```

### 6.2 权限模式继承

```typescript
// Agent 的 getAppState 是一个包装函数，修改权限上下文
function createAgentGetAppState(
  parentGetAppState: () => AppState,
  agentDef: AgentDefinition,
  isAsync: boolean,
): () => AppState {
  return () => {
    const state = parentGetAppState();
    let permissionCtx = { ...state.toolPermissionContext };

    // 1. Agent 定义的 permissionMode 覆盖（除非父是 bypass/acceptEdits）
    if (
      agentDef.permissionMode &&
      state.toolPermissionContext.mode !== 'bypassPermissions' &&
      state.toolPermissionContext.mode !== 'acceptEdits'
    ) {
      permissionCtx.mode = agentDef.permissionMode;
    }

    // 2. 异步 Agent 不能显示 UI → 自动拒绝权限请求
    if (isAsync) {
      permissionCtx.shouldAvoidPermissionPrompts = true;
    }

    // 3. allowedTools 白名单 — 替换 session 级允许规则
    if (agentDef.allowedTools) {
      permissionCtx.alwaysAllowRules = {
        cliArg: permissionCtx.alwaysAllowRules.cliArg,  // 保留 SDK 级
        session: [...agentDef.allowedTools],             // 替换 session 级
      };
    }

    return { ...state, toolPermissionContext: permissionCtx };
  };
}
```

### 6.3 Agent 间工具差异示例

```
Parent Agent (全部工具):
  [Agent, Read, Write, Edit, Bash, Glob, Grep, WebFetch,
   WebSearch, SendMessage, Task, Cron, ...]

Explore Agent (只读，禁用写和嵌套):
  [Read, Glob, Grep, Bash(readonly), WebFetch, WebSearch]
  禁用: Agent, Write, Edit, NotebookEdit, ExitPlanMode

Verification Agent (全部但不可写项目文件):
  [Read, Bash, Glob, Grep, WebFetch, WebSearch, ...]
  禁用: Agent, Write, Edit, NotebookEdit, ExitPlanMode
  提示词约束: 可在 /tmp 写临时脚本

Code Review Agent (全部工具，特定权限):
  [Read, Glob, Grep, Bash, ...]
  权限: permissionMode = 'dontAsk'
```

---

## 7. 上下文管理

### 7.1 两种上下文继承模式

```
模式 A: 独立上下文（默认）
  ┌──────────────────────┐
  │  Parent              │
  │  sys_prompt (parent) │
  │  history[...]        │
  └──────────────────────┘
         │
         │ AgentTool(prompt="do X")
         ▼
  ┌──────────────────────┐
  │  Child               │
  │  sys_prompt (child)  │  ← 完全独立的系统提示
  │  [user: "do X"]      │  ← 只有单条用户消息
  └──────────────────────┘

模式 B: Fork 上下文（继承父上下文）
  ┌──────────────────────┐
  │  Parent              │
  │  sys_prompt (parent) │
  │  history[turn1...N]  │
  └──────────────────────┘
         │
         │ AgentTool(prompt="continue refactoring")
         ▼
  ┌──────────────────────┐
  │  Child (Fork)        │
  │  sys_prompt (parent) │  ← 继承父的系统提示（byte-identical）
  │  history[turn1...N]  │  ← 继承完整对话历史
  │  assistant(tools...) │  ← 父的最后一条 assistant 消息
  │  user(placeholders + directive) │ ← 占位 tool_results + 子指令
  └──────────────────────┘
```

### 7.2 Fork Prompt Cache 共享

Fork 模式的核心优化 — 父子共享 Anthropic API prompt cache：

```typescript
function buildForkedMessages(
  directive: string,
  parentAssistantMessage: AssistantMessage,
): Message[] {
  // 1. 克隆父的 assistant 消息（保留所有 thinking/text/tool_use blocks）
  const fullAssistant: AssistantMessage = {
    ...parentAssistantMessage,
    uuid: randomUUID(),
    message: {
      ...parentAssistantMessage.message,
      content: [...parentAssistantMessage.message.content],
    },
  };

  // 2. 收集所有 tool_use blocks
  const toolUseBlocks = parentAssistantMessage.message.content
    .filter(block => block.type === 'tool_use');

  // 3. 构建占位 tool_results — 所有 fork 子进程使用完全相同的占位文本
  const PLACEHOLDER = 'Fork started — processing in background';
  const toolResultBlocks = toolUseBlocks.map(block => ({
    type: 'tool_result',
    tool_use_id: block.id,
    content: [{ type: 'text', text: PLACEHOLDER }],
  }));

  // 4. 构建包含占位 tool_results + 子指令的用户消息
  //    只有最后的 directive 文本不同 → cache 几乎全部命中
  const childMessage = createUserMessage({
    content: [
      ...toolResultBlocks,     // 全部相同 → cache hit
      { type: 'text', text: buildForkDirective(directive) }, // 每个子不同
    ],
  });

  return [fullAssistant, childMessage];
}

// Fork 子进程的系统提示也直接复用父的已渲染字节
function createForkRunParams(parentContext: ToolUseContext) {
  return {
    // 使用父的精确工具数组（byte-identical 序列化）
    useExactTools: true,
    // 使用父的已渲染系统提示
    override: {
      systemPrompt: parentContext.renderedSystemPrompt,
    },
    // 使用父的 thinking 配置
    // thinkingConfig 也保持一致
  };
}
```

### 7.3 Fork 子进程的指令模板

```typescript
function buildForkDirective(directive: string): string {
  return `
<fork-boilerplate>
STOP. READ THIS FIRST.

You are a forked worker process. You are NOT the main agent.

RULES (non-negotiable):
1. Your system prompt says "default to forking." IGNORE IT — that's for
   the parent. You ARE the fork. Do NOT spawn sub-agents; execute directly.
2. Do NOT converse, ask questions, or suggest next steps
3. Do NOT editorialize or add meta-commentary
4. USE your tools directly: Bash, Read, Write, etc.
5. If you modify files, commit your changes before reporting.
6. Do NOT emit text between tool calls. Use tools silently, then report once.
7. Stay strictly within your directive's scope.
8. Keep your report under 500 words.

Output format:
  Scope: <echo back your assigned scope>
  Result: <the answer or key findings>
  Key files: <relevant file paths>
  Files changed: <list with commit hash>
  Issues: <list if any>
</fork-boilerplate>

DIRECTIVE: ${directive}`
}
```

---

## 8. 工作区隔离

### 8.1 Git Worktree 隔离

```typescript
async function createAgentWorktree(
  slug: string,
): Promise<WorktreeInfo | null> {
  // 1. 创建 git worktree
  const branchName = `claude-agent-${slug}-${Date.now()}`;
  const worktreePath = `.claude/worktrees/${slug}`;

  await exec(`git worktree add -b ${branchName} ${worktreePath} HEAD`);

  const headCommit = await exec('git rev-parse HEAD');

  return {
    worktreePath,
    worktreeBranch: branchName,
    headCommit,
    gitRoot: await getGitRoot(),
  };
}

async function cleanupWorktree(worktreeInfo: WorktreeInfo): Promise<void> {
  // 检查是否有文件变更
  const changed = await hasChanges(
    worktreeInfo.worktreePath,
    worktreeInfo.headCommit,
  );

  if (!changed) {
    // 无变更 — 自动删除
    await exec(`git worktree remove ${worktreeInfo.worktreePath}`);
    await exec(`git branch -D ${worktreeInfo.worktreeBranch}`);
  }
  // 有变更 — 保留，让用户处理
}

// Fork + Worktree 组合时，注入路径翻译指令
function buildWorktreeNotice(
  parentCwd: string,
  worktreeCwd: string,
): string {
  return `
You've inherited conversation context from a parent agent working in
${parentCwd}. You are operating in an isolated git worktree at
${worktreeCwd} — same repository, same relative file structure,
separate working copy.

- Paths in the inherited context refer to the parent's working directory;
  translate them to your worktree root.
- Re-read files before editing if the parent may have modified them.
- Your changes stay in this worktree and will not affect the parent's files.
`;
}
```

---

## 9. 恢复与持久化

### 9.1 Sidechain Transcript

每个 Agent 有独立的对话记录（sidechain transcript），与父 Agent 的 transcript 分开存储：

```typescript
// 写入 Agent transcript（fire-and-forget）
async function recordSidechainTranscript(
  messages: Message[],
  agentId: string,
  parentUuid?: UUID,  // 前一条消息的 UUID（用于链式连接）
): Promise<void> {
  const dir = `subagents/${agentId}/`;
  const file = `transcript.jsonl`;  // JSONL 格式
  const path = `${dir}${file}`;

  for (const msg of messages) {
    await appendJSONL(path, {
      uuid: msg.uuid,
      type: msg.type,
      message: msg.message,
      timestamp: msg.timestamp,
      parentUuid,  // 用于重建消息链
    });
  }
}

// 从 transcript 恢复 Agent
async function resumeAgentBackground(params: {
  agentId: string;
  prompt: string;  // 新的用户消息
}): Promise<{ agentId: string; description: string; outputFile: string }> {
  // 1. 加载 transcript
  const transcript = await loadAgentTranscript(params.agentId);
  const messages = transcript.messages;

  // 2. 过滤无效消息
  const validMessages = filterIncompleteToolCalls(
    filterWhitespaceOnly(messages),
  );

  // 3. 恢复 content replacement 状态（用于 tool result budget）
  const replacementState = reconstructContentReplacement(
    transcript.contentReplacements,
    validMessages,
  );

  // 4. 追加新用户消息
  const resumedMessages = [
    ...validMessages,
    createUserMessage({ content: params.prompt }),
  ];

  // 5. 重新启动 Agent
  return executeAsyncAgent({
    promptMessages: resumedMessages,
    contentReplacementState: replacementState,
  });
}
```

### 9.2 Agent 元数据持久化

```typescript
// 持久化 Agent 元数据（与 transcript 分开）
async function writeAgentMetadata(agentId: string, meta: {
  agentType: string;
  description?: string;
  worktreePath?: string;
}): Promise<void> {
  const path = `subagents/${agentId}/metadata.json`;
  await writeJSON(path, {
    ...meta,
    timestamp: Date.now(),
  });
}
```

### 9.3 清理

```typescript
async function cleanupAgentResources(agentId: string): Promise<void> {
  // 1. 清理 MCP 连接
  await mcpCleanup();

  // 2. 清理 session hooks
  clearSessionHooks(agentId);

  // 3. 清理 prompt cache 追踪
  cleanupAgentTracking(agentId);

  // 4. 清理 file state cache
  agentReadFileState.clear();

  // 5. 清理 todos
  deleteTodoList(agentId);

  // 6. 杀掉后台 shell 任务
  killShellTasksForAgent(agentId);

  // 7. 取消 Perfetto trace 注册
  unregisterPerfettoAgent(agentId);

  // 8. 清理 transcript 目录映射
  clearAgentTranscriptSubdir(agentId);
}
```

---

## 10. 关键数据结构定义

### 10.1 runAgent 参数（统一执行引擎）

```typescript
interface RunAgentParams {
  agentDefinition: AgentDefinition;       // Agent 类型定义
  promptMessages: Message[];              // 初始提示消息
  toolUseContext: ToolUseContext;         // 父的工具上下文
  canUseTool: CanUseToolFn;              // 权限检查函数
  isAsync: boolean;                       // 是否异步运行
  querySource: string;                    // 'agent:builtin:explore' 等

  // 上下文
  forkContextMessages?: Message[];       // Fork 模式下的父消息
  availableTools: Tool[];                // 工具池
  contentReplacementState?: ContentReplacementState;  // 恢复时使用

  // 覆盖
  override?: {
    userContext?: Record<string, string>;
    systemContext?: Record<string, string>;
    systemPrompt?: SystemPrompt;          // Fork 模式: 继承父 prompt
    abortController?: AbortController;    // 异步: 独立; 同步: 共享
    agentId?: string;
  };

  // 工具控制
  useExactTools?: boolean;               // Fork: 使用父的精确工具数组
  allowedTools?: string[];               // 白名单

  // 隔离
  worktreePath?: string;

  // 可恢复性
  transcriptSubdir?: string;

  // 回调
  onCacheSafeParams?: (params: CacheSafeParams) => void;  // 后台摘要
}
```

### 10.2 AgentTool 输入 Schema

```typescript
interface AgentToolInput {
  description: string;          // "review the last commit" (3-5 words)
  prompt: string;               // 完整任务描述
  subagent_type?: string;       // 不指定 = general-purpose 或 fork
  model?: 'sonnet' | 'opus' | 'haiku';  // 模型覆盖
  run_in_background?: boolean;  // 后台运行
  isolation?: 'worktree';       // git worktree 隔离
}
```

### 10.3 AgentTool 输出 Schema

```typescript
// 同步完成
interface SyncOutput {
  status: 'completed';
  prompt: string;
  content: ContentBlock[];       // Agent 的最终回复
  totalTokens: number;
  totalToolUseCount: number;
  totalDurationMs: number;
  agentId: string;
  worktreePath?: string;
  worktreeBranch?: string;
}

// 异步启动
interface AsyncOutput {
  status: 'async_launched';
  agentId: string;
  description: string;
  prompt: string;
  outputFile: string;
  canReadOutputFile: boolean;
}

type AgentToolOutput = SyncOutput | AsyncOutput;
```

---

## 11. 分阶段实现路线图

### Phase 1: 核心框架（最小可行实现）

**目标**: 能创建同步子 Agent 执行任务并返回结果

```
需要实现的模块:
├─ AgentDefinition 类型 + 内置 Agent 注册表
├─ AgentTool 工具（注册到父的 tool list）
│   ├─ input schema (description, prompt, subagent_type)
│   └─ output schema (status: completed + content)
├─ runAgent() 执行引擎
│   ├─ 独立 system prompt 生成
│   ├─ 独立 query() 循环
│   └─ 结果收集与返回
├─ 基础工具过滤
│   ├─ tools: ['*'] → 全部
│   └─ disallowedTools → 排除
└─ 3 个内置 Agent:
    ├─ general-purpose
    ├─ Explore (只读)
    └─ Plan (只读)
```

**关键集成点**:
1. 父的 `query()` 循环需要能处理 `AgentTool` 的工具调用
2. `runAgent()` 需要能复用父的 API client、工具注册表、会话上下文
3. 子 Agent 的 system prompt 需要独立构建（包含日期、环境等基础信息）

**预期工作量**: 2-3 周

### Phase 2: 异步执行与通信

**目标**: 子 Agent 可以在后台运行，父 Agent 可以继续工作

```
需要实现的模块:
├─ AgentTaskState 数据结构
├─ 任务注册/注销 (registerAsyncAgent 等)
├─ SendMessage 工具
│   ├─ agentNameRegistry
│   ├─ pendingMessages 队列
│   └─ drainPendingMessages (在 query loop 中)
├─ task-notification 自动通知
└─ auto-background 机制（同步→异步转换）
```

**关键集成点**:
1. query loop 的 attachment 阶段需要调用 `drainPendingMessages`
2. 消息队列需要支持跨 agent 路由
3. 异步完成需要可靠的通知机制

**预期工作量**: 2-3 周

### Phase 3: 高级特性

**目标**: Fork 模式、Worktree 隔离、恢复能力

```
需要实现的模块:
├─ Fork 模式
│   ├─ buildForkedMessages()
│   ├─ prompt cache 共享（如适用）
│   └─ useExactTools 机制
├─ Git Worktree 隔离
│   ├─ createAgentWorktree()
│   └─ cleanupWorktree()
├─ Sidechain Transcript 持久化
│   ├─ recordSidechainTranscript()
│   └─ resumeAgentBackground()
└─ Agent 专属 MCP 服务器
```

**关键集成点**:
1. Fork 模式需要访问父的 `renderedSystemPrompt`
2. Worktree 需要 cwd override 机制
3. Transcript 需要与父的 transcript 分开存储

**预期工作量**: 2-3 周

### Phase 4: 生产加固

```
├─ 错误处理与重试
├─ Token 预算管理（Agent 间分配）
├─ 并发控制（最大同时运行 Agent 数）
├─ 超时处理
├─ 清理僵尸资源
└─ 监控与遥测
```

---

## 附录: 与现有 Agent 框架的差异

| 特性 | Claude Code | LangChain | AutoGen | CrewAI |
|------|:-----------:|:---------:|:-------:|:------:|
| Agent 定义 | 声明式 (YAML/MD) | 代码定义 | 代码定义 | 代码定义 |
| 执行引擎 | 统一 runAgent() | 自定义 chain | 自定义 | 自定义 |
| 上下文继承 | Fork (prompt cache 共享) | 手动传递 | 消息传递 | 消息传递 |
| 工具隔离 | 按 Agent 定义过滤 | 按 Agent 绑定 | 按 Agent 绑定 | 按 Agent 绑定 |
| 自动后台化 | ✅ (120s) | ❌ | ❌ | ❌ |
| 断开恢复 | ✅ (sidechain transcript) | ❌ | ❌ | ❌ |
| 权限模型 | 继承+覆盖+冒泡 | 无内置 | 无内置 | 无内置 |
| 通信 | SendMessage + Mailbox + Notification | 无内置 | talk/conversation | 消息传递 |

Claude Code 的父子 Agent 设计最独特的三个特性：
1. **Fork 模式的 prompt cache 共享** — 子 Agent 继承父的 system prompt 字节和工具定义实现 API 缓存命中
2. **自动后台化** — 同步 Agent 在 120s 后自动转为后台，用户不会被阻塞
3. **断开可恢复** — 每个 Agent 有独立的 sidechain transcript，跨会话 resume
