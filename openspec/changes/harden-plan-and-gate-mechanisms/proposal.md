## Why

2026-09-12 对 plan 机制的代码审查发现，`improve-langgraph-workflow-orchestration` 交付的固定拓扑工作流和既有 legacy 任务计划存在若干实现缺陷，当前仅靠 prompt 约束或文档声明兜底，缺少代码级强制：

1. **重规划是盲的**：`workflow_graph.py` 的 `planner_node` 在 `assess -> replan` 路径上仍只读取原始请求和路由决策，上一轮 `task_results` 的失败原因、错误类别和已完成进度不会传给 planner。重规划无法从失败中学习，大概率产出相同计划并再次失败，最终被 `max_replans` 截断。
2. **planner 失败无降级**：planner 抛异常直接置 `blocked` 进入 finalize；`workflow_runtime._default_planner` 是确定性兜底方案但从未接线，属于死代码。
3. **plan_guard 的能力校验比 planner 宽松**：`plan_guard` 用全部工具的能力注册表校验计划，而 route 批准的能力子集约束只依赖 `ModelTaskPlanner` 自身的 prompt 自检。`build_workflow_graph` 的 `planner` 参数接受任意 callable，一旦注入自定义 planner，route 级别的越权在计划阶段没有守门。
4. **审批不重评计划级风险**：`approval_required` 只在 route 阶段按 `risk_level == high` 判定；计划产出后即使任务请求高危能力也不会触发审批。
5. **legacy 计划的不可降级声明与实现不符**：`update_task_plan` 工具文档承诺已完成项不可降级，但 `SessionStore.set_task_plan` 对 `status` 无条件覆盖，模型提交陈旧快照即可把 completed 项改回 pending。
6. **完成门禁使用错误的存储根**：`agent.py` 内多处 `SessionStore(Path.cwd())`（含完成门禁与恢复上下文）与 `build_agent` 的 `workspace` 参数不一致，服务工作目录与 workspace root 不同时会读写错误会话目录。
7. **executor 步数预算不可配置**：executor 子图 `max_steps` 固定默认值 8，dispatch 未从 `WorkflowConfig` 传入，部署无法按模型与任务类型调整预算。

## What Changes

- 重规划路径将失败摘要（失败任务的 context、错误类别、已成功任务清单）注入 planner 输入。
- planner 异常时先走确定性降级（复用 `_default_planner` 生成单任务计划），降级仍失败才 blocked；移除死代码语义。
- `plan_guard` 在全局能力注册表校验之外，增加对 route 批准能力子集的第二层强制。
- 计划校验后重评任务级风险：计划包含高危能力的任务时设置 `approval_required` 并进入审批门。
- executor 步数预算进入 `WorkflowConfig`（`executor_max_steps`）并沿 dispatch 传入。
- `SessionStore.set_task_plan` 拒绝把已完成任务降级为 pending/in_progress，拒绝时记录 warning 并保留原状态。
- `agent.py` 的完成门禁、恢复上下文和追加命令记录统一改用 `build_agent` 传入的 workspace root 构造 `SessionStore`。

## Capabilities

### New Capabilities

- `session-task-plan`: legacy 任务计划的不可变性、状态迁移保护与消费方（完成门禁、恢复上下文）的存储一致性。

### Modified Capabilities

- `workflow-orchestration`: 重规划失败上下文与 planner 确定性降级。
- `policy-driven-routing`: plan_guard 的 route 能力子集强制与计划级风险重评。
- `executor-subgraph`: 可配置的执行步数预算。

说明：主 specs 目录尚未归档上述 capability，本变更按既有约定以 ADDED Requirements 形式落在同名 capability 目录；归档本变更前需先归档 `improve-langgraph-workflow-orchestration`。

## Impact

- 代码影响集中在 `backend/workflow_graph.py`、`backend/workflow_runtime.py`、`backend/workflow_policy.py`、`backend/config.py`、`backend/session_store.py`、`backend/agent.py`；不新增模块。
- `WorkflowConfig` 新增一个有界整数键，`load_workflow_config` 白名单与文档同步更新；默认值保持当前行为（8 步）。
- `set_task_plan` 新增状态回退拒绝属于行为变更：此前依赖降级完成的调用会收到 warning 与保留状态，需在工具返回 JSON 中透出。
- 不改变 API 请求/响应契约、SSE 事件词汇表和 checkpoint 语义；`MemorySaver` 进程内限制与两套 plan 词汇表的统一不在本变更范围内。
