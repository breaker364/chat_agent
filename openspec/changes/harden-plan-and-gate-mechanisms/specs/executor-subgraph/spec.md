## ADDED Requirements

### Requirement: 执行步数预算必须可配置并按任务传入

executor 子图的单任务模型步数上限 MUST 从部署配置读取（`workflow.executor_max_steps`，有界整数，默认值等于现行行为），并由 `dispatch_task` 在每次任务分发时传入 executor。未显式配置时 MUST 保持与配置项引入前一致的行为。

#### Scenario: 部署配置自定义步数上限

- **WHEN** `workflow.executor_max_steps` 配置为合法界内的整数
- **THEN** 每次 executor 子图调用的模型步数上限等于该配置值

#### Scenario: 配置越界

- **WHEN** 配置值超出允许的整数范围
- **THEN** 配置加载阶段拒绝并报错，运行时回退到默认值语义不被越界值影响

#### Scenario: 配置缺省

- **WHEN** 配置未提供 `executor_max_steps`
- **THEN** executor 步数上限等于引入该配置项前的默认值
