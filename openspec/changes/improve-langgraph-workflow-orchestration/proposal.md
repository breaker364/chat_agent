## Why

当前 Agent 的 LangGraph 部分主要依赖 `create_react_agent` 生成单一的 `agent -> tools -> agent` 循环；任务计划、研究流程、恢复、重试和完成判断分散在应用层。这样可以快速接入工具，但无法用统一的图状态表达任务依赖、并行阶段、人工确认和失败后的重规划，复杂任务也难以审计、恢复和限制工具权限。

现在需要将“模型决定调用什么工具”和“系统决定任务如何推进”分层：由固定拓扑的顶层工作流负责生命周期、路由、调度和完成门禁，由受限的 Executor 与 Research 子图分别负责单任务执行和证据研究。

## What Changes

- 新增一个显式的顶层工作流图，覆盖输入接收、上下文准备、路由、计划、计划校验、调度、汇聚、评估、验证和最终化等阶段。
- 引入结构化 `WorkflowState`、任务记录、依赖关系、尝试次数、证据引用、产物引用、错误和审计事件，并为可并行字段定义 reducer。
- 将单任务的 ReAct 循环封装为 Executor 子图；顶层图只向它传递经过授权的任务上下文和工具集合。
- 将研究规划、证据收集、证据评估和研究完成判断封装为 Research 子图，支持预算、来源去重、证据缺口和有限的再检索。
- 将 route 设计为“结构化 LLM 提议 + schema 校验 + 代码策略裁决”，使 LLM 参与意图识别，但不能直接决定越权工具、非法节点或无限循环。
- 使用任务依赖和 `Send` 实现受上限约束的并行调度，并在 join 节点统一合并结果、错误和指标。
- 增加基于 checkpointer 的节点级恢复、基于 interrupt 的人工确认，以及可审计的 retry/replan 策略。
- 明确顶层节点、子图节点和工具的边界；工具不默认全部暴露给顶层 Agent，工具访问按阶段和任务能力白名单授予。
- 保留现有 `SessionStore`、工具 wrapper、SSE 事件和应用层 API 作为兼容适配层，分阶段迁移现有 ReAct 驱动逻辑。

## Capabilities

### New Capabilities

- `workflow-orchestration`: 定义顶层工作流图的节点、普通边、条件边、任务依赖、并行调度和完成门禁。
- `workflow-state-and-recovery`: 定义结构化工作流状态、状态 reducer、checkpointer、恢复、人工确认和运行审计。
- `executor-subgraph`: 定义单任务 Executor/ReAct 子图、工具授权、执行预算、失败返回和结果契约。
- `research-subgraph`: 定义研究子图的规划、证据收集、评估、去重、预算和证据缺口处理。
- `policy-driven-routing`: 定义结构化路由提议、代码裁决、非法路由降级、重试和重规划规则。

### Modified Capabilities

<!-- 当前 openspec/specs 下没有可复用的主规范，因此本变更不创建 delta spec。现有应用层能力通过兼容适配方式接入新图。 -->

## Impact

- 主要影响 `backend/agent.py`、`backend/agentic_research/`、`backend/subagents.py`、`backend/session_store.py`、`backend/tools.py`、运行时上下文和 SSE 事件适配代码。
- 需要引入或统一 LangGraph 的 `StateGraph`、`Send`、checkpointer、interrupt 和子图编译接口；具体实现应适配项目当前依赖版本。
- 需要新增状态模型、任务调度器、路由 schema、节点/工具能力映射、恢复序列化和测试夹具。
- API 请求与响应保持兼容；内部事件会增加节点、任务和恢复元数据，现有前端事件类型继续可用。
- 运行成本、并发度和持久化量会增加，需要配置最大并行任务数、递归/尝试预算、检查点保留策略和超时。
