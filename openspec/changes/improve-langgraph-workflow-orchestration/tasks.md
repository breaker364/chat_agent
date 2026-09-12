## 1. 契约与迁移基础

- [x] 1.1 锁定当前 LangGraph/LangChain 依赖版本及可用的 `StateGraph`、`Send`、子图、checkpointer 和 interrupt API，补充一个版本适配模块。
- [x] 1.2 定义版本化的 `WorkflowState`、`RequestContext`、`RouteDecision`、`PlanSnapshot`、`TaskRef`、`TaskAttempt`、`TaskResult`、`ResearchResult`、`ArtifactRef`、`WorkflowError` 和 `AuditEvent` 类型。
- [x] 1.3 为 `messages`、任务结果、证据、产物、错误和审计事件实现并发 reducer，并为重复、迟到和冲突更新补充单元测试。
- [x] 1.4 实现现有 `SessionStore` 任务计划、阶段结果、primary result、产物和历史到 `WorkflowState` 的读取映射，以及工作流结果到现有持久化结构的写回映射。
- [x] 1.5 增加工作流 feature flag、schema 版本和运行 thread 标识生成逻辑，确保同一 session 的不同 run 不共用 checkpoint。

## 2. 路由与计划校验

- [x] 2.1 定义 `RouteProposal` 的结构化 schema、枚举值、置信度范围和任务/能力数量限制。
- [x] 2.2 实现 `route` 节点的 LLM 结构化调用，并将原始输出转换为不包含任意节点名、边名和工具名的提议对象。
- [x] 2.3 实现纯代码 `enforce_route`，完成请求完整性、能力注册、知识策略、风险、并发、预算和人工确认条件校验。
- [x] 2.4 实现低置信度、schema 错误、路由模型错误和策略冲突的确定性降级路径，并为每类降级补充测试。
- [x] 2.5 实现 `planner` 的任务 DAG 输出契约和 `plan_guard` 校验：唯一 ID、依赖存在、无环、能力合法、预算可满足和资源上限。
- [x] 2.6 实现计划版本、有限重规划次数和已完成任务复用规则，验证重规划不会重复执行有效结果。

## 3. 顶层工作流图

- [x] 3.1 创建 `build_workflow_graph` 工厂，注册 `intake`、`prepare_context`、`route`、`planner`、`plan_guard`、`scheduler`、`dispatch_task`、`join`、`assess`、`validate` 和 `finalize` 系统节点。
- [x] 3.2 注册简单请求、计划请求、研究请求、澄清、审批、完成、重试、重规划和 blocked 的普通边与条件边，并增加图静态拓扑测试。
- [x] 3.3 实现 `intake` 和 `prepare_context`，复用 canonical session history、知识策略、运行上下文和压缩后的输入，避免将客户端快照当作事实源。
- [x] 3.4 实现 `scheduler` 的 ready 任务计算、最大并行数限制、预算扣减和 batch ID 管理。
- [x] 3.5 使用 `Send("dispatch_task", ...)` 实现任务 fan-out，并实现带预期任务集合和 batch ID 的 `join` barrier，覆盖零任务、单任务和多任务场景。
- [x] 3.6 实现 `assess`、`validate` 和 `finalize` 的完成门禁，确保未验证结果、缺失产物或未解决阻塞不能生成 completed 响应。
- [x] 3.7 实现调度层的 retry、replan、blocked 和取消状态转移，并为预算耗尽和重复分发补充集成测试。

## 4. Executor 子图

- [x] 4.1 创建单任务 Executor 子图及输入/输出适配器，定义 `executor_prepare`、`executor_agent`、`executor_tools` 和 `executor_validate` 节点。
- [x] 4.2 将现有 ReAct Agent 能力接入 `executor_agent`，确保每个子图实例只接收一个任务的局部上下文和消息历史。
- [x] 4.3 基于任务能力和工具元数据构造 allowlist，排除父图控制工具、未注册能力和未获审批的副作用工具。
- [x] 4.4 在 Executor 工具调用边界复用现有工具 wrapper 的去重、幂等键、预算、超时、权限和副作用保护。
- [x] 4.5 实现 Executor 输出契约校验、瞬时错误 retry、终态错误返回和预算耗尽返回，并验证策略拒绝不会通过重试放权。
- [x] 4.6 实现 `dispatch_task` 到 Executor 子图的任务结果映射，验证子图不能直接修改父图计划、其他任务或图拓扑。

## 5. Research 子图

- [x] 5.1 将 `backend/agentic_research/` 的研究模型和预算对象适配为 LangGraph Research 子图状态。
- [x] 5.2 创建 `research_prepare`、`research_plan`、`collect_evidence`、`assess_evidence` 和 `research_finalize` 节点及 refine/next-source 条件边。
- [x] 5.3 为研究工具建立只读能力映射，阻止 Research Agent 调用父图控制能力和写入能力。
- [x] 5.4 实现证据稳定标识、来源与内容摘要去重、引用关联、可信度/时效字段和预算统计。
- [x] 5.5 实现证据充分、冲突、缺口、来源不可用和预算耗尽的 ResearchResult 状态，并为父图提供有限结果契约。
- [x] 5.6 集成 `dispatch_task` 与 Research 子图，验证研究中间消息不会污染父图主会话，且部分研究结果可触发 assess/replan。

## 6. Checkpoint、审批与持久化

- [x] 6.1 实现 checkpointer 注入和按 run thread 保存节点级 checkpoint，记录 schema 版本、阶段、任务和尝试元数据。
- [x] 6.2 实现从最近 checkpoint 恢复的运行入口，并验证进程中断后不会重复提交已完成副作用任务。
- [x] 6.3 实现 `approval_gate` 与 `interrupt()`，持久化待确认 payload、策略原因、任务 ID、过期时间和 checkpoint 引用。
- [x] 6.4 增加批准、拒绝和过期的恢复 API/服务层适配，验证恢复操作与原 run 匹配且具备幂等性。
- [x] 6.5 保持 `SessionStore` 作为跨 run 业务事实源，增加 checkpoint 状态与 session 业务结果之间的一致性测试。

## 7. 事件、API 兼容与可观测性

- [x] 7.1 定义统一的 graph/node/subgraph/task/tool 生命周期事件模型，包含 graph path、node、task ID、attempt、状态、错误和时间戳。
- [x] 7.2 扩展现有 SSE 适配器，将新事件映射到现有 `debug`、`activity`、`progress`、`tool_call`、`tool_result` 和 `done` 类型，并保持旧字段兼容。
- [x] 7.3 将运行中追加指令接入模型调用边界或预定义控制节点，验证追加不会跳过工具权限、审批和状态 reducer。
- [x] 7.4 为路由、调度、子图调用、checkpoint、审批、重试、重规划和最终化补充结构化日志与指标。
- [x] 7.5 增加端到端 parity 测试：新图与旧 ReAct 路径对简单请求、工具请求、工具失败和历史恢复的 API/SSE 结果兼容。

## 8. 灰度、回滚与验收

- [x] 8.1 增加新旧图入口切换配置，默认先在受控请求范围启用新图，并记录图版本和运行路径。
- [x] 8.2 建立回滚测试：关闭 feature flag 后旧路径可读取兼容历史和任务摘要，且不删除新图产生的 checkpoint 或业务结果。
- [x] 8.3 执行并记录并行任务、循环计划、路由异常、权限越权、审批拒绝、进程重启、预算耗尽和迟到结果测试。
- [x] 8.4 验证验收指标：任务完成率、错误分类准确性、重复副作用率、路由拒绝率、恢复成功率、端到端延迟、并行度和 checkpoint 体积。
- [x] 8.5 完成文档和运维说明，明确图拓扑变更必须通过代码评审、测试和重新编译部署，Agent 运行时不具备改图权限。
