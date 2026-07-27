## Why

当前应用已经支持保存多个 session 和在前端切换 session，但一次任务运行时的前端流状态是全局的，后端工具上下文也依赖进程级环境变量。这样会导致两个问题：用户不能可靠地在 session A 执行中切到 session B 发起任务；即使后端同时收到多个请求，session/run 上下文也存在跨请求串线风险。

本变更要把“session 切换”和“任务执行”解耦，让不同 session 可以并发执行任务，同时保持每个任务的 SSE、工具事件、进度、取消和历史写入都严格归属到对应 session/run。

## What Changes

- 前端将当前全局的运行状态改为按 session 维护：每个 session 拥有独立的 loading、streaming text、tool events、activity events、debug events 和 abort controller。
- 前端允许用户在一个 session 运行中切换到另一个 session，并在另一个 session 发起新任务；发送按钮只受当前 session 的运行状态影响。
- 前端在 session 列表中展示 session 级运行状态，并在切换回来时继续展示该 session 的实时流输出或最终结果。
- 后端将当前 session/run 上下文从进程级 `os.environ` 迁移到并发安全的请求上下文，避免不同 SSE 请求互相覆盖上下文。
- 后端增加 session/run 级执行控制：不同 session 可以并发执行；同一 session 默认只允许一个前台 run，避免同一会话历史和输出顺序冲突。
- SSE 事件和内部事件分发保持 session 隔离，并在必要时携带 run 标识，确保前端能把事件落到正确的 session 运行实例。
- 保留已有后台 subagent 并发能力；本变更只定义用户发起的前台 session 任务并发。
- 增加后端并发上下文隔离测试、同 session 重入测试，以及前端 per-session stream state 的关键行为验证。

## Capabilities

### New Capabilities

- `concurrent-session-runs`: 定义多个用户 session 的前台任务并发执行、上下文隔离、SSE 归属、同 session 防重入和取消行为。

### Modified Capabilities

无。

## Impact

- 后端：`backend/main.py` 的 `/chat/stream` 和 `/chat` 请求生命周期、SSE event generator、run id 管理、上下文设置/清理逻辑。
- 后端：`backend/tools.py` 当前 session/run 读取、工具结果缓存、工具去重、任务计划和 subagent 关联逻辑。
- 后端：`backend/session_events.py` 的事件分发契约可能需要扩展 run id 过滤或事件载荷规范。
- 后端：`backend/session_store.py` 继续作为 session 持久化边界，需要验证并发写入同一 session 被限制、不同 session 写入保持安全。
- 前端：`frontend/src/App.jsx` 的 session 选择、发送、取消、SSE 解析、流式消息展示、工具事件、activity/debug 面板和输入按钮状态。
- 测试：新增或扩展后端 async 并发测试、工具上下文隔离测试、同 session 重入行为测试；前端至少覆盖状态 reducer/handler 级逻辑，条件允许时补充端到端手工验证。
- API 兼容性：保留现有 `/chat/stream` 请求入口；可向 SSE payload 增加向后兼容字段，例如 `session_id`、`run_id`、`status`，不删除现有事件类型。
