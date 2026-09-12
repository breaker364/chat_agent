## ADDED Requirements

### Requirement: 顶层工作流必须使用固定拓扑表达生命周期

系统 MUST 编译一个由系统注册的顶层 LangGraph 工作流，至少包含 `intake`、`prepare_context`、`route`、`planner`、`plan_guard`、`scheduler`、`dispatch_task`、`join`、`assess`、`validate` 和 `finalize` 节点，并通过预定义普通边和条件边连接这些节点。运行中的 Agent 不得创建、删除或修改节点和边。

#### Scenario: 编译图包含必需节点

- **WHEN** 应用启动并调用工作流图工厂
- **THEN** 工厂返回已编译图，所有必需节点和从 `START` 到 `END` 的合法路径均已注册

#### Scenario: Agent 尝试改变拓扑

- **WHEN** Agent 输出任意节点名、边名或图修改意图
- **THEN** 系统将其视为普通数据或无效路由，不改变已编译图的节点集合和边集合

### Requirement: 顶层图必须区分简单请求、计划请求和澄清请求

系统 MUST 根据经过裁决的路由结果选择预定义路径：简单请求进入直接执行路径，复杂或多任务请求进入 `planner` 和 `plan_guard`，信息不足或无法安全执行的请求进入澄清/终止路径。

#### Scenario: 简单请求直接执行

- **WHEN** 路由结果为 `direct` 且请求不要求任务依赖、研究或高风险副作用
- **THEN** 顶层图跳过任务规划，调用 `direct_executor` 或等价的单任务执行路径，并最终进入验证和最终化

#### Scenario: 复杂请求进入计划路径

- **WHEN** 路由结果为 `planned` 或 `research`
- **THEN** 顶层图依次执行 `planner` 和 `plan_guard`，只有计划通过校验后才进入 `scheduler`

#### Scenario: 请求需要澄清

- **WHEN** 路由裁决为 `clarify` 或安全策略判定关键输入缺失
- **THEN** 顶层图不得调用任务工具，进入澄清/终止路径并返回机器可识别的原因

### Requirement: 计划必须以可校验的任务 DAG 驱动调度

`planner` MUST 输出具有唯一任务 ID、任务类型、依赖、能力、预算和尝试上限的任务集合；`plan_guard` MUST 拒绝重复 ID、缺失依赖、环依赖、非法能力和超出系统上限的计划。

#### Scenario: 合法计划通过校验

- **WHEN** 计划中的任务 ID 唯一、依赖存在且无环，且任务数、预算和能力均在上限内
- **THEN** `plan_guard` 将计划标记为可调度，并允许 `scheduler` 计算 ready 任务

#### Scenario: 非法计划被拒绝

- **WHEN** 计划包含环依赖、未知能力或超出任务数量上限
- **THEN** `plan_guard` 记录结构化错误，不向 `dispatch_task` 分发该计划，并根据策略进入有限重规划或 blocked 路径

### Requirement: 调度器必须使用受控 fan-out/fan-in 执行独立任务

`scheduler` MUST 仅选择依赖已满足且状态为 `ready` 的任务，并通过 LangGraph `Send` 将每个任务分发到 `dispatch_task`；系统 MUST 以批次屏障汇聚分支结果，并限制最大并行数和总执行预算。

#### Scenario: 多个独立任务并行执行

- **WHEN** 同一批次存在多个相互独立且预算足够的 ready 任务
- **THEN** 系统为每个任务创建带 `task_id` 和 `attempt_id` 的 `Send` 分支，分支完成后由 `join` 一次性汇聚结果

#### Scenario: 依赖任务等待

- **WHEN** 某任务依赖的前置任务尚未进入成功终态
- **THEN** `scheduler` 不分发该任务，`join` 保留其 pending 状态，并在下一调度轮次重新计算

#### Scenario: 并行度超出上限

- **WHEN** ready 任务数量大于配置的最大并行度或剩余预算不足
- **THEN** 调度器只分发受限数量的任务，其余任务保持 ready，不得由模型提高并行度上限

### Requirement: 顶层图必须对结果执行评估、验证和完成门禁

系统 MUST 在任务批次汇聚后执行 `assess`，根据结果选择完成、重试、重规划或阻塞路径；只有 `validate` 通过且必要产物、证据和任务结果满足策略时，`finalize` 才能生成成功响应。

#### Scenario: 所有任务完成并通过验证

- **WHEN** `assess` 判定目标已满足且 `validate` 通过结果契约检查
- **THEN** `finalize` 生成最终响应和结果摘要，工作流以 completed 状态结束

#### Scenario: 结果仍可重试

- **WHEN** `assess` 判定存在可恢复错误且任务尚未达到尝试上限
- **THEN** 图回到 `scheduler` 创建新的 attempt，并保留原失败记录供审计

#### Scenario: 计划需要调整

- **WHEN** 已完成结果显示依赖或证据缺口无法通过当前计划解决
- **THEN** 图回到 `planner` 生成受限新版本计划，已验证有效的任务结果不得被重复执行

#### Scenario: 结果不可恢复

- **WHEN** 错误不可恢复、预算耗尽或策略禁止继续执行
- **THEN** 图进入 `finalize` 的 blocked/failed 分支，响应包含结构化失败原因且不伪装为成功
