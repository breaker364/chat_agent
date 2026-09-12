## ADDED Requirements

### Requirement: Executor 子图必须以单任务契约运行

Executor 子图 MUST 接收单个任务快照、任务上下文、允许的能力、尝试预算和可复用结果，并返回任务状态、结果摘要、产物引用、用量、错误和审计事件；它不得直接修改父图的计划或拓扑。

#### Scenario: 单任务成功返回

- **WHEN** Executor 在预算内完成任务并通过结果校验
- **THEN** 子图返回带 task ID、attempt ID、结果状态和引用的成功 `TaskResult`

#### Scenario: Executor 只返回局部失败

- **WHEN** 工具或模型无法完成任务
- **THEN** 子图返回该任务的结构化失败结果，父图可以决定重试或重规划，其他任务状态不被直接修改

### Requirement: Executor Agent 只能访问任务授权工具

系统 MUST 根据任务能力和工具元数据构造 Executor 的可见工具集合；未授权工具、父图控制能力和不满足确认条件的副作用工具 MUST 不出现在工具绑定中或在运行前被拒绝。

#### Scenario: 只读任务绑定工具

- **WHEN** 任务只声明只读检索能力
- **THEN** Executor 仅获得满足该能力的只读工具，不能调用计划、调度或写入工具

#### Scenario: 工具请求越权

- **WHEN** Executor 模型请求未授权工具
- **THEN** 工具层返回可诊断的权限错误，子图不执行该工具，并按策略返回失败或澄清结果

#### Scenario: 副作用工具需要确认

- **WHEN** 任务包含受保护的副作用工具且尚未获得审批
- **THEN** Executor 不调用该工具，控制权返回父图的 approval 路径

### Requirement: Executor 必须保留受限 ReAct 工具循环

Executor MUST 支持模型响应、工具执行、工具结果回填和再次模型响应的有限循环；无工具调用且输出满足结束条件时进入验证，达到递归、时间、token 或工具预算时必须终止。

#### Scenario: 模型产生工具调用

- **WHEN** Executor Agent 返回合法工具调用
- **THEN** 子图执行已授权工具，将带 call ID 的工具结果回填，并允许有限次数的下一轮模型调用

#### Scenario: 模型无工具调用

- **WHEN** Executor Agent 返回无工具调用的候选答案
- **THEN** 子图进入 `executor_validate`，不会无条件再开启新的工具循环

#### Scenario: 达到执行预算

- **WHEN** 子图达到任一硬预算上限
- **THEN** 子图停止继续调用模型或工具，返回预算耗尽原因和当前可用的部分结果

### Requirement: Executor 必须通过结果校验和分层重试处理失败

`executor_validate` MUST 校验输出契约、必要字段、产物引用和任务目标；瞬时失败可以在任务预算内重试，权限、策略和非法输出失败不得通过自动重试绕过约束。

#### Scenario: 瞬时工具错误可重试

- **WHEN** 工具返回标记为 retryable 的瞬时错误且任务仍有尝试预算
- **THEN** Executor 创建新的 attempt，复用幂等键，并记录前一次错误

#### Scenario: 输出契约不满足

- **WHEN** 模型输出缺少任务要求的字段或引用
- **THEN** 校验节点返回结构化失败，禁止将其标记为成功；系统可发起受限修复轮次

#### Scenario: 策略失败不可重试放权

- **WHEN** 失败原因是权限、审批或策略拒绝
- **THEN** Executor 不修改工具集合或策略，直接返回 blocked/needs_approval 状态
