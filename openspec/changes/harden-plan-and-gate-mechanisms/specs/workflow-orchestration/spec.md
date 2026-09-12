## ADDED Requirements

### Requirement: 重规划必须携带上一轮失败上下文

当 `assess` 节点选择 `replan` 路径时，`planner` 节点 MUST 将受界的失败摘要注入 planner 输入：失败任务的标识、类型、上下文摘要、错误类别与尝试序号，以及已成功任务的标识与"勿重复执行"标注。摘要 MUST 经过与 route prompt 一致的长度截断，且 MUST NOT 携带完整任务输出。

#### Scenario: 存在失败任务时重规划

- **WHEN** 上一批次存在 `status=failed` 的任务结果且重规划次数未超上限
- **THEN** planner 收到的输入包含这些任务的标识、错误类别与已成功任务清单，而非只有原始请求

#### Scenario: 无失败证据的重规划

- **WHEN** 触发重规划但没有任何失败任务或错误记录
- **THEN** planner 输入退化为原始请求与路由决策，行为与首规划一致

#### Scenario: 摘要长度受限

- **WHEN** 失败任务数量或上下文文本超过摘要预算
- **THEN** 摘要按确定性规则截断，planner 调用不因摘要超长而失败

### Requirement: planner 失败必须先确定性降级再阻塞

`planner` 节点 MUST 在模型 planner 异常时改用确定性兜底 planner（基于原始请求与 route 批准能力集合成单任务计划），兜底计划 MUST 经过与普通计划相同的 `plan_guard` 全量校验；仅当兜底也失败时才置 `blocked`。降级发生 MUST 记录可审计事件。

#### Scenario: 模型 planner 异常

- **WHEN** 模型 planner 调用抛出异常或返回无法解析的结构
- **THEN** 系统改用确定性兜底 planner 生成单任务计划并继续走 plan_guard，而不是直接 blocked

#### Scenario: 兜底计划通过校验

- **WHEN** 兜底生成的单任务计划通过 plan_guard 校验
- **THEN** 流程按普通计划继续调度执行，且审计事件中标记使用了降级

#### Scenario: 兜底仍失败

- **WHEN** 确定性兜底同样抛出异常或其计划未通过校验
- **THEN** 系统置 `status=blocked` 并记录 planner_error 类别的可审计错误
