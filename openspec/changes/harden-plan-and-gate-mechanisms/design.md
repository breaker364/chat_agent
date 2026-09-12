## Context

plan 机制当前分布在两层：默认 ReAct 路径的 legacy 任务计划（`update_task_plan` 工具 + `SessionStore.set_task_plan` + 完成门禁），以及 opt-in 固定拓扑工作流（`route -> planner -> plan_guard -> scheduler -> assess`）。代码审查确认了 proposal.md 列出的七项缺陷，全部为局部实现问题，不涉及图拓扑调整、API 契约变化或新增模块。

关键现状代码位置：

- 重规划输入：`backend/workflow_graph.py` `planner_node`（只读 `request.message` 与 `route_decision`）。
- 死代码兜底：`backend/workflow_runtime.py` `_default_planner`（已定义、未接线）。
- 能力校验基准：`backend/workflow_graph.py` `plan_guard` 用 `available_capabilities`（全部工具能力）；route 批准集在 `WorkflowState.route.required_capabilities`。
- 审批判定：`backend/workflow_policy.py` `enforce_route`（`approval_required = risk_level == "high"`，仅 route 阶段）。
- 状态覆盖：`backend/session_store.py` `set_task_plan` 合并循环对 `status` 无条件覆盖。
- 存储根不一致：`backend/agent.py` 四处 `SessionStore(Path.cwd())`。
- 步数预算：`backend/executor_graph.py` `executor_agent` 默认 `max_steps = 8`，`workflow_graph.dispatch_task` 未传该字段。

## Goals / Non-Goals

**Goals:**

- 重规划能基于上一轮失败证据产出不同的计划。
- 计划阶段对能力越权拥有独立于 planner prompt 的代码级守门。
- planner 失败有确定性降级，blocked 成为最后手段。
- 高危能力出现在计划中时必然经过人工审批。
- legacy 计划的已完成项不可被陈旧快照降级，文档与实现一致。
- 完成门禁与恢复上下文读写正确的会话存储根。
- executor 步数预算可按部署调整，默认值保持现状。

**Non-Goals:**

- 不替换进程内 `MemorySaver` 为持久化 checkpointer（跨 worker 审批恢复另行立项）。
- 不统一 legacy `task_plan.json` 与工作流 `PlanSnapshot` 两套 plan 词汇表。
- 不改变 `enforce_route` 的模式裁决规则、`Send` 并行调度语义和 reducer 行为。
- 不引入计划预览/交互式修订 UI；审批门沿用现有 `interrupt` + approval API。

## Decisions

### 1. 重规划上下文以受界文本注入 planner，而非改 planner 签名

`planner` 参数保持 `(request: str, route: Mapping) -> Sequence[Mapping]` 签名不变。`planner_node` 检测 `control.replans > 0` 或存在失败/尝试次数耗尽的任务结果时，构造一段受界的失败摘要文本（沿用 route prompt 的 4000 字符截断约定），与原始请求拼接后作为第一个参数传入：

- 摘要仅包含确定性与可审计信息：失败任务的 `task_id`/`kind`/`context` 摘要、错误 `category`、attempt 序号；成功任务只列 `task_id` 与状态，不携带完整 output（避免证据噪声与 token 膨胀）。
- 已成功任务的结果必须在摘要中标注为"已完成、勿重复执行"，配合 `assess` 既有语义（replan 不清空 `task_results`，重算 ready 时跳过 succeeded）保证增量重规划。
- 摘要构造是纯函数（如 `_plan_failure_summary(plan, task_results, errors)`），便于单测且不引入实体硬编码。

### 2. planner 降级链：模型 planner -> 确定性单任务 -> blocked

`build_workflow_runtime` 将 `_default_planner` 提升为模块级命名函数 `deterministic_fallback_planner`，由 `build_workflow_graph` 在 `planner_node` 内部持有：

1. 调用注入的 `planner`；异常则记录 `planner_error` 事件与错误；
2. 改调 `deterministic_fallback_planner(request_with_failure_context, route_decision)`，产出单任务计划（kind 按 route mode 映射，capabilities 取 `route.required_capabilities`）；
3. 降级计划同样经过 `plan_guard` 全量校验；校验失败才置 `blocked`。

`planner_node` 返回结构增加事件字段 `planner_fallback_used: true`，保证降级可观测。被移除的语义：原实现中 planner 异常即 blocked 的短路径。

### 3. plan_guard 增加 route 能力子集强制

`plan_guard` 已持有 `state["route"]`。校验顺序调整为先全局注册表、后 route 批准集：

- `validate_plan(tasks, capabilities=available_capabilities, ...)` 保持不变（结构、依赖、环、上限）；
- 新增纯函数 `assert_route_scoped_capabilities(validated_tasks, route_required)`：任务的 `capabilities` 必须是 `route.required_capabilities` 的子集，违例抛 `PlanPolicyError`，事件类别 `invalid_plan`、原因 `capability_outside_route`。

route 决策本身已由 `enforce_route` 保证其能力均在注册表内，因此该检查不会与全局校验冲突。`direct_executor` 合成的单任务计划 capabilities 同样取自 route 批准集，天然满足约束。

### 4. 计划级风险重评复用 approval_gate，不新增节点

在 `plan_guard` 校验通过后计算计划级风险：若任一任务的 `capabilities` 与既有高危能力判定重叠，或 `plan_guard` 入参显式声明（本变更内仅以能力判定为准，不发明新的风险分类器），则将 `route.approval_required` 置真。高危能力判定集中为 `workflow_policy.py` 的一个常量集合 `HIGH_RISK_CAPABILITY_HINTS`（如写文件、执行脚本、审批类副作用能力标识，均为能力名模式而非实体名），部署可通过 `workflow.tool_capabilities` 覆盖工具能力归属来间接调整。

`decide_after_plan_guard` 逻辑不变（读 `approval_required`），高危计划自然流入既有 `approval_gate` interrupt，不新增节点或边，checkpoint 恢复协议不变。

### 5. executor 步数预算进入 WorkflowConfig

`WorkflowConfig` 白名单新增 `executor_max_steps`（整数，界 [1, 64]，默认 8）。`WorkflowRuntime.stream_events` 不涉及该值；接线点在 `build_workflow_runtime`：把配置传入 `build_workflow_graph`（新关键字参数 `executor_max_steps`），`dispatch_task` 构造 executor 输入时附加 `"max_steps": executor_max_steps`。executor 子图本身已读取 `state["max_steps"]`，无需改动；非工作流路径不受影响。

### 6. legacy 计划拒绝已完成项降级

`set_task_plan` 合并循环中，对 `existing.status == "completed"` 的任务：

- incoming `status` 为 `pending`/`in_progress`/`failed` 时保留 `completed`，追加 warning `Refused status downgrade for completed task {task_id}`；
- `details`/`result_ref` 仍允许更新（补充说明与结果引用是合法操作）。

`update_task_plan` 工具返回 JSON 已包含完整 `warnings`（经 `load_task_plan`），无需改工具层。`update_task_plan_todo` 单项更新路径同样拒绝把 completed 改回 pending/in_progress，行为对齐。该约束只锁状态回退，不阻止 completed -> completed 的刷新，也不影响 `plan_changed` 增删 pending 项的既有规则。

### 7. 存储根统一：workspace root 从构建参数流向消费点

`build_agent` 已持有 `workspace` 参数。将 `stream_agent_events` 闭包内的 `SessionStore(Path.cwd())` 全部替换为构建期捕获的 `SessionStore(workspace)`（或在 `build_agent` 内构造一次并闭包引用），覆盖四类调用点：追加命令事件记录、resume 上下文读取、完成门禁读取、同步 `/chat` 路由摘要读取。`tools.py` 内 `_workspace_root()` 机制不变。行为兼容性：当服务工作目录等于 workspace root（当前部署默认）时结果不变；不等于时修复的是错误读写。

## Risks / Trade-offs

- 重规划摘要会占用 planner prompt 预算；以 4000 字符截断与"只列失败任务详情"控制上界，最坏情况退化为现状（相同计划再失败一次后被 blocked）。
- route 能力子集强制会让"planner 自作主张扩大能力"从静默通过变为计划被拒。为避免误伤，`ModelTaskPlanner` 的能力校验保持不变，且降级 planner 恒产出 route 批准集内的计划。
- 高危能力判定集合是能力名模式，依赖工具声明的准确性；无法覆盖未声明能力的工具，但未声明能力的工具本就无法进入任何任务（executor 拒绝空能力工具）。
- `set_task_plan` 拒绝降级是对模型行为的收紧；已有测试中如存在依赖降级语义的用例需同步修正，预计改动集中在测试夹具而非生产路径。
- `SessionStore` 构造点从惰性 `Path.cwd()` 改为构建期注入，需确认 `build_agent` 在测试中以临时 workspace 调用，避免测试间串目录。

## Migration Plan

1. 先合入纯增量修复（重规划上下文、plan_guard 子集校验、executor 预算、存储根统一），全部有单测，行为对现有通过测试无影响。
2. 再合入两条行为收紧（planner 降级链、completed 不可降级），更新受影响测试并在 `test_workflow_orchestration.py`、`test_session_store.py` 补充回归用例。
3. 计划级风险重评最后合入：默认 `HIGH_RISK_CAPABILITY_HINTS` 初始为空集时行为与现状完全一致，部署按需在配置中启用，规避 rollout 期审批面突增。

全部变更在 `workflow.enabled=false` 的默认路径上仅影响第 6、7 两项；工作流路径整体保持 flag 关闭即可回滚。

## Open Questions

- 高危能力判定是否应扩展为 per-tool 配置（`workflow.tool_capabilities` 增加 risk 维度）而非全局常量集合？暂以常量集合起步，留待真实审批数据反馈。
- 重规划摘要是否需要结构化（JSON）而非文本注入？当前 planner 输入本就是自由文本 prompt，文本方案更简单；若后续 planner 改为结构化输出调用，再一并调整。
