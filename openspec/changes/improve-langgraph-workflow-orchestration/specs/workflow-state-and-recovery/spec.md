## ADDED Requirements

### Requirement: 工作流必须使用版本化结构化状态

系统 MUST 使用版本化 `WorkflowState` 承载请求上下文、路由、计划、任务尝试、任务结果、研究摘要、产物、错误、审批、响应、控制预算和审计事件；`session_id`、`run_id`、能力和预算等控制值 MUST 来自受信任的运行时上下文，不得由模型结果直接覆盖。

#### Scenario: 新运行初始化状态

- **WHEN** 新请求创建工作流运行
- **THEN** 系统生成带 schema 版本、session 标识、run 标识和初始预算的合法 `WorkflowState`

#### Scenario: 模型返回控制字段

- **WHEN** 模型输出包含 session、预算、权限或任意系统控制字段
- **THEN** 状态合并器忽略模型对这些受信任字段的覆盖，并保留运行时上下文的值

### Requirement: 并行状态合并必须可确定且幂等

系统 MUST 为消息、任务结果、证据、产物、错误和审计事件定义 reducer；任务结果按 `task_id`/`attempt_id` 做版本判断，证据和产物按稳定标识去重，迟到或重复分支不得覆盖更新的终态。

#### Scenario: 并行分支合并不同任务

- **WHEN** 两个分支分别返回不同任务的结果、产物和事件
- **THEN** reducer 将两组数据合并到同一状态，且任务、产物和事件均可按标识追踪

#### Scenario: 重复或迟到结果到达

- **WHEN** 同一任务的旧 attempt 结果在新 attempt 已完成后到达
- **THEN** 系统保留该结果的审计记录但不覆盖新 attempt 的任务终态

#### Scenario: 工具消息需要回放

- **WHEN** 状态包含 AI 工具调用和对应工具结果
- **THEN** 消息 reducer 保持 `tool_call_id` 配对和原有顺序，恢复后的模型输入仍符合工具消息协议

### Requirement: 工作流必须支持节点级 checkpoint 恢复

系统 MUST 在编译图时注入 checkpointer，并以由 `session_id` 与 `run_id` 派生的稳定 thread 标识保存节点级状态；恢复同一运行时 MUST 从最近合法 checkpoint 继续，而不是从头执行已完成的副作用任务。

#### Scenario: 节点执行中断后恢复

- **WHEN** 进程在某节点完成后、下一节点开始前退出，随后使用同一运行 thread 恢复
- **THEN** 图从最近 checkpoint 继续，已提交的任务结果和审计事件保持不变

#### Scenario: 新回合使用独立运行标识

- **WHEN** 同一 session 开始新的用户回合
- **THEN** 系统创建新的 run/thread 标识，同时从 `SessionStore` 读取已确认的业务历史和可复用结果

### Requirement: 高风险操作必须支持可审计人工确认

系统 MUST 在需要人工确认的计划或任务进入副作用执行前调用 `interrupt()`；系统 MUST 持久化待确认内容、策略原因、任务标识和 checkpoint 引用，确认或拒绝均只能恢复同一运行。

#### Scenario: 高风险任务暂停

- **WHEN** `plan_guard` 或策略裁决要求人工确认
- **THEN** 图在 `approval_gate` 暂停，不调用被保护工具，并向事件适配器发送 approval-needed 事件

#### Scenario: 用户批准后恢复

- **WHEN** 用户提交与待确认运行匹配的批准操作
- **THEN** 系统通过恢复命令继续同一 checkpoint，并只执行原计划中已授权的任务

#### Scenario: 用户拒绝或确认过期

- **WHEN** 用户拒绝操作或确认请求已过期
- **THEN** 图不执行副作用工具，记录拒绝/过期原因并以 blocked 或 cancelled 状态结束

### Requirement: 工作流事件必须可映射到现有 SSE 协议

系统 MUST 为图、节点、子图、任务和工具事件附加 `graph_path`、`node`、`task_id`、`attempt` 和状态元数据，并由适配层映射到现有 `debug`、`activity`、`progress`、`tool_call`、`tool_result` 和 `done` 事件。

#### Scenario: 节点生命周期可观测

- **WHEN** 任意顶层节点或子图节点开始、完成或失败
- **THEN** SSE 消费者收到带运行和节点上下文的可识别事件，旧客户端仍可按原事件类型处理

#### Scenario: 任务失败可追踪

- **WHEN** 某任务工具调用失败并触发重试
- **THEN** 事件流包含任务 ID、attempt、失败原因和后续 retry/replan 阶段，不把重复执行伪装成新任务
