# Agent 路由决策机制 — 可复现技术文档

> 如何让模型自主决定何时使用多 Agent、何时单 Agent 直行，完整复现 Claude Code 的路由决策体系。

---

## 目录

1. [核心设计理念](#1-核心设计理念)
2. [路由决策全景架构](#2-路由决策全景架构)
3. [第一层：Agent 工具描述（Prompt Template）](#3-第一层agent-工具描述)
4. [第二层：系统提示中的使用指导](#4-第二层系统提示中的使用指导)
5. [第三层：Verification 强制合约](#5-第三层verification-强制合约)
6. [第四层：Fork 模式零摩擦路由](#6-第四层fork-模式零摩擦路由)
7. [第五层：反向约束（When NOT to use）](#7-第五层反向约束when-not-to-use)
8. [第六层：代码级路由逻辑](#8-第六层代码级路由逻辑)
9. [完整 Prompt 模板组装](#9-完整-prompt-模板组装)
10. [Feature Flag 等价实现](#10-feature-flag-等价实现)
11. [分阶段复现路线图](#11-分阶段复现路线图)

---

## 1. 核心设计理念

Claude Code 的 Agent 路由**没有任何硬编码的任务分类器**。模型 100% 自主决定是否使用 Agent。

路由体系由 **6 层提示引导** 叠加构成：

```
┌──────────────────────────────────────────────────────────────┐
│                      用户发出任务                              │
└────────────────────────┬─────────────────────────────────────┘
                         ▼
┌──────────────────────────────────────────────────────────────┐
│  Layer 1: Agent 工具描述                                       │
│  "Launch a new agent to handle complex, multi-step tasks"     │
│  正向锚点 —— 告诉模型 Agent 的用途                             │
├──────────────────────────────────────────────────────────────┤
│  Layer 2: 系统提示使用指导                                      │
│  "Reach for it when research or multi-step work would         │
│   fill your context with raw output"                          │
│  场景引导 —— 告诉模型什么时候该用                               │
├──────────────────────────────────────────────────────────────┤
│  Layer 3: Verification 强制合约                                │
│  "when non-trivial implementation happens... verification     │
│   MUST happen before you report completion"                   │
│  硬性要求 —— 特定条件下必须用                                  │
├──────────────────────────────────────────────────────────────┤
│  Layer 4: Fork 模式零摩擦                                      │
│  "force ALL spawns async"                                     │
│  降低门槛 —— 模型不需要等待,用完即抛                            │
├──────────────────────────────────────────────────────────────┤
│  Layer 5: 反向约束 (When NOT to use)                           │
│  "If you want to read a specific file... use Read tool"       │
│  防止滥用 —— 简单操作必须直接调用                               │
├──────────────────────────────────────────────────────────────┤
│  Layer 6: 代码级路由 (AgentTool.call)                          │
│  fork vs general-purpose vs named agent vs verification       │
│  执行层 —— 解析 subagent_type, 应用 feature flags              │
└──────────────────────────────────────────────────────────────┘
```

**关键洞察**：这不是"if task == X then route to agent Y"的规则引擎，而是通过精心编排的 prompt 让 LLM 自己做路由判断。每一层 prompt 都是独立可替换的模块。

---

## 2. 路由决策全景架构

### 2.1 任务分类 vs 路由结果

| 任务类型 | 路由结果 | 触发机制 |
|---------|---------|---------|
| 读已知文件、搜类定义、小修改 | **不走 Agent** | Layer 5 反向约束 |
| 广泛代码库探索（3+ 查询） | **Agent(Explore)** | Layer 2 场景引导 |
| 复杂多步骤实施 | **Agent(general-purpose)** | Layer 1 锚点 + Layer 2 |
| 3+文件编辑/API变更/基础设施变更 | **Agent(verification)** | Layer 3 强制合约 |
| 大量搜索结果需上下文隔离 | **Agent(fork)** | Layer 2 + Layer 4 |
| 并行独立子任务 | **多个 Agent 并发** | Layer 2 并行激励 |
| 单次 WebFetch / 单次 Skill | **不走 Agent** | Layer 5 反向约束 |

### 2.2 决策流程图

```
用户任务
    │
    ▼
┌─────────────────────────────────────────┐
│ 模型评估 (基于所有 6 层 prompt)            │
│                                           │
│ Q1: 是否匹配 "When NOT to use" 列表?       │
│     ├ 是 → 直接调用 Read/Grep/Bash/Skill  │
│     └ 否 → Q2                             │
│                                           │
│ Q2: 是否"complex, multi-step"?            │
│     ├ 是 → Q3                             │
│     └ 否 → 直接调用对应工具                │
│                                           │
│ Q3: 是否需要上下文继承? (Fork 开启)        │
│     ├ 是 → Agent(fork:true)               │
│     └ 否 → Q4                             │
│                                           │
│ Q4: 是否"广泛探索 3+ 查询"?               │
│     ├ 是 → Agent(Explore)                 │
│     └ 否 → Q5                             │
│                                           │
│ Q5: 是否"非平凡实现"(3+文件)?              │
│     ├ 是 → Agent(general-purpose)         │
│     │     → 完成后 Agent(verification)     │
│     └ 否 → 直接在父循环中执行              │
│                                           │
│ Q6: 是否有独立并行子任务?                   │
│     └ 是 → 单条消息多个 Agent 调用          │
└─────────────────────────────────────────┘
```

---

## 3. 第一层：Agent 工具描述

### 3.1 核心 Prompt

这是 Agent 工具的 `description` 和 `prompt`，模型在工具列表中看到的内容。**这是路由决策的第一锚点**。

```text
# Agent 工具描述 (Tool Definition)

Launch a new agent to handle complex, multi-step tasks autonomously.

The Agent tool launches specialized agents (subprocesses) that autonomously
handle complex tasks. Each agent type has specific capabilities and tools
available to it.

Available agent types and the tools they have access to:
- general-purpose: General-purpose agent for researching complex questions,
  searching for code, and executing multi-step tasks.
  (Tools: *)
- Explore: Read-only search agent for broad fan-out searches — when answering
  means sweeping many files, directories, or naming conventions and you only
  need the conclusion, not the file dumps.
  (Tools: All tools except Agent, Edit, Write, NotebookEdit)
- Plan: Software architect agent for designing implementation plans.
  (Tools: All tools except Agent, Edit, Write, NotebookEdit)
- claude-code-guide: Use when the user asks questions about Claude Code, SDK,
  or API usage.
  (Tools: Glob, Grep, Read, WebFetch, WebSearch)
- verification: Adversarial verification agent for non-trivial implementations.
  (Tools: All tools)

When using the Agent tool, specify a subagent_type parameter to select which
agent type to use. If omitted, the general-purpose agent is used.
```

### 3.2 使用说明 (Usage notes)

```text
Usage notes:
- Always include a short description (3-5 words) summarizing what the agent
  will do
- Launch multiple agents concurrently whenever possible, to maximize
  performance; to do that, use a single message with multiple tool uses
- When the agent is done, it will return a single message back to you. The
  result returned by the agent is not visible to the user. To show the user
  the result, you should send a text message back to the user with a concise
  summary of the result.
- You can optionally run agents in the background using the run_in_background
  parameter. When an agent runs in the background, you will be automatically
  notified when it completes — do NOT sleep, poll, or proactively check on its
  progress. Continue with other work or respond to the user instead.
- Foreground vs background: Use foreground (default) when you need the agent's
  results before you can proceed. Use background when you have genuinely
  independent work to do in parallel.
- To continue a previously spawned agent, use SendMessage with the agent's ID
  or name as the `to` field. The agent resumes with its full context preserved.
- The agent's outputs should generally be trusted
- Clearly tell the agent whether you expect it to write code or just to do
  research (search, file reads, web fetches, etc.)
- If the agent description mentions that it should be used proactively, then
  you should try your best to use it without the user having to ask for it
  first. Use your judgement.
- If the user specifies that they want you to run agents "in parallel", you
  MUST send a single message with multiple Agent tool use content blocks.
- You can optionally set isolation: "worktree" to run the agent in a temporary
  git worktree, giving it an isolated copy of the repository.
```

### 3.3 Prompt 编写指导 (Writing the prompt)

```text
## Writing the prompt

Brief the agent like a smart colleague who just walked into the room — it
hasn't seen this conversation, doesn't know what you've tried, doesn't
understand why this task matters.

- Explain what you're trying to accomplish and why, what you've already
  learned or ruled out, and enough context for the agent to make judgment
  calls.
- If you need a short response, say so ("report in under 200 words").
- Lookups: hand over the exact command. Investigations: hand over the
  question — prescribed steps become dead weight when the premise is wrong.

Terse command-style prompts produce shallow, generic work.

**Never delegate understanding.** Don't write "based on your findings, fix
the bug" or "based on the research, implement it." Write prompts that prove
you understood: include file paths, line numbers, what specifically to change.
```

### 3.4 Fork 模式专项 (When to fork)

```text
## When to fork

When you need to delegate work that benefits from full conversation context
(e.g., continuing a multi-file refactor where the child needs the same system
prompt and history), use `fork: true`. For most tasks, prefer specialized
agent types (Explore, Plan, general-purpose).

**Don't peek.** The tool result includes an `output_file` path — do not Read
or tail it unless the user explicitly asks for a progress check. You get a
completion notification; trust it.

**Don't race.** After launching, you know nothing about what the fork found.
Never fabricate or predict fork results. If the user asks a follow-up before
the notification lands, tell them the fork is still running.

**Writing a fork prompt.** Since the fork inherits your context, the prompt is
a *directive* — what to do, not what the situation is. Be specific about scope.
Don't re-explain background.
```

---

## 4. 第二层：系统提示中的使用指导

### 4.1 核心 Agent 使用段落

这是嵌入在系统提示（system prompt）中的段落，出现在模型每次 API 调用的上下文开头。

#### Fork 开启时

```text
# Using your tools

Calling Agent without a subagent_type creates a fork, which runs in the
background and keeps its tool output out of your context — so you can keep
chatting with the user while it works. Reach for it when research or
multi-step implementation work would otherwise fill your context with raw
output you won't need again.

**If you ARE the fork** — execute directly; do not re-delegate.
```

#### Fork 关闭时

```text
# Using your tools

Use the Agent tool with specialized agents when the task at hand matches
the agent's description. Subagents are valuable for parallelizing independent
queries or for protecting the main context window from excessive results, but
they should not be used excessively when not needed. Importantly, avoid
duplicating work that subagents are already doing - if you delegate research
to a subagent, do not also perform the same searches yourself.
```

### 4.2 探索型 Agent 专项指导

```text
For simple, directed codebase searches (e.g. for a specific file/class/function)
use grep or glob directly.

For broader codebase exploration and deep research, use the Agent tool with
subagent_type=Explore. This is slower than using search tools directly, so use
this only when a simple, directed search proves to be insufficient or when your
task will clearly require more than 3 queries.
```

### 4.3 并行激励

```text
Launch multiple agents concurrently whenever possible, to maximize performance;
to do that, use a single message with multiple tool uses.
```

---

## 5. 第三层：Verification 强制合约

这是唯一一个接近"硬编码"的机制——使用强制性语言（MUST）。

### 5.1 合约文本

```text
The contract: when non-trivial implementation happens on your turn,
independent adversarial verification must happen before you report completion
— regardless of who did the implementing (you directly, a fork you spawned,
or a subagent). You are the one reporting to the user; you own the gate.

Non-trivial means: 3+ file edits, backend/API changes, or infrastructure
changes.

Spawn the Agent tool with subagent_type="verification".

Your own checks, caveats, and a fork's self-checks do NOT substitute — only
the verifier assigns a verdict; you cannot self-assign PARTIAL.

Pass the original user request, all files changed (by anyone), the approach,
and the plan file path if applicable.

Flag concerns if you have them but do NOT share test results or claim things
work.

On FAIL: fix, resume the verifier with its findings plus your fix, repeat
until PASS.

On PASS: spot-check it — re-run 2-3 commands from its report, confirm every
PASS has a Command run block with output that matches your re-run. If any PASS
lacks a command block or diverges, resume the verifier with the specifics.

On PARTIAL (from the verifier): report what passed and what could not be
verified.
```

### 5.2 Verification Agent 的核心特质

```text
You are an adversarial verification agent. Your job is to independently
verify implementation changes, NOT to approve them.

Core principles:
1. Never trust claims — run actual commands and inspect actual output
2. Assume bugs exist — actively search for them
3. Watch for these rationalization patterns:
   - "The code looks correct" → Run it
   - "This matches the pattern" → Test edge cases
   - "The logic is sound" → Execute and check output
   - "It should work" → Verify it does
   - "The error is unrelated" → Prove it
   - "This is a simple change" → Simple changes cause the most bugs
4. Every finding MUST be backed by a command you actually ran with its output
5. Return a VERDICT: PASS (verified working) | FAIL (found issues) |
   PARTIAL (could not fully verify)
```

### 5.3 触发条件（代码层）

```typescript
// 三层 AND 条件
const verificationEnabled =
  feature('VERIFICATION_AGENT') &&                          // Build flag
  getFeatureValue('tengu_hive_evidence', false) &&          // A/B experiment
  !isPoorModeActive();                                       // Not budget mode

// 如果全部满足,合约文本被注入 system prompt
// 模型自主判断"non-trivial"标准: 3+文件 / 后端变更 / 基础设施变更
```

---

## 6. 第四层：Fork 模式零摩擦路由

Fork 模式是单 Agent → 多 Agent 转换率的最大提升器。

### 6.1 核心改动

```typescript
// AgentTool.tsx — 路由逻辑
const effectiveType = subagent_type
  ?? (isForkSubagentEnabled() ? undefined : GENERAL_PURPOSE_AGENT.agentType);

const isForkPath = effectiveType === undefined;
```

**Fork 关闭时**: 模型不传 `subagent_type` → 默认 `general-purpose` → 正常子 Agent 流程

**Fork 开启时**: 模型不传 `subagent_type` → `undefined` → 走 fork path → 继承父上下文

### 6.2 强制异步

```typescript
// Fork 模式下所有 Agent 强制异步
const forceAsync = isForkSubagentEnabled();
```

效果：模型发送 Agent 调用 → 立刻拿到 `{ status: "async_launched", agentId: "..." }` → 不阻塞 → 继续工作 → 子 Agent 完成时收到 `<task-notification>`

### 6.3 同步→异步 自动降级

即使不走 Fork 路径，同步 Agent 运行超过 120 秒会自动后台化：

```typescript
function getAutoBackgroundMs(): number {
  if (isEnvTruthy('CLAUDE_AUTO_BACKGROUND_TASKS') ||
      getFeatureValue('tengu_auto_background_agents', false)) {
    return 120_000; // 120 秒
  }
  return 0; // 禁用
}

// 运行时: Promise.race(agentMessage, backgroundSignal)
// backgroundSignal 在 120s 后 resolve
// → agent 转为后台运行, 父 Agent 立刻恢复控制
```

### 6.4 Fork Agent 的虚拟定义

```typescript
// 不在 builtInAgents 中注册, 仅用于 fork path
const FORK_AGENT = {
  agentType: 'fork',
  whenToUse: 'Implicit fork — inherits full conversation context.',
  tools: ['*'],                   // 继承父工具池
  maxTurns: 200,
  model: 'inherit',               // 继承父模型
  permissionMode: 'bubble',       // 权限提示上浮到父终端
  source: 'built-in',
  getSystemPrompt: () => '',      // 不使用, 直接用父 system prompt
};
```

---

## 7. 第五层：反向约束（When NOT to use）

### 7.1 明确禁止的场景

```text
When NOT to use the Agent tool:
- If you want to read a specific file path, use the Read tool or glob instead
  of the Agent tool, to find the match more quickly
- If you are searching for a specific class definition like "class Foo", use
  grep instead, to find the match more quickly
- If you are searching for code within a specific file or set of 2-3 files,
  use the Read tool instead of the Agent tool, to find the match more quickly
- Other tasks that are not related to the agent descriptions above
```

### 7.2 性能提示

```text
Agent(Explore) is slower than using search tools directly, so use this only
when a simple, directed search proves to be insufficient or when your task
will clearly require more than 3 queries.
```

### 7.3 Fork 的反向约束（防止递归爆炸）

```typescript
// 代码层: Fork 子进程不能再 fork
if (toolUseContext.options.querySource === `agent:builtin:fork` ||
    isInForkChild(toolUseContext.messages)) {
  throw new Error(
    'Fork is not available inside a forked worker. ' +
    'Complete your task directly using your tools.'
  );
}

// Teammate 不能再 spawn Teammate
if (isTeammate() && teamName && name) {
  throw new Error(
    'Teammates cannot spawn other teammates.'
  );
}
```

---

## 8. 第六层：代码级路由逻辑

### 8.1 AgentTool.call() 路由决策

```typescript
async function agentToolCall(input, context) {
  const { prompt, subagent_type, description, model, run_in_background } = input;

  // ====== 路由决策 1: subagent_type 解析 ======
  const effectiveType = subagent_type
    ?? (isForkSubagentEnabled() ? undefined : 'general-purpose');

  const isForkPath = (effectiveType === undefined);

  // ====== 路由决策 2: Agent 定义查找 ======
  let selectedAgent: AgentDefinition;
  if (isForkPath) {
    // 递归防护: fork 内不能再 fork
    if (isInForkChild(context.messages)) {
      throw new Error('Fork not available inside fork.');
    }
    selectedAgent = FORK_AGENT;
  } else {
    // 从 agentDefinitions 中按 agentType 查找
    selectedAgent = agentDefinitions.find(a => a.agentType === effectiveType);
    if (!selectedAgent) {
      throw new Error(`Agent type '${effectiveType}' not found.`);
    }
  }

  // ====== 路由决策 3: MCP 依赖检查 ======
  if (selectedAgent.requiredMcpServers?.length) {
    if (!hasRequiredMcpServers(selectedAgent, availableMcpServers)) {
      throw new Error(`Agent requires MCP servers: ${missing.join(', ')}`);
    }
  }

  // ====== 路由决策 4: 同步/异步 ======
  const shouldRunAsync =
    (run_in_background === true) ||          // 模型显式指定
    (selectedAgent.background === true) ||   // Agent 定义要求
    isCoordinatorMode() ||                   // 协调者模式
    isForkSubagentEnabled() ||               // Fork 模式强制异步
    kairosEnabled ||                         // KAIROS 助手模式
    (isProactiveActive() ?? false);           // 主动模式

  // ====== 路由决策 5: 工作区隔离 ======
  const effectiveIsolation = input.isolation ?? selectedAgent.isolation;
  if (effectiveIsolation === 'worktree') {
    worktreeInfo = await createAgentWorktree(slug);
  }

  // ====== 路由决策 6: 工具池 ======
  let tools;
  if (isForkPath) {
    // Fork: 继承父工具的过滤子集 (useExactTools)
    tools = filterParentToolsForFork(parentTools);
  } else {
    // 非 Fork: 独立组装, 使用 agent 的 permissionMode
    tools = assembleToolPool(agentPermissionContext, mcpTools);
  }

  // ====== 路由决策 7: 执行 ======
  if (shouldRunAsync) {
    // 异步路径: 注册 background task → 立即返回 async_launched
    registerAsyncAgent({ agentId, description, ... });
    void runAgent({ ...runAgentParams, isAsync: true });
    return { status: 'async_launched', agentId, outputFile };
  } else {
    // 同步路径: 阻塞等待, 支持 auto-background (120s timeout)
    const agentIterator = runAgent({ ...runAgentParams });
    // Promise.race(agentIterator.next(), backgroundSignal)
    return { status: 'completed', ... };
  }
}
```

### 8.2 路由决策优先级总结

| 优先级 | 决策点 | 判断因素 | 结果 |
|--------|--------|---------|------|
| 1 | `subagent_type` 解析 | 模型传了什么 / Fork 是否开启 | fork / general-purpose / named agent |
| 2 | 递归防护 | 当前是否在 fork/teammate 中 | throw Error |
| 3 | Agent 定义查找 | `agentType` 匹配 | 找到 AgentDefinition |
| 4 | MCP 依赖 | `requiredMcpServers` | throw Error 如果缺失 |
| 5 | 同步/异步 | 5 个 bool OR | sync vs async execution |
| 6 | 工作区隔离 | `isolation` param / agent def | worktree / none / remote |
| 7 | 工具池 | fork vs non-fork | inherit vs assemble |
| 8 | 执行 | 同步 vs 异步 | 阻塞等待 vs fire-and-forget |

---

## 9. 完整 Prompt 模板组装

### 9.1 组装顺序

```
┌─────────────────────────────────────┐
│ System Prompt (发送给模型的完整上下文)  │
│                                     │
│ ┌─────────────────────────────────┐ │
│ │ # Using your tools              │ │ ← Layer 2: getAgentToolSection()
│ │ (Fork 或非 Fork 版本)            │ │
│ ├─────────────────────────────────┤ │
│ │ Core tools (Read, Edit, Write,  │ │
│ │  Glob, Grep, Bash, Agent,       │ │
│ │  WebFetch, WebSearch, ...)      │ │
│ ├─────────────────────────────────┤ │
│ │ # Session-specific guidance     │ │ ← Layer 2: Explore 指导
│ │ For simple searches use grep    │ │   Layer 3: Verification 合约
│ │ For broad exploration use       │ │
│ │ Agent(Explore)...               │ │
│ │ The contract: when non-trivial  │ │
│ │ implementation happens...       │ │
│ └─────────────────────────────────┘ │
│                                     │
│ ┌─────────────────────────────────┐ │
│ │ Tool Definitions (发送给模型的    │ │
│ │ 工具列表)                         │ │
│ │                                  │ │
│ │ AgentTool:                       │ │ ← Layer 1: Tool description
│ │   description: "Launch a new     │ │   Layer 5: When NOT to use
│ │   agent to handle complex,       │ │   Layer 4: When to fork
│ │   multi-step tasks..."           │ │
│ │   prompt: "..."                  │ │
│ │                                  │ │
│ │ WebFetchTool, ReadTool, ...      │ │
│ └─────────────────────────────────┘ │
│                                     │
│ ┌─────────────────────────────────┐ │
│ │ Agent Listing (可选 attachment)  │ │ ← Agent 类型列表 + whenToUse
│ │ - general-purpose: ...          │ │
│ │ - Explore: ...                  │ │
│ │ - Plan: ...                     │ │
│ └─────────────────────────────────┘ │
└─────────────────────────────────────┘
```

### 9.2 可配置参数

在复现时，以下是可以通过配置文件开关的维度：

```yaml
# agent-routing-config.yaml — 复现参考配置

routing:
  # Layer 1: Agent 工具描述
  agent_tool:
    description: "Launch a new agent to handle complex, multi-step tasks autonomously."
    usage_notes:
      concurrency: "Launch multiple agents concurrently whenever possible"
      background: "Use background when you have genuinely independent work"
      trust: "The agent's outputs should generally be trusted"

  # Layer 2: 系统提示
  system_prompt:
    fork_enabled_version: >
      "Calling Agent without a subagent_type creates a fork, which runs in
       the background and keeps its tool output out of your context."
    fork_disabled_version: >
      "Subagents are valuable for parallelizing independent queries or for
       protecting the main context window from excessive results, but they
       should not be used excessively."

  # Layer 3: 强制合约
  verification:
    enabled: false  # 通过 feature flag 控制
    contract: >
      "when non-trivial implementation happens... independent adversarial
       verification MUST happen before you report completion"
    threshold:
      file_edits: 3
      changes: ["backend", "api", "infrastructure"]

  # Layer 4: Fork 模式
  fork:
    enabled: false  # FORK_SUBAGENT feature flag
    force_async: true
    auto_background_ms: 120000

  # Layer 5: 反向约束
  when_not_to_use:
    - condition: "read specific file"
      alternative: "Read tool"
    - condition: "search class definition"
      alternative: "Grep tool"
    - condition: "search within 2-3 files"
      alternative: "Read tool"

  # Layer 6: 代码路由
  code_routing:
    subagent_type_resolution:
      explicit: "use as-is"
      omitted_with_fork: "undefined → fork path"
      omitted_without_fork: "general-purpose"
    async_triggers:
      - run_in_background_param
      - agent_definition_background
      - fork_mode
      - coordinator_mode
      - kairos_mode
    recursion_guard:
      fork_in_fork: "throw Error"
      teammate_spawn_teammate: "throw Error"
```

---

## 10. Feature Flag 等价实现

这些是控制路由行为的开关。在复现时可以用环境变量或配置文件替代。

```typescript
// ====== 路由相关 Feature Flags ======

// 1. FORK_SUBAGENT — 最强的路由影响者
// 开启后: 模型省略 subagent_type → fork path → 继承父上下文 → 强制异步
// 关闭后: 模型省略 subagent_type → general-purpose → 正常子 Agent
const FORK_SUBAGENT_ENABLED = process.env.ENABLE_FORK_SUBAGENT === 'true';

// 2. VERIFICATION_AGENT — 强制验证合约
// 开启后: 合约文本注入 system prompt
// 关闭后: 无合约文本
const VERIFICATION_AGENT_ENABLED = process.env.ENABLE_VERIFICATION === 'true';

// 3. BUILTIN_EXPLORE_PLAN_AGENTS — Explore/Plan Agent 可用性
// 开启后: Explore/Plan 加入 Agent 列表 + system prompt 中有使用指导
// 关闭后: 只有 general-purpose 可用
const EXPLORE_PLAN_ENABLED = process.env.ENABLE_EXPLORE_PLAN === 'true';

// 4. AUTO_BACKGROUND — 同步→异步自动降级
// 开启后: 同步 Agent 超 120s 自动转后台
// 关闭后: 同步 Agent 永远阻塞
const AUTO_BACKGROUND_MS = process.env.AUTO_BACKGROUND === 'true' ? 120000 : 0;

// 5. POOR_MODE — 穷鬼模式 (反向影响)
// 开启后: 跳过 verification + memory extraction + prompt suggestion
const POOR_MODE = process.env.POOR_MODE === 'true';
```

### 10.1 不同配置组合的效果

| 配置 | Agent 使用率 | 适用场景 |
|------|-------------|---------|
| Fork ON + Verify ON + Explore ON | **最高** — 几乎所有多步任务走 Agent | 企业版全功能 |
| Fork ON + Verify OFF + Explore ON | **高** — 多步任务走 fork, 跳过验证 | 默认配置 |
| Fork OFF + Verify OFF + Explore ON | **中** — 仅显式 Agent 调用 | 标准配置 |
| Fork OFF + Verify OFF + Explore OFF | **低** — 仅 general-purpose | 最小化配置 |
| Poor Mode ON | **极低** — 跳过所有附加 Agent | Token 节省模式 |

---

## 11. 分阶段复现路线图

### Phase 1: 最小可行路由（1-2 天）

**目标**：模型能自主决定是否使用 Agent

**交付物**：
1. 一个 Agent 工具定义 + prompt
2. 一个 general-purpose Agent 实现
3. System prompt 中的基础使用指导

```
# 最小 prompt 模板

## Agent 工具定义
"Launch a new agent to handle complex, multi-step tasks autonomously.
Available agents: general-purpose (Tools: *)"

## System prompt 注入
"Use the Agent tool when the task is complex and multi-step.
Do NOT use it for simple file reads, single searches, or small edits."

## When NOT to use
"- Reading a specific file → use Read
 - Searching for a class → use Grep
 - Searching within 2-3 files → use Read"
```

### Phase 2: 类型化 Agent + 场景路由（3-5 天）

**目标**：不同任务类型走不同的 Agent

**交付物**：
1. Explore Agent (read-only, haiku)
2. Plan Agent (read-only, design focus)
3. Agent 类型列表注入 system prompt
4. Explore 使用指导 ("3+ queries → Explore")

```
# Explore 指导
"For simple directed searches use grep/glob directly.
 For broader exploration (3+ queries), use Agent(Explore).
 This is slower, so use only when simple search is insufficient."
```

### Phase 3: Fork 模式 + 异步化（5-7 天）

**目标**：降低模型使用 Agent 的心理门槛

**交付物**：
1. Fork 子 Agent（继承父上下文 + prompt cache 共享）
2. 强制异步执行（模型不等待）
3. `<task-notification>` 完成通知机制
4. 递归防护（fork 内不能再 fork）

```
# Fork 模式指导
"Calling Agent without a subagent_type creates a fork, which runs in the
 background and keeps its output out of your context. Reach for it when
 research or multi-step implementation work would otherwise fill your
 context with raw output you won't need again."
```

### Phase 4: Verification 合约 + 并行优化（3-5 天）

**目标**：特定条件下强制多 Agent，最大化并行

**交付物**：
1. Verification Agent（对抗性验证）
2. 合约文本注入 system prompt
3. 并行激励 ("single message with multiple tool uses")
4. Auto-background (超时降级)

```
# Verification 合约
"The contract: when non-trivial implementation happens (3+ file edits,
 backend/API changes, infrastructure changes), independent adversarial
 verification MUST happen before you report completion."
```

### Phase 5: 生产打磨（持续）

**交付物**：
1. A/B 实验框架（GrowthBook 等价物）
2. 路由决策的可观测性（analytics/logging）
3. 反向约束调优（什么时候不该用 Agent）
4. Token 预算感知路由（穷鬼模式下减少 Agent 使用）

---

## 附录 A：关键代码文件索引

| 文件 | 内容 |
|------|------|
| `packages/builtin-tools/src/tools/AgentTool/prompt.ts` | Agent 工具描述 + 使用指导 + Fork/非Fork 分支 |
| `packages/builtin-tools/src/tools/AgentTool/AgentTool.tsx` | AgentTool.call() 路由逻辑 + sync/async 决策 |
| `src/constants/prompts.ts` (getAgentToolSection) | System prompt 中的 Agent 使用段落 |
| `src/constants/prompts.ts` (getSessionSpecificGuidanceSection) | Explore 指导 + Verification 合约 |
| `packages/builtin-tools/src/tools/AgentTool/forkSubagent.ts` | Fork Agent 定义 + isForkSubagentEnabled |
| `packages/builtin-tools/src/tools/AgentTool/builtInAgents.ts` | 内置 Agent 注册 + feature gate |
| `packages/builtin-tools/src/tools/AgentTool/built-in/verificationAgent.ts` | Verification Agent 系统提示 |
| `packages/builtin-tools/src/tools/AgentTool/built-in/exploreAgent.ts` | Explore Agent 系统提示 |

## 附录 B：关键环境变量

| 变量 | 作用 |
|------|------|
| `FEATURE_FORK_SUBAGENT=1` | 启用 Fork 模式 |
| `FEATURE_VERIFICATION_AGENT=1` | 启用 Verification Agent |
| `FEATURE_BUILTIN_EXPLORE_PLAN_AGENTS=1` | 启用 Explore/Plan Agent |
| `CLAUDE_AUTO_BACKGROUND_TASKS=1` | 启用 120s 自动后台化 |
| `CLAUDE_CODE_DISABLE_BACKGROUND_TASKS=1` | 禁用所有后台任务 |
| `CLAUDE_CODE_AGENT_LIST_IN_MESSAGES=1` | Agent 列表作为 attachment 而非内联 |
