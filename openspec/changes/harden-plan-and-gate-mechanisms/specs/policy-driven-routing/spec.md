## ADDED Requirements

### Requirement: plan_guard 必须将任务能力约束在 route 批准集之内

`plan_guard` MUST 在全局能力注册表校验之外，独立校验每个任务的 `capabilities` 是否为本次 route 决策 `required_capabilities` 的子集。该校验 MUST 不依赖 planner 的 prompt 约束或 planner 自身实现，违例计划 MUST 以可审计的拒绝原因被拒绝。

#### Scenario: 任务请求 route 未批准的能力

- **WHEN** 计划中某任务声明的能力在全局注册表内但不在 route 批准集内
- **THEN** plan_guard 拒绝该计划，流程进入 finalize/blocked，审计事件携带能力越权原因

#### Scenario: 自定义 planner 注入

- **WHEN** `build_workflow_graph` 被注入不做事前能力约束的自定义 planner
- **THEN** 越权计划仍在 plan_guard 被拦截，route 能力边界不因 planner 实现而失效

#### Scenario: 计划能力是批准集的子集

- **WHEN** 所有任务的能力均为 route 批准集的子集
- **THEN** plan_guard 放行计划并进入后续审批或调度路径

### Requirement: 计划产出后必须重评审批要求

`plan_guard` 校验通过后，系统 MUST 按计划内容重评审批要求：当计划中任一任务的能力命中高危能力判定时，流程 MUST 进入既有 approval_gate 中断等待人工确认。高危能力判定 MUST 以能力标识模式定义，不得包含具体实体名，且初始为空时行为 MUST 与未启用该重评时一致。

#### Scenario: 计划包含高危能力

- **WHEN** 某任务的能力命中高危能力判定且 route 阶段未要求审批
- **THEN** 计划校验通过后仍进入 approval_gate，需人工批准才能调度

#### Scenario: 高危判定为空

- **WHEN** 高危能力判定集合为空
- **THEN** 审批要求仅由 route 阶段的 `risk_level` 决定，行为与重评机制引入前一致

#### Scenario: 审批拒绝高危计划

- **WHEN** 用户在 approval_gate 拒绝包含高危任务的计划
- **THEN** 流程按既有审批拒绝路径终止，不调度任何任务
