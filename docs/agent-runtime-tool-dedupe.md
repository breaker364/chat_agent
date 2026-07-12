# Agent Runtime 工具调用去重与结果复用方案

## 1. 背景

当 LLM API 被用作 Agent 的“大脑”时，模型可能产生两类冗余工具调用：

1. **同一个 API 响应中发出两条完全相同的工具调用**
2. **上一轮刚调用完工具并获得结果，下一轮又重复调用相同工具**

即使历史工具调用结果已经完整进入下一轮上下文，模型仍然可能因为注意力遗漏、规划冗余、并行调用习惯或提示词约束不足而重复请求工具。

因此，不能把“避免重复调用”完全交给模型本身，必须在 **Agent Runtime 层** 提供确定性的去重、缓存、幂等和预算控制机制。

## 2. 目标

### 2.1 核心目标

- 避免同一轮响应内重复执行完全相同的工具调用。
- 避免跨轮对话重复执行已有有效结果的工具调用。
- 降低 token、网络、计算和外部 API 成本。
- 防止副作用工具因重复调用造成重复写入、重复发送、重复下单等问题。
- 在结果过期或用户明确要求刷新时，允许重新调用工具。

### 2.2 非目标

- 不依赖模型保证工具调用唯一性。
- 不要求所有工具结果永久缓存。
- 不把所有重复调用都视为错误，时间敏感或状态变化类工具可按 TTL 重新执行。
- 不用提示词替代 Runtime 层的确定性控制。

## 3. 问题分析

### 3.1 同一响应内重复工具调用

模型可能一次返回多个工具调用，例如：

```json
[
  {
    "name": "search",
    "arguments": { "query": "OpenAI Agents SDK" }
  },
  {
    "name": "search",
    "arguments": { "query": "OpenAI Agents SDK" }
  }
]
```

如果 Runtime 不做拦截，会导致同一个工具被执行两次。

常见根因：

- 模型生成是概率式的，不具备唯一性约束。
- 并行工具调用时，模型可能为多个子任务生成相同查询。
- 提示词没有明确禁止重复调用。
- Runtime 缺少 `tool_name + args` 级别的去重机制。

### 3.2 跨轮重复工具调用

上一轮已经执行：

```json
{
  "name": "get_user_profile",
  "arguments": { "user_id": "123" }
}
```

下一轮上下文中已经包含结果，但模型仍可能再次请求相同工具。

常见根因：

- 模型不一定稳定利用长上下文中的工具结果。
- 工具结果缺少结构化索引，模型难以识别“这个结果已经存在”。
- Runtime 没有在执行前检查历史结果缓存。
- 工具结果没有 TTL、版本或 freshness 策略。
- 工具调用结果只作为自然语言上下文存在，而没有进入可检索状态表。

## 4. 总体方案

Agent Runtime 应增加四层防线：

1. **同轮工具调用去重**
2. **跨轮工具结果缓存**
3. **执行前重复调用拦截**
4. **副作用工具幂等保护**

推荐架构：

```text
用户输入
  ↓
构造模型上下文
  ↓
注入已有工具结果摘要
  ↓
调用 LLM
  ↓
解析 tool_calls
  ↓
同轮去重
  ↓
执行前查缓存 / 查历史结果
  ↓
复用结果或真实执行工具
  ↓
写入工具调用日志与缓存
  ↓
返回工具结果给模型
  ↓
生成最终回答
```

## 5. 核心设计

### 5.1 工具调用唯一键

Runtime 必须为每个工具调用生成稳定唯一键：

```text
call_key = tool_name + ":" + canonical_json(arguments)
```

其中 `canonical_json(arguments)` 必须满足：

- JSON key 稳定排序。
- 去除无意义空白。
- 统一数字、布尔、null 表达。
- 可选：对等价参数做归一化，例如 URL 去尾部 `/`。

示例：

```ts
function getCallKey(toolName: string, args: unknown): string {
  return `${toolName}:${stableStringify(args)}`;
}
```

示例结果：

```text
search:{"query":"OpenAI Agents SDK"}
get_user_profile:{"user_id":"123"}
```

### 5.2 同轮工具调用去重

当模型一次返回多个工具调用时，Runtime 在执行前先做去重。

处理规则：

- 对所有 `tool_calls` 计算 `call_key`。
- 如果同一响应内出现重复 `call_key`，只真实执行第一条。
- 后续重复调用复用第一条结果。
- 返回给模型时仍保持每个 `tool_call_id` 都有对应结果，避免协议断裂。

示例实现：

```ts
async function executeToolCallsWithDedupe(
  calls: ToolCall[],
  state: AgentState
): Promise<ToolResult[]> {
  const sameResponseExecutions = new Map<string, Promise<ToolResult>>();
  const firstCallByKey = new Map<string, string>();
  const results: ToolResult[] = [];

  for (const call of calls) {
    const key = getCallKey(call.name, call.arguments);

    if (!sameResponseExecutions.has(key)) {
      firstCallByKey.set(key, call.id);
      sameResponseExecutions.set(
        key,
        executeToolCallWithCacheAndPolicy(call, state)
      );
    }

    const result = await sameResponseExecutions.get(key)!;
    const firstCallId = firstCallByKey.get(key)!;

    results.push({
      role: "tool",
      tool_call_id: call.id,
      name: call.name,
      content: result.content,
      metadata: {
        call_key: key,
        reused: call.id !== firstCallId || result.metadata?.reused === true,
        reused_from_tool_call_id:
          call.id !== firstCallId
            ? firstCallId
            : result.metadata?.reused_from_tool_call_id,
        dedupe_scope:
          call.id !== firstCallId
            ? "same_response"
            : result.metadata?.dedupe_scope ?? "none"
      }
    });
  }

  return results;
}
```

### 5.3 跨轮工具结果缓存

Runtime 需要维护一个结构化缓存，而不是只依赖聊天上下文。

推荐缓存结构：

```ts
type ToolResultCacheEntry = {
  call_key: string;
  tool_name: string;
  arguments: unknown;
  content: string;
  structured_content?: unknown;
  tool_call_id: string;
  created_at: number;
  expires_at?: number;
  source_turn_id: string;
  side_effect: boolean;
  status: "success" | "failed";
  error_type?: string;
};
```

推荐缓存层级：

| 层级 | 生命周期 | 适用场景 |
| --- | --- | --- |
| Same-response cache | 单次模型响应 | 防止同一响应内重复工具调用 |
| Conversation cache | 当前会话 | 防止跨轮重复查同一结果 |
| Persistent cache | 跨会话 | 适合稳定公开资料、静态配置、低变更数据 |

默认至少实现前两层。

### 5.4 执行前缓存拦截

所有工具调用在真实执行前，必须先检查缓存。

```ts
async function executeToolCallWithCacheAndPolicy(
  call: ToolCall,
  state: AgentState
): Promise<ToolResult> {
  const key = getCallKey(call.name, call.arguments);
  const policy = getToolPolicy(call.name);

  if (policy.side_effect) {
    return executeSideEffectToolWithIdempotency(call, state, policy);
  }

  const cached = state.toolResultCache.get(key);

  if (cached && canReuseToolResult(cached, policy, state)) {
    logToolExecution({
      call,
      key,
      action: "reused",
      dedupe_scope: "conversation_cache",
      reused_from_tool_call_id: cached.tool_call_id
    });

    return {
      role: "tool",
      tool_call_id: call.id,
      name: call.name,
      content: cached.content,
      metadata: {
        call_key: key,
        reused: true,
        reused_from_tool_call_id: cached.tool_call_id,
        reused_from_turn_id: cached.source_turn_id,
        dedupe_scope: "conversation_cache"
      }
    };
  }

  const result = await actuallyExecuteTool(call);

  saveToolResultToCache(call, result, key, policy, state);

  logToolExecution({
    call,
    key,
    action: "executed"
  });

  return result;
}
```

## 6. 工具策略配置

不同工具应有不同复用策略。

```ts
type ToolPolicy = {
  cacheable: boolean;
  ttl_ms?: number;
  side_effect: boolean;
  allow_stale?: boolean;
  require_confirmation_on_repeat?: boolean;
  dedupe_scope: "same_response" | "conversation" | "persistent" | "none";
};
```

示例配置：

```ts
const toolPolicies: Record<string, ToolPolicy> = {
  search: {
    cacheable: true,
    ttl_ms: 10 * 60 * 1000,
    side_effect: false,
    dedupe_scope: "conversation"
  },

  get_user_profile: {
    cacheable: true,
    ttl_ms: 5 * 60 * 1000,
    side_effect: false,
    dedupe_scope: "conversation"
  },

  get_current_weather: {
    cacheable: true,
    ttl_ms: 2 * 60 * 1000,
    side_effect: false,
    dedupe_scope: "conversation"
  },

  send_email: {
    cacheable: false,
    side_effect: true,
    require_confirmation_on_repeat: true,
    dedupe_scope: "same_response"
  },

  create_order: {
    cacheable: false,
    side_effect: true,
    require_confirmation_on_repeat: true,
    dedupe_scope: "same_response"
  }
};
```

## 7. Freshness 与 TTL 策略

不是所有缓存结果都能永久复用。

### 7.1 推荐 TTL

| 工具类型 | TTL 建议 |
| --- | --- |
| 静态配置查询 | 1 小时到 24 小时 |
| 代码仓库文件读取 | 当前任务内长期有效，文件修改后失效 |
| Web 搜索 | 5 到 30 分钟 |
| 天气、价格、库存 | 30 秒到 5 分钟 |
| 用户资料查询 | 1 到 10 分钟 |
| 数据库读查询 | 按业务一致性要求设置 |
| 副作用工具 | 不缓存结果用于重放，只记录幂等日志 |

### 7.2 允许重新调用的情况

以下情况可以绕过缓存：

- 用户明确说“重新查询”“刷新”“获取最新”。
- 缓存已过期。
- 上一次结果失败且错误可重试。
- 工具参数变化。
- 上游数据版本变化。
- 当前任务要求强一致性。
- 安全策略要求重新验证。

示例：

```ts
function canReuseToolResult(
  cached: ToolResultCacheEntry,
  policy: ToolPolicy,
  state: AgentState
): boolean {
  if (!policy.cacheable) return false;
  if (state.userRequestedRefresh) return false;
  if (cached.status !== "success") return false;

  if (cached.expires_at && Date.now() > cached.expires_at) {
    return false;
  }

  return true;
}
```

## 8. 副作用工具幂等保护

对于有副作用的工具，去重不能只靠缓存，而必须有幂等控制。

### 8.1 副作用工具示例

- 发邮件。
- 发短信。
- 创建订单。
- 扣款。
- 写数据库。
- 删除文件。
- 创建工单。
- 发布消息。
- 调用外部 Webhook。

### 8.2 幂等键设计

同轮防重：

```text
idempotency_key = conversation_id + turn_id + tool_name + canonical_json(arguments)
```

跨轮业务防重：

```text
idempotency_key = user_id + business_action + canonical_json(arguments)
```

示例：

```ts
function getIdempotencyKey(call: ToolCall, state: AgentState): string {
  return stableHash({
    conversation_id: state.conversationId,
    turn_id: state.currentTurnId,
    tool_name: call.name,
    arguments: call.arguments
  });
}
```

### 8.3 副作用工具执行规则

| 场景 | 处理方式 |
| --- | --- |
| 同一响应内重复副作用调用 | 只执行一次，其余返回已拦截 |
| 跨轮重复副作用调用 | 默认拒绝或要求用户确认 |
| 用户明确要求再次执行 | 使用新的业务幂等键或确认令牌 |
| 工具超时但状态未知 | 查询外部状态，不直接重试 |
| 支付、下单、删除类工具 | 必须强制幂等，不允许裸重试 |

## 9. 上下文注入策略

即使 Runtime 有缓存，也建议在调用模型前注入已有结果摘要，减少模型再次请求工具的概率。

### 9.1 注入内容示例

```text
已可复用工具结果：
1. call_key: search:{"query":"OpenAI Agents SDK"}
   工具: search
   时间: 2026-07-09T10:00:00+08:00
   状态: success
   结果摘要: OpenAI Agents SDK 文档包含 Runner、Agent、Tool 等核心概念。
   复用规则: 若当前问题可由该结果回答，不要再次调用相同工具。
```

### 9.2 注意事项

- 注入摘要，不要注入过长原始结果。
- 保留 `call_key`，便于模型引用。
- 标记结果时间和是否过期。
- 对过期结果明确提示“需要刷新才可使用”。
- 对副作用工具结果只说明已执行，不鼓励重复执行。

## 10. 执行日志与审计

Runtime 应记录每次工具调用的处理结果。

```ts
type ToolExecutionLog = {
  timestamp: number;
  conversation_id: string;
  turn_id: string;
  tool_call_id: string;
  tool_name: string;
  arguments_hash: string;
  call_key: string;
  action: "executed" | "reused" | "deduped" | "blocked" | "failed";
  dedupe_scope?: "same_response" | "conversation_cache" | "persistent_cache";
  reused_from_tool_call_id?: string;
  latency_ms?: number;
  token_saving_estimate?: number;
  error?: string;
};
```

示例日志：

```json
{
  "timestamp": 1783572000000,
  "conversation_id": "conv_123",
  "turn_id": "turn_8",
  "tool_call_id": "call_abc",
  "tool_name": "search",
  "arguments_hash": "sha256:...",
  "call_key": "search:{\"query\":\"OpenAI Agents SDK\"}",
  "action": "reused",
  "dedupe_scope": "conversation_cache",
  "reused_from_tool_call_id": "call_prev_001"
}
```

## 11. 错误处理策略

### 11.1 可缓存错误

部分错误可以短时间缓存，避免模型不断重试。

| 错误类型 | 是否缓存 | TTL |
| --- | --- | --- |
| 404 / Not Found | 是 | 5 到 30 分钟 |
| 权限不足 | 是 | 当前会话 |
| 参数非法 | 是 | 当前会话 |
| 429 限流 | 是 | 按 Retry-After |
| 5xx | 可短暂缓存 | 2 到 10 秒 |
| 网络超时 | 可短暂缓存 | 2 到 10 秒 |

### 11.2 失败结果结构

```ts
state.toolResultCache.set(key, {
  call_key: key,
  tool_name: call.name,
  arguments: call.arguments,
  content: error.message,
  tool_call_id: call.id,
  created_at: Date.now(),
  expires_at: Date.now() + errorTtlMs,
  source_turn_id: state.currentTurnId,
  side_effect: policy.side_effect,
  status: "failed",
  error_type: error.type
});
```

## 12. Prompt 配合建议

Runtime 是主防线，Prompt 是辅助防线。

可以在系统提示词中加入：

```text
工具调用规则：
1. 如果上下文中已有相同工具名和相同参数的有效结果，必须直接复用，不得重复调用。
2. 不要在同一轮响应中生成重复的工具调用。
3. 只有当用户明确要求刷新、结果过期或参数变化时，才允许重新调用。
4. 对写入、发送、删除、下单等副作用工具，重复调用前必须请求确认。
```

注意：**Prompt 不能替代 Runtime 去重。**

## 13. 推荐实现流程

### 13.1 模型调用前

```text
1. 从历史消息解析工具调用与结果
2. 更新 conversation tool cache
3. 根据 TTL 标记结果是否可复用
4. 构造可复用工具结果摘要
5. 注入模型上下文
```

### 13.2 模型返回后

```text
1. 解析 tool_calls
2. 为每个 tool_call 生成 call_key
3. 对同一响应内重复 call_key 去重
4. 执行前查询 conversation cache
5. 命中缓存则返回虚拟 tool result
6. 未命中则真实执行工具
7. 写入缓存与审计日志
8. 将结果返回给模型继续推理
```

## 14. 参考伪代码

```ts
async function runAgentTurn(input: UserInput, state: AgentState) {
  hydrateToolCacheFromHistory(state);

  const reusableResults = getReusableToolResultSummaries(state);

  const modelResponse = await callLLM({
    messages: buildMessages(input, reusableResults),
    tools: state.availableTools
  });

  if (!modelResponse.tool_calls?.length) {
    return modelResponse;
  }

  const toolResults = await executeToolCallsWithDedupe(
    modelResponse.tool_calls,
    state
  );

  appendToolResultsToHistory(state, toolResults);

  return await callLLM({
    messages: state.messages,
    tools: state.availableTools
  });
}
```

## 15. 验收标准

### 15.1 功能验收

- 同一响应内相同 `tool_name + args` 只真实执行一次。
- 跨轮相同 `tool_name + args` 在 TTL 内复用缓存。
- 模型重复请求相同工具时，Runtime 返回虚拟工具结果而不是重新执行。
- 用户明确要求刷新时，可以绕过缓存。
- 副作用工具重复调用被阻止或要求确认。
- 所有执行、复用、拦截行为都有日志。

### 15.2 质量验收

- `canonical_json(args)` 对 key 顺序不敏感。
- 缓存命中不会破坏工具调用协议。
- 每个模型请求的 `tool_call_id` 都有对应 tool result。
- 缓存过期逻辑可测试。
- 副作用工具具备幂等键。
- 单元测试覆盖同轮重复调用、跨轮缓存复用、TTL 过期、用户强制刷新、副作用工具重复拦截、失败结果短 TTL 缓存。

## 16. 最小落地清单

### P0 必须实现

- `call_key = tool_name + canonical_json(args)`。
- 同一响应内工具调用去重。
- conversation 级工具结果缓存。
- 执行前缓存命中拦截。
- 副作用工具标记与重复阻断。

### P1 建议实现

- TTL / freshness 策略。
- 工具策略配置表。
- 工具执行审计日志。
- 模型调用前注入可复用结果摘要。

### P2 可增强

- persistent cache。
- 失败结果短 TTL 缓存。
- token saving 统计。
- 语义级近似重复检测。
- 基于用户意图的 refresh 识别。

## 17. 结论

LLM API 不应被视为可靠的工具调用去重边界。即使工具结果已经完整进入下一轮上下文，模型仍可能重复调用相同工具。

有效解决方案应放在 Agent Runtime 层：

1. 用 `tool_name + canonical_json(args)` 建立确定性调用键。
2. 对同一响应内重复工具调用做去重。
3. 对跨轮工具结果做结构化缓存和 TTL 管理。
4. 在真实执行前拦截重复调用并返回虚拟工具结果。
5. 对副作用工具强制幂等与确认机制。
6. 用提示词减少重复调用概率，但不依赖提示词保证正确性。

最终原则：

```text
模型可以提出重复工具调用，
但 Runtime 不应该重复执行没有必要的工具调用。
```

