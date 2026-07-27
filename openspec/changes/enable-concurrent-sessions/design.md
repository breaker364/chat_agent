## Context

当前系统具备 session 持久化、session 列表、session 切换、SSE 流式回答、工具事件记录和后台 subagent 能力，但用户发起的前台任务仍按“全局单运行态”组织。

前端主要限制在 `frontend/src/App.jsx`：

- `loading`、`streamingText`、`toolEvents`、`activityItems`、`debugEvents` 和 `abortRef` 是全局状态。
- `handleSend` 在 `loading` 为真时直接返回，因此任意 session 运行中都会阻止另一个 session 发起任务。
- SSE reader 完成后直接写当前 `messages`，如果用户在请求期间切换 session，存在把旧请求输出落到当前 session 视图的风险。
- `handleStop` 只控制一个全局 `abortRef`，无法表达“停止 session A，但 session B 继续运行”。

后端主要限制在 `backend/main.py` 和 `backend/tools.py`：

- `/chat/stream` 和 `/chat` 为每次请求生成 `run_id`，但通过 `os.environ[CHAT_AGENT_SESSION_ID]` 和 `os.environ[CHAT_AGENT_RUN_ID]` 设置当前工具上下文。
- `backend/tools.py` 的 `_current_session_id()` / `_current_run_id()` 在工具缓存、工具审计、任务计划、subagent 关联、脚本阶段记录等路径中读取这些进程级环境变量。
- 进程级环境变量会被并发请求共享；两个 session 同时执行时，后发请求可能覆盖先发请求的工具上下文。
- `SessionStore` 已经按 session 文件夹持久化数据，并使用进程内文件锁保护写入；这适合作为隔离边界，但不应承担同一 session 前台 run 的顺序仲裁。
- `SessionEventHub` 当前按 session 分发事件；并发 session 可以自然隔离，但同一 session 多 run 时需要 run 级过滤或直接禁止同 session 重入。

## Goals / Non-Goals

**Goals:**

- 允许不同 session 同时执行用户发起的前台任务。
- 禁止同一 session 同时执行多个前台任务，保持该 session 的消息历史、工具事件和最终回答顺序明确。
- 后端工具运行时上下文必须按请求/run 隔离，不再依赖进程级可变环境变量表达当前 session/run。
- 前端 SSE 状态必须按 session/run 保存；用户切换 session 不会中断其他 session 的流，也不会把流输出写入错误视图。
- 每个 session 都能独立取消自己的当前 run，取消一个 session 不影响其他 session。
- 保持现有 `/chat/stream` API 可用，新增字段保持向后兼容。

**Non-Goals:**

- 不实现同一 session 内多个用户消息并行生成回答；这会破坏线性对话历史。
- 不把运行状态持久化升级为跨进程/跨 worker 的分布式锁；本变更以当前单进程本地开发形态为目标。
- 不重写 agent、tool、subagent 的核心执行模型。
- 不改变后台 subagent 的并发语义；只确保它们继续关联到触发它们的 session。
- 不引入新的外部服务或数据库。

## Decisions

### Decision 1: 使用 per-session active run registry 控制前台任务

新增一个后端运行注册表，按 `session_id` 记录当前活跃的前台 run。`/chat/stream` 和 `/chat` 在写入用户消息、更新 progress、启动 agent 前先尝试注册 run。

建议数据结构：

- `session_id`
- `run_id`
- `started_at`
- `endpoint`，例如 `chat_stream` 或 `chat_sync`
- `status`，例如 `running`、`cancelling`、`finished`

行为：

- 如果 session 没有活跃 run，则创建新的 `run_id` 并注册。
- 如果 session 已有活跃 run，则拒绝新的前台请求，且不得向该 session 追加新的 user message。
- 不同 session 的 run 注册互不影响，可以并发执行。
- run 在完成、失败、客户端断开或 finally 清理路径中释放。

备选方案：

- 允许同一 session 并发多个 run，并用 run id 合并历史。这会让同一对话的消息顺序和上下文选择变得不确定，不适合作为默认行为。
- 只在前端禁止同一 session 重入。后端仍会被直接 API 调用绕过，因此不能作为完整保护。

### Decision 2: 用请求上下文替代进程级环境变量

将当前 session/run 上下文迁移到 Python `contextvars.ContextVar`。推荐新增轻量模块或在 `backend/tools.py` 中集中提供：

- `bind_runtime_context(session_id, run_id)`
- `current_session_id()`
- `current_run_id()`
- `clear_tool_dedupe_cache(run_id)`

`backend/main.py` 在每个 run 生命周期内绑定上下文，并在 finally 中 reset token。`backend/tools.py` 的 `_current_session_id()` 和 `_current_run_id()` 改为读取 `ContextVar`，只在兼容旧路径时保留环境变量 fallback，不再由请求路径写入 `os.environ`。

需要注意线程边界：

- `asyncio.create_task` 会复制当前 context，因此 `/chat/stream` 中 agent task 和 event queue task 可以继承绑定上下文。
- 项目内显式开线程的 helper，例如 `_run_coro_in_thread()` 和 `_run_coro_in_thread_with_timeout()`，应使用 `contextvars.copy_context()` 包住 target，避免同步 subagent 或工具路径丢失上下文。
- 后台 subagent 已显式接收 `session_id`，仍应继续显式传递，不依赖后台线程继承前台 context。

备选方案：

- 继续使用 `os.environ` 并加锁。锁会把不同 session 串行化，违背并发目标。
- 把 session/run 作为参数穿透每个工具函数。隔离最强，但改动面过大，且会污染工具公开 schema。

### Decision 3: SSE payload 增加 run/session 归属字段

后端继续使用现有事件类型，例如 `text`、`progress`、`activity`、`debug`、`tool_call`、`tool_result`、`error`、`done`。对结构化 JSON payload 增加向后兼容字段：

- `session_id`
- `run_id`
- `status`，在适用时提供

对于纯文本 token 事件，可以保持现状，因为它只通过当前 HTTP response 返回给对应 reader；前端用本地闭包中的 `session_id/run_id` 归属即可。对于来自 `SessionEventHub` 的 subagent/debug 事件，应携带 `session_id`，并在同一 session 未来支持多 run 时携带 `run_id`。

备选方案：

- 新增全量事件协议并替换所有事件类型。这样会增加回归风险；当前需求只需要归属字段和前端路由。

### Decision 4: 前端使用 per-session run state map

前端新增按 session 维护的运行状态，而不是继续使用全局 run state。

建议状态形态：

```js
{
  [sessionId]: {
    runId,
    status: "running" | "completed" | "failed" | "stopped",
    streamingText,
    toolEvents,
    activityItems,
    debugEvents,
    usage,
    error,
    startedAt,
    updatedAt
  }
}
```

`AbortController` 不应放入 React state；使用 `useRef(new Map())` 保存 `sessionId -> controller`。渲染时派生：

- `activeRun = sessionRuns[activeSessionId]`
- `activeLoading = activeRun?.status === "running"`
- `activeStreamingText = activeRun?.streamingText || ""`
- `activeToolEvents = activeRun?.toolEvents || []`

`handleSend` 捕获 `ensuredSessionId` 和 `runId`，所有 SSE handler 都通过这两个值更新对应 session 的 run state。只有当 `ensuredSessionId === activeSessionId` 时，才影响当前滚动和当前可见输入态；但数据始终写入 `sessionRuns[ensuredSessionId]`。

备选方案：

- 每次切换 session 都中断旧请求。实现简单，但不能满足并发执行。
- 保留全局 state 并在切换时隐藏。这样仍无法处理后台 session 完成后写错 `messages` 的问题。

### Decision 5: 前端消息也按 session 缓存或按 session guarded update

当前 `messages` 表示当前 active session 的消息列表。并发后，后台 session 的 SSE 完成回调可能在用户已经切到另一个 session 时触发，因此不能直接 `setMessages([...])`。

推荐实现一个 `messagesBySession` 缓存：

- `loadSession(sessionId)` 将后端返回的 messages 写入 `messagesBySession[sessionId]`，并设置 `activeSessionId`。
- 渲染时使用 `messagesBySession[activeSessionId]`。
- `handleSend` 在请求开始时把 user message 写入 `messagesBySession[ensuredSessionId]`。
- `done/error/abort` 时把 assistant message 写入 `messagesBySession[ensuredSessionId]`。
- 如果该 session 当前不可见，不切换 UI；用户切回时直接看到缓存，随后可由 `loadSession` 或 `refreshSessions` 与持久化结果对齐。

如果为了降低改动面暂不引入完整 `messagesBySession`，则所有异步 `setMessages` 必须先检查当前 active session 是否仍是 `ensuredSessionId`；后台 session 完成只刷新 session list，不改当前可见 messages。这种方案体验较弱，但仍必须保证不串线。

### Decision 6: 取消行为按 session 作用域处理

`handleStop` 改为取消当前 active session 的 controller：

- 如果当前 active session 没有运行中的 controller，则不做操作。
- abort 后只更新该 session 的 run state 为 `stopped`。
- 其他 session 的 controller 和 stream reader 不受影响。

后端当前依赖客户端断开在 finally 中保存 partial/blocked 状态。该行为可以保留。未来如果需要服务端主动取消，可新增 `/runs/{run_id}/cancel`，但不是本变更的必需项。

## Risks / Trade-offs

- [Risk] `ContextVar` 不自动传播到某些第三方线程池工具执行路径。→ Mitigation：为项目内线程 helper 显式使用 `copy_context()`；增加并发工具上下文隔离测试，测试失败时在 agent/tool wrapper 层补充显式绑定。
- [Risk] 进程内 active run registry 不能跨多 worker 生效。→ Mitigation：文档中明确单进程约束；如果部署多 worker，后续改为文件锁、SQLite 或外部锁。
- [Risk] 前端同时维护 session list、messages cache、run state，容易出现持久化消息和本地流状态重复。→ Mitigation：以 `runId` 和最终 assistant message 追加点为边界；完成后刷新对应 session，合并时避免重复追加同一 run。
- [Risk] 客户端切换 session 后继续读取旧 SSE，会增加浏览器内并发连接和内存占用。→ Mitigation：只保存有限长度 debug/tool/activity 事件；完成后清理 controller；session list 只显示摘要状态。
- [Risk] 同 session 重入被拒绝可能让用户觉得“发送失效”。→ Mitigation：前端在该 session 运行时禁用发送并显示运行状态；直接 API 调用返回明确错误码。

## Migration Plan

1. 后端先引入 run registry 和 runtime context，替换 `/chat/stream`、`/chat` 中对 `os.environ` 的请求级写入。
2. 更新 `backend/tools.py` 的当前 session/run 读取函数和项目内线程 helper，保持环境变量 fallback 但不作为主路径。
3. 增加后端测试：不同 session 并发工具调用写入各自 session；同 session 第二个 run 被拒绝且不追加消息；异常和断开后 registry 释放。
4. 前端引入 per-session run state 和 controller map，把 `handleSend`、`handleStop`、SSE dispatch、按钮 disabled 状态迁移到 session 作用域。
5. 前端补充 session 消息缓存或 guarded update，确保后台 session 完成不会修改当前 session 视图。
6. 手工验证：session A 发起长任务，切到 session B 发起任务，两个任务都能继续；停止 A 不影响 B；最终历史分别写入 A/B。
7. 回滚策略：如前端迁移出现问题，可先保留后端上下文隔离和同 session guard，前端继续单全局运行态；这不会提供完整并发体验，但能降低后端串线风险。

## Open Questions

- 生产部署是否会使用多 worker。如果会，active run registry 需要共享锁设计，而不是进程内 map。
- 是否需要展示“所有运行中 session”的全局停止入口。当前设计只要求当前 active session 可停止。
- 是否要为 `/chat` 同步接口暴露 HTTP 409，还是与 `/chat/stream` 一样返回结构化错误 payload。建议 `/chat` 使用 HTTP 409，`/chat/stream` 使用 SSE error event 或 409，具体取决于前端兼容成本。
