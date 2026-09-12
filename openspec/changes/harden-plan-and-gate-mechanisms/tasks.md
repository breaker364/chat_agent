## 1. 重规划失败上下文

- [ ] 1.1 在 `backend/tests/test_workflow_orchestration.py` 编写失败测试：注入捕获 planner 输入的伪 planner，构造含失败任务结果的 state 走 `assess -> replan -> planner`，断言 planner 收到的请求文本包含失败任务的 task_id 与错误类别、包含已成功任务与"勿重复执行"标注、不包含完整任务输出。
- [ ] 1.2 在 `backend/workflow_graph.py` 新增纯函数 `_plan_failure_summary(plan, task_results, errors)`：仅提取失败任务的标识/类型/上下文摘要/错误类别/尝试序号与成功任务标识，按 4000 字符截断；无失败证据时返回空串。
- [ ] 1.3 修改 `planner_node`：当存在失败证据时把摘要与原始请求拼接后传入 planner，事件中记录 `failure_context_chars`；确认无失败证据路径行为不变。
- [ ] 1.4 补充截断边界测试（失败任务数超量、超长 context）与重规划后成功任务不被重复分度的回归测试。

## 2. planner 确定性降级链

- [ ] 2.1 在 `backend/tests/test_workflow_orchestration.py` 编写失败测试：伪 planner 抛异常时，`planner` 节点产出经 plan_guard 校验通过的单任务计划；兜底也失败时置 blocked 并记录 planner_error。
- [ ] 2.2 将 `backend/workflow_runtime.py` 的 `_default_planner` 重命名导出为 `deterministic_fallback_planner` 并补充 docstring，删除"planner 异常即 blocked"的短路径。
- [ ] 2.3 在 `build_workflow_graph` 接收兜底 planner（默认 `deterministic_fallback_planner`），`planner_node` 实现"模型 planner -> 兜底 -> blocked"降级链，事件携带 `planner_fallback_used` 标记。
- [ ] 2.4 验证兜底计划同样经过 `validate_plan` 全量校验；补充兜底计划含越权能力时被 plan_guard 拒绝的测试。

## 3. plan_guard 能力子集强制

- [ ] 3.1 在 `backend/tests/test_workflow_orchestration.py` 编写失败测试：任务能力在全局注册表内但不在 route 批准集内时 plan_guard 拒绝，事件原因为 `capability_outside_route`。
- [ ] 3.2 在 `backend/workflow_policy.py` 新增纯函数 `assert_route_scoped_capabilities(tasks, route_required)`，违例抛 `PlanPolicyError`。
- [ ] 3.3 在 `plan_guard` 全局校验通过后调用该函数（route 批准集取自 `state["route"]["required_capabilities"]`）；确认 `direct_executor` 合成计划天然满足约束。
- [ ] 3.4 补充"自定义 planner 不做事前约束时越权仍被拦截"的注入测试与"子集计划正常放行"的正向测试。

## 4. 计划级风险重评

- [ ] 4.1 在 `backend/tests/test_workflow_orchestration.py` 编写失败测试：高危判定非空且计划命中时，`plan_guard` 后进入 approval_gate；判定为空时行为与现状一致。
- [ ] 4.2 在 `backend/workflow_policy.py` 定义常量集合 `HIGH_RISK_CAPABILITY_HINTS`（能力标识模式，初始为空集），并提供纯函数 `plan_requires_approval(tasks, hints)`。
- [ ] 4.3 在 `plan_guard` 校验通过后调用该函数，命中时将 `route.approval_required` 置真；`decide_after_plan_guard` 与 approval_gate 逻辑不变。
- [ ] 4.4 补充审批拒绝高危计划的路径测试，确认不调度任何任务。

## 5. executor 步数预算配置化

- [ ] 5.1 在 `backend/tests/test_workflow_orchestration.py` 编写失败测试：配置 `executor_max_steps` 后 executor 在第 N 步触发 `budget_exhausted`；缺省时保持默认 8。
- [ ] 5.2 在 `backend/config.py` 的 `load_workflow_config` 白名单新增 `executor_max_steps`（整数，界 [1, 64]，默认 8），越界抛 `WorkflowConfigError`。
- [ ] 5.3 `build_workflow_runtime` 将配置传入 `build_workflow_graph` 新关键字参数 `executor_max_steps`，`dispatch_task` 构造 executor 输入时附加该字段；executor 子图无需改动。
- [ ] 5.4 补充配置越界拒绝与非法类型拒绝的单元测试。

## 6. legacy 计划状态保护与存储根统一

- [ ] 6.1 在 `backend/tests/test_session_store.py` 编写失败测试：快照把 completed 任务状态写回 pending/in_progress/failed 时保持 completed 并产生 warning；details/result_ref 更新被接受；单项更新路径同样拒绝回退。
- [ ] 6.2 修改 `backend/session_store.py` `set_task_plan` 合并循环与 `update_task_plan_todo`：completed 任务拒绝状态回退，warning 文案包含任务标识。
- [ ] 6.3 排查并修正既有测试中依赖降级语义的用例；确认 `update_task_plan` 工具返回 JSON 已透出 warnings。
- [ ] 6.4 在 `backend/tests/test_agent_activity.py`（或就近的 agent 层测试）编写失败测试：workspace root 与工作目录不一致时，完成门禁与恢复上下文读取 workspace 内会话数据。
- [ ] 6.5 修改 `backend/agent.py`：`build_agent` 以 workspace 参数构造 `SessionStore` 并沿闭包传递，替换四处 `SessionStore(Path.cwd())` 调用点；确认默认路径（两者一致）行为不变。

## 7. 收尾验证

- [ ] 7.1 运行全量 `backend/tests`，确认新增与既有用例全部通过。
- [ ] 7.2 更新 `docs/langgraph-state-orchestration.md` 第 15 节：重规划失败上下文、planner 降级链、plan_guard 子集强制、计划级审批重评、executor 预算配置。
- [ ] 7.3 确认 `workflow.enabled=false` 默认路径仅受第 6 节两项影响，并完成一次关闭状态下的冒烟验证。
