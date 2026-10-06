## 为什么

运行时的子代理类型目前硬编码为四个(`subagents.py` 的 `built_in_subagents()`),`Agent` 工具遇到未知 `subagent_type` 时静默回落 general-purpose,模型的输出只有任务 prompt 可变,角色提示与工具边界都无法按任务定制。而任务形态是多样的:同构复制的并行审查需要同一角色模板实例化多份、特殊视角的独立核验需要定制的角色约束与输出格式,现有固定类型覆盖不了。`AgentDefinition` 的字段结构已经具备承载运行时定义的形状,缺的只是让模型在调用时填写这份结构的通道,以及随之必须补齐的护栏(现网 general-purpose 子代理未被禁止再派生子代理,自由构造会放大该风险)。

## 变更内容

- `Agent` 工具入参扩展:新增可选字段 `system_prompt`(现场角色提示)、`allowed_tools`(现场工具白名单)、`disallowed_tools`(现场工具黑名单)、`max_turns`(现场轮次预算);显式字段覆盖类型预设,省略字段沿用类型预设。
- 新增统一解析入口 `resolve_agent_definition`:以 `subagent_type` 为基础模板,显式字段按覆盖语义合成有效的 `AgentDefinition`;未知类型名回落 general-purpose 作为基础模板,并在结果中透出解析说明。
- 递归封锁收紧为全量强制:所有子代理(无论预设还是现场构造)的有效工具集一律移除 `Agent` 与 `SendMessage`,现场定义的黑名单只能收紧不能解禁。
- 预算护栏:`max_turns` 钳制到硬上限;`system_prompt` 超过长度上限时拒绝派发并返回明确错误;`allowed_tools`/`disallowed_tools` 中不存在的工具名剔除且不授予任何未注册能力。
- 后台派发并发有界:`AsyncSubagentManager` 以信号量限制同时在跑的子代理数量,超限排队;同步路径超时转后台的既有行为不变。
- 可观测:工具结果、任务状态与子代理 transcript 记录解析后的定义摘要(基础类型、是否使用现场提示、有效工具数、有效轮次),预设代理与构造代理在审计中可区分。
- 提示词追加构造原则:`system_prompt.md` 与 `agent_policy.md` 的子代理小节补充何时值得现场构造、如何写自包含任务提示、如何收敛工具边界;不改变现有类型清单。

## 能力

### 新增能力

- `dynamic-subagent-construction`:模型经 `Agent` 工具现场定义一次性子代理的角色提示、工具边界与轮次预算的覆盖语义、硬性护栏(递归封锁、预算钳制、提示长度上限、工具名校验)、后台并发上限,以及构造行为的可观测要求与验收标准。

### 已修改能力

无。既有子代理预设类型的行为保持不变;`add-agent-action-idempotency` 的工具审计包装层自动覆盖本变更新增的字段,无需改动该能力。

## 影响

- 后端:`backend/tools.py`(`AgentToolInput` 字段、`Agent` 工具改走统一解析、结果负载携带定义摘要与告警)、`backend/subagents.py`(`resolve_agent_definition`、`filter_tools_for_subagent` 强制递归封锁、`run_subagent` 接受解析后的定义、payload 摘要)、`backend/subagent_runtime.py`(`AsyncSubagentManager` 并发信号量、任务状态持久化现场定义字段、mailbox 续跑时定义保真)。
- 提示词:`backend/prompts/system_prompt.md`、`backend/prompts/agent_policy.md` 子代理小节追加构造原则。
- 测试:新增 `backend/tests/test_dynamic_subagent_construction.py`,覆盖覆盖语义、递归封锁不可绕过、预算钳制、超限拒绝、未知工具名剔除、并发排队、mailbox 续跑定义保真;既有测试语义不变。
- 不改变 SSE 协议与前端;不引入新依赖;不改模型配置与 `config.json` 结构(护栏上限沿用 `SUBAGENT_MAX_RUNTIME_SECONDS` 一致的模块常量形式)。
