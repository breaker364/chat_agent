## Why

当前长任务运行期间，用户只能等待、停止或切换 session；如果发现需要补充约束、修正方向或追加资料，无法把新指令注入到正在进行的 run 中，只能中断后重新发起。需要支持“运行中追加命令”，让下一次调用 LLM API 时把用户追加内容送入 agent，从而减少重跑和上下文丢失。

本变更面向同一 session 的当前 active run：允许用户在任务运行中提交追加 prompt；系统在下一轮模型调用前将其作为带“追加指令”语义的用户补充输入注入 agent，并在 UI 和事件中可追踪地展示。

## What Changes

- 新增运行中追加命令入口，允许同一 session 的 active run 接收一个或多个追加 prompt。
- 前端在当前 session 运行中保持输入框可用，发送按钮进入“追加”语义，而不是被 running 状态完全禁用。
- 后端为 active run 维护追加命令队列，并保证追加内容归属到正确 `session_id/run_id`。
- agent 在下一次调用 LLM API 前读取 pending append commands，并把它们作为明确标注的补充指令注入消息上下文。
- 注入给 LLM 的提示词必须含有类似“追加指令”/“用户追加要求”的字样，避免模型误判为原始任务或历史对话。
- 追加命令需要被记录到 session 历史、run activity/debug 事件或任务进度中，便于用户和开发者追踪。
- 如果当前 session 没有 active run，则追加请求不得静默丢弃；应返回可读错误，并提示用户作为普通新消息发送。
- 不改变不同 session 并发 run 的既有隔离规则；追加命令只能作用于目标 session 的当前 run。

## Capabilities

### New Capabilities

- `run-command-append`: 定义用户在 active run 执行中追加 prompt 的接收、排队、下一次 LLM 调用注入、提示标记、状态展示和错误处理行为。

### Modified Capabilities

无。

## Impact

- 后端：`backend/main.py` 需要新增 active run 追加命令 API 或扩展现有 chat stream 处理，维护 run 级 append queue，并复用当前 `session_id/run_id` 隔离。
- 后端：`backend/agent.py` 的 `stream_agent_events` / 模型调用循环需要在每次 LLM API 调用前消费 pending append commands 并注入消息上下文。
- 后端：`backend/session_store.py` 或新的运行态模块需要记录追加命令事件，支持 UI 刷新后可见。
- 后端：`backend/session_events.py` 可用于向当前 SSE stream 推送“收到追加指令 / 已注入追加指令”的 activity 或 debug 事件。
- 前端：`frontend/src/App.jsx` 和 session run state 需要支持 running 状态下的 append 输入、append 发送、append pending/accepted 展示，以及同 session stop 行为不变。
- 测试：新增后端 run append queue、LLM 调用前注入、无 active run 错误、跨 session 隔离测试；前端新增 running 状态下 append 发送和按钮语义测试。
- API 兼容性：保留现有 `/chat/stream` 行为；追加命令应通过新增 endpoint 或明确字段实现，避免破坏普通新消息发送。
