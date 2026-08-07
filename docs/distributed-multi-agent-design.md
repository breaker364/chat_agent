# 分布式多 Agent 改造设计文档

## 1. 文档定位

本文档基于仓库中的 OpenSpec 变更整理，目标是把当前“单进程内多线程/异步 Agent”升级为“可跨进程、跨机器运行的分布式多 Agent”。

本文档重点覆盖：

- 现状与分布式化缺口；
- 推荐的控制面、执行面、状态面和事件面；
- Agent、任务、租约、消息和事件契约；
- session/run 隔离、幂等、重试、取消和故障恢复；
- 从当前实现到分布式运行时的分阶段迁移路径；
- 验收标准、风险和待决策事项。

本文档不是应用代码实现，也不替代后续需要创建的 OpenSpec change。正式实施前，建议以本文档为设计输入，新增一个独立的分布式运行时变更。

## 2. 依据的 OpenSpec 与结论

### 2.1 相关变更

| OpenSpec 变更 | 当前结论 | 对分布式改造的意义 |
|---|---|---|
| `enable-concurrent-sessions` | 35/36 个任务完成，仍有一次跨 session 手工验证未完成 | 已建立 session/run 归属，但运行注册表仍是进程内 map |
| `add-agentic-rag-orchestration` | 已完成 | 已具备规划、证据执行、评估、重规划、预算和安全观测抽象，可作为分布式 Agent 任务类型 |
| `preserve-recent-tool-history` | 已完成 | 工具调用和结果必须保留关联 ID，适合通过事件和持久化 transcript 跨 worker 恢复 |
| `auto-compress-older-context` | 已完成 | 上下文压缩需要以 session 为粒度串行化，分布式后必须从进程内锁升级为共享锁/数据库约束 |
| `improve-agent-activity-and-exception-analysis` | 仍在进行 | activity/debug/error 需要统一事件流和安全降级，分布式后应从共享事件总线读取 |
| `append-command-during-run` | 已完成 | 运行中的追加指令需要从进程内 mailbox 升级为持久化任务消息 |

### 2.2 核心结论

现有系统已经解决了部分“并发语义”，但尚未解决“分布式所有权”：

```text
当前：HTTP 请求
  -> 单进程 FastAPI
  -> 进程内 _ACTIVE_RUNS
  -> 进程内 asyncio.Queue
  -> Thread/asyncio Agent
  -> 本地文件 SessionStore

目标：HTTP 请求
  -> API/Run Coordinator
  -> 共享任务状态与租约
  -> 持久化消息流
  -> 任意 Agent Worker
  -> 共享 session、事件和产物存储
```

因此，分布式改造不能只把 `Thread` 替换为进程或容器。必须把以下进程内对象改为共享、可恢复的组件：

- `_ACTIVE_RUNS`：改为共享的 run registry 和租约；
- `SessionEventHub`：改为持久化事件流或消息总线；
- `AsyncSubagentManager` 的任务字典和 mailbox：改为任务表和持久化消息；
- `ContextVar`：只保留为 worker 内部便利机制，跨服务调用必须使用显式 envelope；
- `SessionStore` 的本地文件写入：改为具备版本控制和并发约束的共享存储；
- 线程内的完成回调：改为可重放的状态事件和幂等结果提交。

## 3. 当前实现分析

### 3.1 当前运行模型

主要实现位置如下：

- `backend/main.py`：聊天入口、SSE、run 注册、运行上下文绑定；
- `backend/runtime_context.py`：worker 内的 session/run `ContextVar`；
- `backend/session_events.py`：进程内 session 事件队列；
- `backend/session_store.py`：本地 session JSON、工具事件和任务计划；
- `backend/subagents.py`：进程内 Thread、任务状态字典和 mailbox；
- `backend/subagent_runtime.py`：subagent manager 单例；
- `backend/agent.py`：主 Agent 事件循环、工具 transcript、RAG 合成和后台 subagent 等待；
- `backend/agentic_research/`：有界的证据规划和重规划；
- `backend/context_compaction.py`：按 session 的进程内压缩锁；
- `backend/run_append.py`：进程内运行中追加指令队列。

### 3.2 分布式化风险

| 风险 | 根因 | 分布式后的表现 |
|---|---|---|
| 重复执行 | 消息至少投递一次但没有统一幂等键 | 同一个工具或子任务可能被两个 worker 执行 |
| split-brain | worker 停顿后旧租约仍认为自己拥有任务 | 两个 worker 同时写同一 run |
| 事件丢失 | 事件只存在于内存队列 | SSE 断线、进程重启后无法补发 |
| 历史覆盖 | 多 worker 直接写同一个 JSON | 后写覆盖先写，工具 transcript 不完整 |
| 上下文串线 | 只依赖本地 `ContextVar` 或隐式线程继承 | 跨服务边界丢失 session/run/parent 信息 |
| 取消失效 | AbortController 只在浏览器和当前进程有效 | 用户停止后，远端 worker 继续执行 |
| 顺序错乱 | 完成回调以到达顺序写入 | 子任务先完成但父任务按错误顺序合并结果 |
| 资源失控 | 每个进程都独立计算并发上限 | 总并发超过模型、工具或租户限制 |

## 4. 目标与非目标

### 4.1 目标

1. 同一服务可以运行多个 Agent Worker，Worker 可部署在不同进程或机器。
2. 一个用户 `session` 可以拥有一个线性的前台 `run`，同时由多个子 Agent 并行处理独立任务。
3. 不同 session 之间完全隔离；同一 session 的状态写入具有明确的顺序和版本约束。
4. 任务、事件和结果可持久化，worker 重启后可以恢复或重试。
5. SSE 断线后可以使用事件序号恢复，而不是重新执行 Agent。
6. 取消、超时、租约过期和 worker 崩溃具有可验证的最终状态。
7. 保留现有 Agentic RAG 的通用 source route、预算和安全边界，不引入实体名称到工具或域名的硬编码映射。
8. 保留 `activity`、`debug`、`tool_call`、`tool_result`、`error`、`done` 等现有用户和调试语义。

### 4.2 非目标

- 不在本阶段允许同一 session 的多个前台用户消息无序并行生成回答。
- 不把所有工具调用都改造成独立服务；工具可以先作为 worker 内的能力执行。
- 不把 Agent 的内部推理链暴露给用户或持久化到事件流。
- 不在业务代码中按具体公司、品牌、学校、人物、地点、产品或域名做路由判断。
- 不要求一次性替换全部存储；迁移期间允许兼容旧 session 数据。

## 5. 推荐目标架构

### 5.1 四个平面

```text
                         ┌──────────────────────┐
                         │       Frontend       │
                         │ SSE + Last-Event-ID  │
                         └──────────┬───────────┘
                                    │
                         ┌──────────▼───────────┐
                         │ API / Stream Gateway  │
                         │ 鉴权、校验、事件回放  │
                         └──────────┬───────────┘
                                    │
                   ┌────────────────▼────────────────┐
                   │          Control Plane           │
                   │ Run Coordinator / Task Manager  │
                   │ 规划、租约、状态转移、取消      │
                   └───────────────┬────────────────┘
                                   │
              ┌────────────────────▼────────────────────┐
              │              Message Plane               │
              │ durable task stream / retry / dead-letter│
              └─────────────┬───────────────┬─────────────┘
                            │               │
                ┌───────────▼──────┐  ┌────▼────────────┐
                │ Coordinator      │  │ Agent Workers   │
                │ synthesis / join │  │ research/tools  │
                └───────────┬──────┘  └────┬────────────┘
                            │              │
              ┌─────────────▼──────────────▼─────────────┐
              │                 State Plane                │
              │ session/run/task/event/artifact/lease     │
              │ PostgreSQL + object storage + cache       │
              └──────────────────┬────────────────────────┘
                                 │
                       ┌─────────▼─────────┐
                       │   Event Relay     │
                       │ persist -> fanout │
                       └───────────────────┘
```

### 5.2 组件职责

| 组件 | 职责 | 不负责的内容 |
|---|---|---|
| API/Stream Gateway | 接收请求、校验 session/run、建立 SSE、事件回放、鉴权 | 不执行长时间 Agent |
| Run Coordinator | 创建 run、拆分任务、维护 DAG/依赖、合并结果、驱动终态 | 不直接持有浏览器连接 |
| Task Broker | 持久化投递、重试、延迟、死信 | 不决定业务结果 |
| Agent Worker | 领取租约、执行一个任务、发布事件、幂等提交结果 | 不修改未授权 session |
| Tool Gateway/Adapter | 工具白名单、超时、限流、脱敏、结果规范化 | 不绕过任务权限 |
| State Store | 持久化 session/run/task/event/lease/version | 不承担消息投递 |
| Event Relay | 将持久化事件分发给 SSE、日志和监控 | 不作为唯一事实源 |
| Artifact Store | 保存大结果、文件、报告和模型产物 | 不保存会话状态主记录 |

### 5.3 技术选型建议

推荐第一阶段采用以下组合：

| 能力 | 推荐实现 | 原因 |
|---|---|---|
| 持久化状态 | PostgreSQL 或现有平台提供的关系型数据库 | 事务、唯一约束、乐观版本、行级锁适合 run/task 状态 |
| 任务消息 | Redis Streams、RabbitMQ 或已有企业消息队列 | 支持消费确认、重试和消费组；具体产品由部署环境决定 |
| 租约与短期状态 | Redis 或数据库租约表 | 支持 TTL、原子 compare-and-set 和心跳 |
| 大型产物 | 对象存储或共享文件服务 | 避免把大工具结果塞进消息和 session 行 |
| 事件查询 | PostgreSQL 事件表加索引；规模增大后再单独归档 | 支持 Last-Event-ID 回放和审计 |
| 可观测性 | OpenTelemetry trace、结构化日志、指标 | 统一关联 gateway、coordinator、worker 和工具调用 |

当前本地文件 `SessionStore` 可以保留为开发模式或兼容读取层，但不能继续作为多 worker 生产环境的并发协调依据。

## 6. Agent 角色与任务模型

### 6.1 角色分层

建议将现有 Agent 运行拆成三类角色：

1. **Coordinator Agent**：接收用户请求，决定是否需要拆分，生成任务计划，等待子任务结果并合成最终回答。
2. **Specialist Agent**：执行有边界的单一能力，例如证据检索、工作区只读检查、网页研究、验证或结果整理。
3. **Tool Executor**：不做开放式规划，只负责在权限、超时和预算约束下调用一个工具适配器。

角色由配置和能力注册表定义。代码只依据能力类型、权限和预算做调度，不依据具体实体名称做分支。

### 6.2 任务拆分原则

只有满足以下条件的工作才拆成并行子任务：

- 子任务可以独立读取输入，不依赖另一个子任务的中间推理；
- 子任务有明确的输出 schema；
- 子任务可以设置独立超时和预算；
- 子任务的副作用要么为只读，要么具有明确的幂等键；
- 父任务可以定义合并顺序和冲突策略。

以下场景默认不并行：

- 同一 session 的连续用户消息；
- 依赖前一个工具结果才能决定参数的链式工具调用；
- 需要对同一外部资源进行非幂等写入且没有资源级版本控制；
- 需要保持原生工具 call/result 顺序的同一个对话回合。

### 6.3 任务状态机

```text
QUEUED -> CLAIMED -> RUNNING -> SUCCEEDED
   │         │          │
   │         │          ├──────> RETRY_WAIT -> QUEUED
   │         │          ├──────> CANCEL_REQUESTED -> CANCELLED
   │         │          └──────> FAILED
   │         └──────────────────> LEASE_EXPIRED -> QUEUED/FAILED
   └────────────────────────────> REJECTED
```

状态转换只能由任务所有者或 Coordinator 按条件更新，使用数据库版本号或 compare-and-set 防止旧 worker 覆盖新状态。

## 7. 统一任务、运行和事件契约

### 7.1 任务记录

```json
{
  "task_id": "task-uuid",
  "run_id": "run-uuid",
  "session_id": "session-id",
  "parent_task_id": null,
  "agent_type": "coordinator",
  "capability": "chat",
  "status": "running",
  "attempt": 1,
  "max_attempts": 3,
  "priority": 50,
  "input_ref": "state://run-uuid/input",
  "result_ref": null,
  "depends_on": [],
  "idempotency_key": "run-uuid:task-uuid:1",
  "lease_owner": "worker-id",
  "lease_expires_at": "2026-08-06T00:00:00Z",
  "version": 7,
  "created_at": "2026-08-06T00:00:00Z",
  "updated_at": "2026-08-06T00:00:01Z"
}
```

`input_ref` 和 `result_ref` 优先引用共享状态或对象存储；消息体只携带小型、必要、脱敏的元数据。

### 7.2 消息 envelope

所有任务和事件都使用统一 envelope：

```json
{
  "event_id": "event-uuid",
  "event_type": "task.progress",
  "schema_version": 1,
  "session_id": "session-id",
  "run_id": "run-uuid",
  "task_id": "task-uuid",
  "parent_task_id": "parent-task-uuid",
  "attempt": 1,
  "sequence": 42,
  "causation_id": "previous-event-uuid",
  "correlation_id": "request-uuid",
  "producer_id": "worker-id",
  "occurred_at": "2026-08-06T00:00:01Z",
  "idempotency_key": "run-uuid:task-uuid:progress:42",
  "payload": {}
}
```

必需的归属字段是 `session_id`、`run_id`、`task_id`。不能只依赖 worker 的本地上下文推断归属。

### 7.3 事件类型

| 事件 | 产生者 | 作用 |
|---|---|---|
| `run.created` | Coordinator | 创建一次用户请求运行 |
| `task.submitted` | Coordinator | 投递一个任务 |
| `task.claimed` | Worker | 成功取得租约 |
| `task.started` | Worker | 开始实际执行 |
| `task.progress` | Worker | 发送受控进度或 activity |
| `tool.called` | Worker | 记录工具调用开始 |
| `tool.completed` | Worker | 记录工具结果摘要 |
| `task.result_committed` | Worker | 结果已经幂等写入共享状态 |
| `task.failed` | Worker/Coordinator | 记录失败和是否可重试 |
| `task.cancel_requested` | Gateway/Coordinator | 请求取消 |
| `task.cancelled` | Worker/Coordinator | 任务已停止或确认无法继续 |
| `run.completed` | Coordinator | 已形成最终回答 |
| `run.failed` | Coordinator | 运行以错误终止 |

`activity`、`debug`、`tool_call` 和 `tool_result` 可以继续作为对前端兼容的 SSE 事件名，但内部应统一映射到上述可持久化事件模型。

## 8. 分布式所有权与一致性

### 8.1 Run 注册表

当前 `_ACTIVE_RUNS` 必须替换为共享的 `active_runs` 记录：

```text
唯一键：session_id
字段：run_id、status、owner、lease_until、version、created_at、updated_at
约束：一个 session 同时只能有一个 foreground run
```

创建 run 使用事务或原子条件写入：

```sql
INSERT INTO active_runs(session_id, run_id, status, lease_until, version)
VALUES (:session_id, :run_id, 'running', :lease_until, 1)
ON CONFLICT (session_id) DO NOTHING;
```

如果写入失败，返回现有 `run_id` 和结构化的 `session_run_active` 错误。不能先写 user message，再尝试取得 run。

### 8.2 Worker 租约

Worker 领取任务时取得有限时长租约，执行期间定期 heartbeat：

1. 只有租约 owner 和当前 version 能更新任务；
2. heartbeat 使用 compare-and-set，更新 `lease_until` 和 version；
3. 租约过期后，Coordinator 才能重新投递任务；
4. 旧 worker 即使恢复，也不能提交过期租约的结果；
5. 结果提交必须带 `task_id`、`attempt`、`lease_version` 和 `idempotency_key`。

建议租约时间明显大于单次 heartbeat 间隔，并将工具调用超时纳入租约续期策略。对无法中断的外部调用，过期后的结果只能作为过期结果记录，不能覆盖任务终态。

### 8.3 Session 写入

Session 不再允许多个 worker 直接覆盖整个 JSON。推荐：

- session 元数据、消息、工具 transcript、任务状态和事件分表保存；
- 每个 session 的消息写入带单调 `message_sequence`；
- `append_message` 使用唯一键 `(session_id, run_id, turn_id, role, idempotency_key)`；
- 工具 transcript 使用 `(session_id, run_id, tool_call_id, event_type)` 去重；
- 最终 assistant 消息只允许 Coordinator 在所有 required 任务完成后提交；
- 读模型可异步构建，但 canonical 事件和消息不可被读模型反写。

这样可以继续满足 `preserve-recent-tool-history` 的要求：最近三轮工具 transcript 从 canonical 记录恢复，不能从被压缩的 operational cache 恢复。

## 9. Agent 执行流程

### 9.1 前台请求

```text
1. Gateway 校验请求和 knowledge_policy
2. Coordinator 原子创建 session/run
3. Coordinator 持久化 user message 和 run.created
4. Coordinator 创建主任务并投递 task.submitted
5. Gateway 订阅 run 事件并返回 SSE
6. Coordinator 取得任务租约并运行主 Agent
7. 主 Agent 根据 agentic RAG 规划决定是否创建 Specialist tasks
8. Specialist Worker 执行、发布 activity/tool/result 事件
9. Coordinator 按依赖和 sequence 收集结果
10. Coordinator 生成最终回答并幂等提交 assistant message
11. Coordinator 发布 run.completed
12. Gateway 发送 done 并释放订阅
```

### 9.2 子 Agent 执行

```text
Coordinator
  -> task.submitted(research-task-a)
  -> task.submitted(research-task-b)

Worker A/B
  -> claimed -> started -> progress
  -> tool.called -> tool.completed
  -> result_committed

Coordinator
  -> assessment: answer_ready / try_next_source / evidence_gap
  -> 若需要，创建下一轮任务
  -> join by task_id and dependency
  -> synthesis -> run.completed
```

Agentic RAG 的 `ResearchPlan`、`EvidenceObservation`、`EvidenceAssessment` 和 `ResearchBudget` 应作为任务输入/输出 schema，不能把完整 planner rationale 或原始证据塞进公共事件。

### 9.3 运行中追加指令

当前 `run_append.py` 的进程内队列应迁移为：

- 持久化 `run_commands` 表或 command stream；
- 唯一键 `(run_id, append_id)`；
- command 带 sequence、status、created_at；
- Worker 在安全的 tool/turn 边界消费 command；
- 消费前标记 `injected`，未消费的 command 在 run 终态时标记 `not_applied`；
- command 事件通过同一 event log 推送给前端。

不能把追加指令直接写入 Worker 内存，也不能依赖某一台机器继续存活。

## 10. 事件流与 SSE

### 10.1 持久化优先

事件发送顺序必须是：

```text
worker/coordinator
  -> 事务提交 canonical event
  -> event relay/fanout
  -> SSE gateway
  -> frontend
```

内存 pub/sub 只能作为低延迟 fanout，不能作为唯一事件源。事件写入成功但 fanout 失败时，客户端可以通过重连回放；fanout 成功但客户端断线时，也可以通过 `Last-Event-ID` 补齐。

### 10.2 SSE 归属和重连

每个 SSE 数据帧至少包含：

```json
{
  "session_id": "session-id",
  "run_id": "run-uuid",
  "task_id": "task-uuid",
  "event_id": "event-uuid",
  "sequence": 42,
  "status": "running",
  "payload": {}
}
```

重连流程：

1. 前端发送 `Last-Event-ID` 或最后确认的 sequence；
2. Gateway 从 event store 查询该 run 的后续事件；
3. 回放完成后切换到实时订阅；
4. 如果事件已归档，返回可识别的 `replay_unavailable`，前端再加载 session 快照；
5. Gateway 不重新触发 Agent 执行。

### 10.3 activity/debug 安全边界

`activity` 面向用户，只允许包含：

- 当前阶段；
- 已确认事实的短摘要；
- 受控的判断类别；
- 下一步；
- 进度和耗时。

`debug` 面向开发或受限诊断面板，可以包含阶段、工具名、计数、错误分类和 task id，但不能包含凭据、原始请求头、完整 traceback、原始工具参数、隐藏推理或未脱敏证据。

## 11. 工具、权限和上下文隔离

### 11.1 显式运行上下文

`ContextVar` 只用于 worker 内部调用链，跨服务必须显式携带：

```text
RuntimeContext {
  tenant_id
  user_id
  session_id
  run_id
  task_id
  parent_task_id
  attempt
  trace_id
  permission_scope
  deadline
}
```

任何工具调用都必须从任务 envelope 或受信任的任务状态构造上下文，不从进程环境变量猜测当前 session/run。

### 11.2 工具授权

工具权限由 Agent profile、任务 capability、租户策略和请求范围共同决定：

```text
effective_permission
  = profile allowlist
  ∩ task capability
  ∩ tenant policy
  ∩ request scope
```

Agentic RAG 的自动证据环只允许已注册的只读 adapter。写入、删除、导入、登录、配置修改等能力不得因为 planner 输出任意字符串而被执行。

### 11.3 幂等工具调用

每次工具调用生成稳定 `tool_call_id` 和 `idempotency_key`。工具适配器按能力声明：

- `read_only`：可在相同 key 下安全重试；
- `idempotent_write`：由资源版本或请求 key 去重；
- `non_idempotent`：默认不自动重试，需要人工或 Coordinator 明确确认。

工具结果要先规范化、脱敏和限长，再写入 transcript、事件和合成上下文。

## 12. 上下文压缩与工具历史

### 12.1 分布式上下文压缩

现有按 session 的 `asyncio.Lock` 不能跨 worker。改造后应使用以下任一方式：

- 数据库 advisory lock；
- `context_compaction` 表上的 session 唯一租约；
- 支持 fencing token 的分布式锁。

压缩流程保持 OpenSpec 约束：

1. 从 canonical history 计算边界和 source fingerprint；
2. 同一 session 同时只允许一个压缩任务提交缓存；
3. 摘要缓存提交时检查 fingerprint、边界、模型和 prompt 版本；
4. 压缩失败不覆盖上一次有效缓存；
5. 主 Agent 只能在容量校验成功后启动；
6. 压缩事件带 session/run/task 归属，但不暴露原始历史。

### 12.2 最近工具历史

工具 transcript 需要以 canonical event/message 为事实源：

- 通过 `tool_call_id` 关联调用和结果；
- 用 sequence 保留原始顺序；
- 最近三轮完整恢复 native tool messages；
- 只对 prompt projection 去重，不修改 canonical 存储；
- 旧 worker 的重复提交由唯一键拦截；
- 事件乱序时先入库，再按 sequence 构建读取视图。

## 13. 取消、超时与故障恢复

### 13.1 取消

取消是持久化状态转换，不是单纯的浏览器 abort：

```text
用户点击停止
  -> Gateway 写入 cancel_requested
  -> Coordinator 停止派发新子任务
  -> Broker 发布 cancel command
  -> Worker 在安全边界检查取消标志
  -> Worker 停止可中断工作并提交 cancelled
  -> Coordinator 汇总为 stopped/cancelled
  -> Gateway 推送终态
```

对于无法中断的外部调用，Worker 必须在返回后检查任务租约和取消状态，过期结果不得覆盖已取消任务。

### 13.2 重试策略

按错误类别决定重试：

| 类别 | 默认策略 |
|---|---|
| worker 崩溃、网络瞬断 | 有界重试，递增退避 |
| provider 临时不可用 | 按 source budget 和 deadline 重试 |
| 参数校验失败、权限拒绝 | 不重试，直接失败 |
| context capacity | 不重复执行主 Agent，返回可修复错误 |
| 外部非幂等写入未知结果 | 转人工/补偿流程，不自动重放 |
| schema 无法解析 | 有界修复后仍失败则死信 |

每次重试增加 `attempt`，但保持同一个 `task_id` 和 `run_id`。需要新语义任务时创建新 task，并通过 `parent_task_id` 关联。

### 13.3 Dead Letter 与人工恢复

超过最大尝试次数或违反安全策略的消息进入 dead-letter：

- 保留原始 event envelope 和错误分类；
- 不保存未经脱敏的凭据和完整原始 payload；
- 允许管理员重新投递时生成新的 attempt；
- 重新投递前检查 run 是否仍然有效、session 版本是否匹配。

## 14. 迁移路线图

### 阶段 0：完成现有进程内契约

- 完成 `enable-concurrent-sessions` 的剩余手工验证；
- 完成或明确 `improve-agent-activity-and-exception-analysis` 的 activity/error 契约；
- 增加跨进程行为的接口测试，而不是继续扩大进程内单例。

### 阶段 1：抽象运行时接口

新增与具体存储、消息产品无关的接口：

```text
RunRepository
TaskRepository
EventRepository
LeaseRepository
TaskBroker
ArtifactStore
EventPublisher
```

现有内存 map、`SessionEventHub`、本地 mailbox 可以实现这些接口，作为 deterministic local adapter，便于保持当前测试。

### 阶段 2：共享 run/task 状态

- 将 `_ACTIVE_RUNS` 替换为共享 `RunRepository`；
- 将 session/run 状态、任务状态和版本写入共享数据库；
- 引入 fencing token 和 lease owner；
- 先迁移前台 run，暂不迁移所有后台 subagent。

### 阶段 3：引入持久化任务消息

- 将 `AsyncSubagentManager` 的 Thread 启动改为提交 task；
- 将 mailbox 改为 task message stream；
- Coordinator 维护 parent-child 关系和 join 条件；
- Worker 以一个任务为最小执行单元；
- 保持现有 subagent 类型和 UI 字段兼容。

### 阶段 4：事件持久化与 SSE 回放

- `SessionEventHub` 改为 event repository + relay；
- 所有 SSE 事件增加 event id/sequence；
- 实现 `Last-Event-ID` 回放；
- 实现 run 终态快照和历史加载；
- 断开连接不再触发 Agent 重启。

### 阶段 5：迁移工具、RAG 和上下文任务

- 将 Agentic RAG 的 source adapter 作为 Worker 能力注册；
- 将 context compaction、tool transcript、append command 改为持久化任务/事件；
- 为不同能力配置独立 concurrency、timeout、retry 和 budget；
- 迁移大结果到 artifact store，事件只保存引用和摘要。

### 阶段 6：灰度与切换

建议按以下顺序灰度：

1. 只读、无副作用的证据检索任务；
2. workspace 只读任务；
3. 主 Agent 合成和 SSE 回放；
4. 追加指令、取消和后台 subagent；
5. 需要幂等约束的写入型工具。

每一步都保留 feature flag 和回滚路径。禁止在未具备幂等和审计能力前，将非幂等工具直接放入自动重试队列。

## 15. 建议的模块边界

后续实现可以按以下模块拆分：

```text
backend/distributed/
  models.py          # Run/Task/Event/Lease schema
  repositories.py    # repository protocols
  broker.py          # broker protocol and adapters
  coordinator.py     # run/task orchestration
  worker.py          # claim/heartbeat/execute/commit
  leases.py          # fencing token and lease operations
  event_relay.py     # persist, fanout, replay
  idempotency.py     # dedupe keys and commit guards
  cancellation.py    # cancel command and terminal state
  artifacts.py       # large result references
```

与现有模块的关系：

- `backend/main.py` 只负责 API、SSE 和 gateway 适配；
- `backend/agent.py` 保留 Agent 执行语义，通过 Worker adapter 运行；
- `backend/agentic_research/` 保留规划/评估模型，输出任务 schema；
- `backend/session_store.py` 逐步降级为 legacy import/local adapter；
- `backend/subagents.py` 迁移为任务定义和兼容查询层；
- `backend/session_events.py` 迁移为本地事件 adapter 或被 event relay 替换；
- `backend/runtime_context.py` 保留 worker 内部上下文，不作为跨进程一致性机制。

## 16. 测试和验收标准

### 16.1 一致性

- 两个 Gateway 同时提交同一 session 时只有一个 foreground run 成功；
- 两个 worker 同时尝试领取同一 task 时只有一个获得有效租约；
- 旧租约 worker 的结果不能覆盖新 attempt；
- 同一 `idempotency_key` 重复提交不会产生重复消息、工具 transcript 或最终回答；
- 不同 session 的历史、工具事件、RAG trace 和 activity 不串线。

### 16.2 可恢复性

- worker 在 tool call 前崩溃，任务可重新投递；
- worker 在结果提交后、事件 fanout 前崩溃，事件可从 event store 补发；
- Gateway 断线重连后只回放缺失事件，不重复执行 Agent；
- Coordinator 重启后可以从 task/event 状态恢复 join；
- broker 暂时不可用时，已提交的 run 不会被错误标记为成功。

### 16.3 用户体验

- session A 和 session B 可以并行运行；
- 停止 A 不影响 B；
- 前端能够显示当前 task、子 Agent 数量、阶段和终态；
- `activity` 事件默认可读，`debug` 保持独立；
- 终态回答只出现一次，刷新和重连不会重复追加。

### 16.4 RAG、上下文和安全

- Agentic RAG 的 source budget 在多个 worker 间仍然原子消耗；
- `required`、`auto`、`disabled` 语义在分布式重试后不改变；
- 最近三轮 native tool history 完整恢复，重复事件不污染 canonical transcript；
- context compaction 同一 session 不会并发覆盖缓存；
- 事件、日志和错误分析不包含 chain-of-thought、凭据、原始敏感 payload 或未经脱敏的证据内容；
- 代码和配置不包含具体实体到工具、域名或 Agent 的硬编码映射。

## 17. 主要风险与决策建议

| 风险 | 建议 |
|---|---|
| 引入消息系统后运维复杂度增加 | 先抽象 broker interface，优先复用已有基础设施；本地 adapter 保持测试可运行 |
| 至少一次投递导致重复副作用 | 所有任务、工具和结果提交先定义幂等键；非幂等工具默认不自动重试 |
| 事件表增长过快 | 事件正文限长，大结果引用 artifact，按 run/session/时间归档 |
| 多 Agent 成本和并发失控 | 在 Coordinator、tenant、source、tool 和 model 层分别设置预算 |
| 子任务拆分降低回答质量 | 只有有明确 schema、依赖和合并策略的任务才并行；保留 direct path |
| 分布式锁误用导致脑裂 | 使用租约 + fencing token，不使用永不过期的互斥锁 |
| 从文件迁移丢失历史 | 先导入 canonical transcript，再切换写入；导入后做 checksum 和数量校验 |

推荐的第一版范围是：

```text
共享 run/task 状态
+ 持久化 task stream
+ coordinator/worker 租约
+ 事件存储与 SSE 回放
+ 只读 Agentic RAG worker
+ session/run 幂等和取消
```

先完成上述闭环，再迁移写入型工具和更复杂的跨 Agent 协作。这样可以验证分布式运行时的核心一致性，而不会把所有工具风险同时引入。

## 18. 后续 OpenSpec 建议

建议新增一个独立变更，例如 `distribute-agent-runtime`，至少拆分以下 capability：

1. `distributed-run-coordination`：跨 worker 的 run 注册、租约、状态机和同 session 防重入；
2. `durable-agent-task-execution`：任务投递、领取、重试、死信、parent-child join；
3. `persistent-agent-event-stream`：事件持久化、sequence、SSE 回放和断线恢复；
4. `distributed-session-consistency`：消息、工具 transcript、context compaction 和 append command 的并发写入；
5. `agent-worker-permission-boundary`：能力注册、工具授权、预算、脱敏和幂等；
6. `distributed-agent-observability`：activity/debug/error 的跨服务 trace 和安全输出。

每个 capability 都应包含正常流程、重复投递、租约过期、worker 崩溃、取消、事件重放和安全降级场景，而不只验证“任务能运行”。

