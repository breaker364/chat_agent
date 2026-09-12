## ADDED Requirements

### Requirement: 已完成任务的状态不可被快照降级

`set_task_plan` 合并既有任务计划时，对状态为 `completed` 的任务 MUST 拒绝将其状态改为 `pending`、`in_progress` 或 `failed`：保留 `completed` 并记录可审计的 warning。`details` 与 `result_ref` 字段 MUST 允许继续更新。单项更新路径对 completed 任务的回退状态 MUST 同样拒绝。

#### Scenario: 陈旧快照试图降级已完成任务

- **WHEN** 模型提交的完整快照把某个 completed 任务的状态写回 pending
- **THEN** 该任务保持 completed，返回结果包含降级被拒绝的 warning

#### Scenario: 允许补充已完成任务的细节

- **WHEN** 快照仅更新 completed 任务的 details 或 result_ref，状态仍为 completed
- **THEN** 更新被接受，不产生 warning

#### Scenario: 单项更新回退被拒绝

- **WHEN** 单项状态更新试图把 completed 任务改为 pending 或 in_progress
- **THEN** 更新被拒绝，任务保持 completed，并记录可审计事件

### Requirement: 计划消费方必须使用构建期注入的存储根

agent 驱动层内的任务计划消费点（完成门禁、恢复上下文投影、追加命令事件记录、路由摘要读取）MUST 使用与 `build_agent` 相同的 workspace root 构造 `SessionStore`，MUST NOT 依赖进程当前工作目录。服务工作目录与 workspace root 不一致时，消费点 MUST 读写 workspace 内的会话数据。

#### Scenario: 工作目录与 workspace root 不一致

- **WHEN** 服务进程启动目录不同于配置的 workspace root，且某会话已有任务计划与 primary result
- **THEN** 完成门禁与恢复上下文读取的是 workspace 内该会话的数据，而非工作目录下的错误路径

#### Scenario: 默认部署一致路径

- **WHEN** 服务工作目录等于 workspace root
- **THEN** 消费点行为与存储根统一前完全一致
