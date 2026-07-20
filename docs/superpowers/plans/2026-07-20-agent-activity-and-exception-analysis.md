# Agent 工作动态与异常分析实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在聊天流中显示可读的实时工作动态，并以执行证据生成可降级的异常分析。

**Architecture:** 后端将可测试的异常分析和活动负载构造逻辑从流式编排中分离，向现有 SSE 流追加 `activity` 事件。前端独立聚合活动事件并在流式回答内渲染默认展开的时间线，保持 Debug stream 不变。

**Tech Stack:** Python、pytest、FastAPI SSE、React 19、Vite、Vitest、React Testing Library。

---

### Task 1: 后端测试边界与异常分析

**Files:** `backend/tests/test_agent_activity.py`、`backend/agent.py`

- [ ] 写入失败测试：安全上下文白名单、有效结构化分析优先、异常/无效 JSON/不安全输出的规则化兜底。
- [ ] 运行 `python -m pytest backend/tests/test_agent_activity.py -q`，确认因待测函数不存在而失败。
- [ ] 实现最小的上下文构造、分析校验与安全格式化逻辑。
- [ ] 重跑同一测试并确认通过。

### Task 2: 后端活动 SSE 事件

**Files:** `backend/tests/test_agent_activity.py`、`backend/agent.py`

- [ ] 写入失败测试：`understanding`、`working`、`completed`、`blocked` 负载、无原始参数泄露及终态顺序。
- [ ] 运行后端测试，确认 `activity` 负载或事件顺序导致预期失败。
- [ ] 实现最小 `build_activity_event` 和请求开始、工具生命周期、重试、正常/失败结束的事件发送。
- [ ] 重跑后端测试并确认通过。

### Task 3: 前端测试基础与活动状态

**Files:** `frontend/package.json`、`frontend/vitest.config.js`、`frontend/src/__tests__/agentActivity.test.jsx`、`frontend/src/App.jsx`

- [ ] 写入失败测试：`activity` SSE 解析、同阶段替换、完成/受阻收束和 Debug stream 回归。
- [ ] 运行 `npm --prefix frontend run test -- agentActivity.test.jsx`，确认因测试脚本或活动组件不存在而失败。
- [ ] 实现最小前端活动合并辅助函数和默认展开的 `AgentActivityTimeline`。
- [ ] 重跑前端测试并确认通过。

### Task 4: 样式与验证

**Files:** `frontend/src/App.css`、`openspec/changes/improve-agent-activity-and-exception-analysis/tasks.md`

- [ ] 补充窄屏可读性和 Debug stream 保留的回归断言。
- [ ] 实现最小响应式样式，不更改 Debug stream 数据模型。
- [ ] 运行后端测试、前端测试、前端构建和 OpenSpec 严格校验。
- [ ] 仅在对应验证通过后，将 OpenSpec `tasks.md` 的相关复选框更新为 `[x]`。
