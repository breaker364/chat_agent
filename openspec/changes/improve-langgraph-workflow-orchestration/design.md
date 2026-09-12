## Context

当前运行路径使用 `create_react_agent` 构建预编译 ReAct 图，核心拓扑是 `agent -> tools -> agent`。模型节点负责决定是否产生工具调用，工具节点负责执行调用；会话历史、任务计划、研究状态、重试和 SSE 事件主要由应用层维护。`backend/agentic_research/` 也已经具备研究规划、证据收集和评估能力，但它是独立状态机，还没有成为主图的 LangGraph 子图。

目标架构需要把两类决策分开：

- 系统级工作流节点决定当前阶段、任务依赖、并行度、恢复和完成条件。
- 子图内部的 Agent 决定如何完成一个已授权任务，例如选择哪个工具以及是否继续推理。

设计必须兼容现有 `SessionStore`、工具运行时包装器、运行中追加指令和 SSE 事件；图拓扑在应用启动时编译后固定，运行时状态只能触发预定义节点和边。当前项目对实体不做硬编码，能力和工具分类应由配置、工具元数据和通用策略驱动。

## Goals / Non-Goals

**Goals:**

- 用显式 `StateGraph` 表达顶层工作流生命周期、条件路由、任务依赖和完成门禁。
- 使用结构化状态和 reducer 支持并行任务结果、证据、错误和审计事件的安全合并。
- 将单任务 ReAct 能力封装为 Executor 子图，将研究流程封装为 Research 子图。
- 让 route 使用结构化模型提议，但由 schema 和确定性代码策略做最终裁决。
- 支持有限重试、重规划、节点级检查点恢复和高风险任务人工确认。
- 为工具建立按阶段、按任务能力授予的访问边界，并兼容现有工具 wrapper 的去重和副作用保护。
- 保持现有 API 入口和前端 SSE 事件兼容，同时补充图路径、节点、任务和尝试元数据。

**Non-Goals:**

- 不允许 Agent 在运行时创建、删除或改写 LangGraph 节点和边。
- 不在本变更中重写所有业务工具、MCP 适配器、RAG 存储或文档处理实现。
- 不把所有请求都强制转换为多任务计划；简单请求仍可以走轻量路径。
- 不以 LangGraph checkpoint 替换长期会话事实源；`SessionStore` 仍保存跨运行的业务结果和会话历史。
- 不实现跨进程分布式调度集群；第一阶段只保证单服务实例内的受控并发。

## Decisions

### 1. 使用固定拓扑的顶层 `StateGraph`

顶层图编译时注册以下系统节点：

```text
START
  -> intake
  -> prepare_context
  -> route
       ├─ direct       -> direct_executor
       ├─ planned      -> planner -> plan_guard
       ├─ research     -> planner -> plan_guard
       └─ clarify      -> clarification_gate
  -> approval_gate?    (高风险或副作用计划才进入)
  -> scheduler
  -> dispatch_task     (由 Send 扇出，可执行多个实例)
  -> join
  -> assess
       ├─ complete      -> validate -> finalize -> END
       ├─ retry         -> scheduler
       ├─ replan        -> planner -> plan_guard
       └─ blocked       -> finalize -> END
```

`direct_executor` 只处理不需要显式任务分解的请求；复杂请求经过 `planner` 产生带依赖关系的任务 DAG。`dispatch_task` 是父图中的统一适配节点：根据任务类型调用 Executor 子图或 Research 子图，但不把子图内部节点提升为父图节点。`join` 是批次屏障，只有当前批次的所有已分发任务进入终态，或明确产生不可恢复失败时，才允许进入 `assess`。

选择显式 `StateGraph` 是因为节点和边可以被静态检查、审计和测试；继续使用一个黑盒 `create_react_agent` 只能观察工具调用结果，无法在图层面表达任务依赖和人工确认。保留预构建 ReAct 图作为 Executor 内部实现，以降低迁移风险。

### 2. 采用结构化 `WorkflowState`，并把并行字段设计为 reducer

顶层状态建议抽象为以下逻辑结构，实际类型可使用 `TypedDict`、Pydantic model 或项目当前兼容形式：

```python
class WorkflowState:
    request: RequestContext
    route: RouteDecision
    plan: PlanSnapshot
    ready_tasks: list[TaskRef]
    running_tasks: dict[str, TaskAttempt]
    task_results: dict[str, TaskResult]
    research: ResearchSnapshot
    artifacts: dict[str, ArtifactRef]
    errors: list[WorkflowError]
    events: list[AuditEvent]
    approval: ApprovalState
    response: FinalResponse | None
    control: ControlState
    messages: list[BaseMessage]
```

字段更新规则如下：

- `messages` 使用 LangGraph 的消息追加 reducer，保留工具消息配对关系；任务子图使用自己的局部消息，不把全部推理消息回写到父图。
- `task_results` 按 `task_id` 合并。相同任务只能接受同一 `attempt_id` 的最终结果，迟到结果必须进入审计但不能覆盖更新的尝试。
- `research.evidence` 按稳定的证据键去重；同一来源的重复观察合并引用信息，不重复计入预算。
- `artifacts` 按 artifact 标识去重，并保留验证状态和生产任务引用。
- `errors`、`events` 使用有界追加 reducer；单个 reducer 不执行外部副作用。
- `control`、`route`、`plan`、`approval` 等控制字段只允许系统节点以整对象方式更新，避免并行分支互相覆盖。

状态中的 `session_id`、`run_id`、策略和预算都从请求上下文传入，不由模型生成。每个任务必须具有 `task_id`、`kind`、`depends_on`、`capabilities`、`status`、`attempt` 和 `idempotency_key`；调度器只分发依赖已满足且状态为 ready 的任务。

### 3. 用“LLM 提议 + 代码裁决”实现 route

`route` 节点可以调用一个只输出结构化 `RouteProposal` 的模型，字段限定为枚举和受限数值，例如 `mode`、`task_kinds`、`needs_research`、`risk_level`、`confidence` 和 `required_capabilities`。模型不能返回任意节点名称、工具名称或边名称。

随后由纯代码 `enforce_route()` 完成：schema 校验、请求完整性检查、能力映射、策略过滤、最大任务数/并发数限制、风险升级和默认降级。非法或低置信度提议进入 `clarification_gate` 或安全的 `planned` 路径；高风险任务进入 `approval_gate`。条件边只读取裁决后的枚举值，不直接读取原始 LLM 文本。

选择混合路由是因为纯代码难以识别开放式意图，纯 LLM 又无法提供稳定的安全边界。路由节点属于系统内部节点，不作为普通工具暴露给 Executor Agent；Executor Agent 只能在已经确定的任务内做工具选择。

### 4. 以任务 DAG 和 `Send` 实现受控并行

`planner` 输出任务 DAG 后，`plan_guard` 做以下确定性检查：任务 ID 唯一、依赖存在、图无环、能力合法、预算可满足、任务数和估算并发度不超限。`scheduler` 每一轮计算 ready 集合，并返回多个 `Send("dispatch_task", task_input)`，每个分支携带不可变的任务快照、授权能力和尝试标识。

`dispatch_task` 对每个分支调用相应子图。分支返回只包含任务结果、产物引用、消耗预算、错误和审计事件的增量；`join` 用批次 ID 和预期任务集合实现屏障，合并完成、失败和取消状态。对于依赖未满足的任务，`join` 不提前分发，下一轮由 `scheduler` 继续计算。

选择 `Send` 是为了使用 LangGraph 原生的 fan-out/fan-in 语义；不直接在节点内部创建裸 `asyncio.gather`，以便每个分支具有可追踪的图运行上下文、错误边界和 checkpoint。并行度由配置和当前预算共同限制，不能由模型任意扩大。

### 5. Executor 子图只处理一个授权任务

Executor 子图的输入/输出契约如下：

```text
输入：task、task_context、allowed_capabilities、attempt_budget、prior_result
输出：TaskResult、artifacts、usage、errors、audit_events
```

推荐内部节点为：

```text
executor_prepare -> executor_agent -> executor_tools -> executor_agent
                                      └─ 无 tool_calls -> executor_validate
executor_validate -> success | retryable_failure | terminal_failure
```

`executor_agent` 可以继续使用 `create_react_agent` 或等价的 ReAct 子图。绑定工具时只传入任务能力映射得到的工具集合，并对每次工具调用再次执行 wrapper 的预算、去重、权限和副作用检查。Executor 不可调用 `planner`、`scheduler` 或任意父图控制节点；它只能返回结果和状态，不直接改变父图拓扑或其他任务状态。

### 6. Research 子图负责证据闭环

Research 子图将现有研究状态机能力适配到 LangGraph 状态契约，内部流程为：

```text
research_prepare -> research_plan -> collect_evidence -> assess_evidence
                                         ^                 |
                                         |-- refine -------|
                                         |-- next source --|
                                         └-- ready/blocked -> research_finalize
```

研究规划只产生查询、来源策略、停止条件和预算；证据收集节点调用只读检索/抓取能力；评估节点判断证据充分性、冲突、时效和缺口。证据按来源与内容摘要去重，单个来源和整个研究任务都必须遵守预算。研究子图输出 `ResearchResult`，其中包含结论、证据引用、未解决缺口和可信度，不直接生成未经验证的最终回答。

Research 子图可以被 `dispatch_task` 调用，也可以作为独立内部服务复用。父图只接收其结果摘要和引用，避免将研究过程的所有中间消息污染主会话上下文。

### 7. 失败处理采用分层 retry/replan

- 工具瞬时错误：在工具 wrapper 或 Executor 子图内按任务预算重试，使用同一幂等键避免重复副作用。
- 任务可恢复失败：`assess` 将任务标记为 retryable，`scheduler` 生成新的 attempt；达到上限后转为 terminal failure。
- 依赖或计划失败：`assess` 生成受限的 replan 请求，回到 `planner`，保留已完成任务结果，不重复执行仍然有效的任务。
- 策略、权限或校验失败：不自动放宽能力，转为 clarification、approval 或 blocked。
- 模型没有产生可用最终结果：沿用现有完成审计的兼容逻辑，但把继续执行表示为预定义的 `assess -> scheduler` 或 `assess -> validate` 边。

每一类重试都记录原因、attempt、延迟、预算和最终裁决。重试次数、重规划次数和总运行步数均有硬上限。

### 8. 使用 checkpoint 与 interrupt 分离运行恢复和业务持久化

编译顶层图时注入项目选定的 checkpointer。调用配置使用由 `session_id` 和 `run_id` 组成的稳定 thread 标识，并在 checkpoint metadata 中写入任务、阶段和 schema 版本。恢复同一运行时优先从 checkpoint 继续；新的用户回合创建新的 run，但从 `SessionStore` 读取已确认的业务历史、任务结果和产物。

高风险副作用任务在 `approval_gate` 通过 `interrupt()` 暂停，向 SSE 输出 approval-needed 事件。用户确认或拒绝后，通过 LangGraph resume 命令恢复同一 checkpoint；拒绝会生成终止结果，不会绕过策略直接执行工具。没有人工确认时，低风险只读任务不进入 interrupt。

### 9. 保持应用层适配和事件兼容

顶层节点和子图发出统一的内部事件：`graph_path`、`node`、`task_id`、`attempt`、`status`、`timestamp`、`error` 和可选 payload。SSE 适配器将其映射到现有 `debug`、`activity`、`progress`、`tool_call`、`tool_result`、`done` 事件；新增字段采用可选方式，旧客户端仍可忽略。

`SessionStore` 继续保存 canonical 会话、任务计划、阶段结果、primary result 和产物索引。LangGraph state 作为一次运行的编排状态，完成或失败时由持久化适配器提交业务摘要；两者通过 `run_id` 和 `task_id` 关联。现有工具 wrapper 继续作为最后一道执行边界，避免迁移后失去去重和副作用保护。

### 10. 编译与运行接口

建议提供一个单一的 `build_workflow_graph()` 工厂，由启动阶段完成：状态 schema 注册、子图编译、系统节点注册、条件边注册、checkpointer 注入和图静态校验。运行时只调用编译结果：

```python
graph = build_workflow_graph(model_registry, tool_registry, checkpointer)
result = await graph.ainvoke(
    initial_state,
    config={
        "configurable": {"thread_id": run_thread_id},
        "recursion_limit": workflow_step_limit,
    },
)
```

工具注册表提供工具能力元数据、是否只读、所需确认级别、超时和幂等策略；它不允许单个请求动态注册新的 LangGraph 节点或边。若需增加新阶段，必须修改代码、测试并重新编译部署。

## Risks / Trade-offs

- [状态迁移复杂度] 现有应用层计划和新 `WorkflowState` 可能短期双写 → 先定义版本化映射，保留旧字段读取，完成一轮 parity 后再关闭旧写入路径。
- [并行合并冲突] 多分支可能更新同一任务或同一产物 → 以 `task_id`、`attempt_id` 和稳定 artifact 标识做幂等合并，拒绝迟到覆盖。
- [checkpoint 成本] 节点级 checkpoint 会增加存储和序列化开销 → 配置保留期限，限制消息/事件体积，并只持久化必要状态和摘要。
- [LLM 路由不稳定] 模型提议可能误判任务类型 → 使用严格 schema、置信度阈值、代码裁决和安全默认路径；路由决策写入审计便于回放。
- [工具权限遗漏] 任务能力映射不完整会导致任务无法完成 → 建立工具元数据校验、能力覆盖测试和只读 fallback；禁止通过运行时 prompt 放开权限。
- [重复副作用] retry 或恢复可能再次触发写操作 → 复用现有工具 wrapper 的幂等键和重复写阻断，要求副作用工具返回可持久化操作标识。
- [拓扑增大带来维护成本] 顶层节点更多、调试路径更长 → 节点保持单一职责，统一事件格式，提供 graph snapshot、状态转移和失败路径测试。
- [依赖版本差异] `Send`、子图、checkpointer 和 interrupt API 可能随 LangGraph 版本变化 → 在依赖锁定版本上建立适配层和集成测试，禁止业务代码直接散落版本特定调用。

## Migration Plan

1. 增加状态、路由、任务和工具能力契约，并实现旧应用状态与新状态的双向映射。
2. 在 feature flag 下构建顶层图，先让 `direct_executor` 包装现有 ReAct Agent，验证普通请求的输出和工具事件 parity。
3. 接入 planner、plan guard、scheduler、join 和 assess，先限制为串行，再开启受控 `Send` 并行。
4. 将现有研究 orchestrator 适配为 Research 子图，增加证据去重、预算和研究结果合并测试。
5. 接入 checkpointer、approval interrupt 和恢复 API，验证中断、进程重启、重试和拒绝路径。
6. 将默认请求切换到新图；观察错误率、工具重复率、端到端延迟、checkpoint 体积和任务完成率。
7. 保留旧 ReAct 路径作为可回滚开关。回滚只切换入口选择，不删除 checkpoint 或 SessionStore 数据；旧路径读取经过兼容映射的历史和任务摘要。

## Open Questions

- checkpointer 的具体持久化后端应与部署环境已有存储能力对齐；默认优先选择支持事务和按 thread 查询的后端。
- approval interrupt 的确认入口可复用现有 SSE/API，但需要确定确认请求的幂等键和过期策略。
- 现有工具需要补齐哪些能力元数据，应通过启动时注册校验报告缺失项；缺失元数据的工具默认不进入副作用任务集合。
- 需要基于实际 LangGraph 依赖版本确定 `Send` fan-in 屏障的实现细节；若原生 fan-in 语义不足，使用显式 batch barrier 节点和计数 reducer。
- 是否将所有历史消息纳入 checkpoint，还是只保留窗口和摘要，应以恢复正确性与存储成本测试结果确定；默认保留工具协议完整的最近窗口。
