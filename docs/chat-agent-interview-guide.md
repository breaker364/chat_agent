# Chat Agent 项目面试准备指南

这份文档从面试官视角整理本项目最可能被问到的内容。重点不是背模型名称，而是能够解释：一次请求如何被规划、执行、流式返回、持久化和恢复；系统在并发、长上下文、工具副作用、检索质量和安全边界下是否可靠。

## 0. 先统一口径

本文只把当前源码能够证明的内容写成“已实现”。README、设计文档中提到但源码没有闭环的内容，会标成“设计/文档描述”；存在明显边界的地方，会标成“待生产化”。面试时建议主动讲边界，这比把原型包装成分布式生产系统更加可信。

项目的核心链路可以概括为：

```text
React/Vite
   │  POST /chat/stream（fetch + ReadableStream 解析 SSE）
   ▼
FastAPI
   ├─ SessionStore、运行中会话互斥、run/session attribution
   ├─ 可选 Agentic Research：plan → collect → assess → refine/next source
   └─ LangGraph ReAct：LLM ↔ tool call ↔ tool result，直到最终回答
         ├─ 文件、网页、视觉、知识库、任务计划等工具
         ├─ Skill：catalog → detail → runner 或 LLM 执行
         └─ Subagent：工具白名单/黑名单、后台线程、mailbox、状态文件
   ▼
SSE 事件 + SessionStore JSON/JSONL + 产物和审计记录

知识库支路：文件导入 → 两阶段同步 → 结构优先分块 → BM25 + dense → RRF → reranker → citation
```

## 1. 简历怎么写更稳妥

### 推荐的一句话项目描述

> 基于 FastAPI、LangGraph 和 React/Vite 构建可扩展 Chat Agent，支持 ReAct 多轮工具调用、SSE 流式交互、Agentic Research 证据协调、混合 RAG（BM25 + dense + RRF + reranker）、多模态分析、Skill/子 Agent 扩展、会话持久化和工具审计去重。

### 推荐写成三条工作内容

1. 设计 FastAPI 到 LangGraph ReAct 的流式执行链路，统一 `session_id/run_id`，把模型文本、工具调用、工具结果、研究摘要、调试和终止状态转换成前端可消费的 SSE 事件。
2. 实现有边界的 Agentic Research 协调器：让 planner 输出结构化研究计划，按来源调用预算和 deadline 采集证据，经 assessor 判断“回答、同源细化、下一来源、冲突或证据不足”，再把有限证据注入最终回答上下文。
3. 实现知识库和运行时可靠性能力：结构优先分块、BM25 与向量召回融合、RRF/rerank、引用返回、工具参数规范化去重、只读 TTL 缓存、上下文压缩、运行中追加指令和子 Agent 工具权限控制。

如果没有真实压测或离线评测数据，不要在简历上写“准确率提升 X%”“支撑百万级并发”“ exactly-once”“分布式多 Agent”。这些指标应在补齐实验后再写。

### 60 秒项目介绍模板

> 这是一个面向复杂任务的 Chat Agent。请求先进入 FastAPI，按 session 做前台运行互斥并建立 run ID；如果问题需要事实证据，会先经过一个有来源、调用次数和 deadline 预算的研究协调器，证据不足时不会让主 Agent绕过预算自行搜索。随后由 LangGraph ReAct 负责模型与工具的循环调用，工具事件和回答通过 SSE 流给前端，同时写入会话历史、任务进度和审计信息。知识库采用结构优先分块，结合 BM25 和向量召回，用 RRF 融合后再 rerank，并返回引用。我的重点是把“模型能回答”做成“可追踪、可恢复、可控制”，当前仍是单进程文件存储，生产化还需要外部状态、认证、沙箱和完整评测。

## 2. 面试官最可能问的 18 个问题

下面每题给出“先答什么”和“容易追问什么”。回答顺序建议是：结论 → 代码证据 → 取舍 → 当前限制 → 下一步改进。

### 2.1 为什么选 LangGraph ReAct，而不是普通 Chain？

**建议回答：** Chain 适合固定的线性步骤；本项目的任务需要根据工具结果决定下一步，可能多次调用不同工具、处理失败并继续，因此用 ReAct 图表达 `model → tool → model` 循环。`build_agent()` 使用 `create_react_agent`，`stream_agent_events()` 通过 `astream_events` 把内部事件转换成应用事件，并设置 `recursion_limit` 防止无限循环。

**背后知识点：**

- ReAct 是 Reasoning 与 Acting 交替，不等于把完整思维链暴露给用户。
- 图执行器需要状态、终止条件和最大步数；最大步数是安全阀，不是请求超时。
- 工具结果必须被视为不可信输入，不能自动获得更高权限。

**深挖：** 如果工具连续报错怎么办？当前代码有有限 repair pass，会把错误上下文交回模型；仍需要将单次 LLM/工具 deadline、取消和最大 token 预算补到运行时。

### 2.2 `recursion_limit=180` 能保证任务不会卡死吗？

**建议回答：** 不能。它限制图的步骤数，不能限制一次 HTTP 请求的墙钟时间，也不能中断已经发出的模型或子进程调用。要防止真正的资源耗尽，需要同时有每步 timeout、总 deadline、并发信号量、取消传播、输出/token 上限和熔断。

**深挖：** 180 是否合理？应通过 trace 统计真实步数分布，按 p99 加安全余量，而不是拍脑袋；不同工具应有不同预算。

### 2.3 SSE 事件是怎么设计的？断线后会不会重复或丢失？

**建议回答：** 后端用 `EventSourceResponse`，事件至少包括 `research`、`activity`、`text`、`tool_call`、`tool_result`、`debug`、`error`、`done`。每个可归属事件附带 `session_id/run_id`；前端用 `fetch` 读取 `ReadableStream`，按空行切分 SSE frame，再由 reducer 更新运行状态。前端有 abort 和完成后重新读取 session 的流程，但当前不是完整的可重放协议，不能承诺断线自动续传不重不漏。

**背后知识点：** SSE 是服务端到客户端的单向文本流；浏览器网络重试可能造成重复消费，因此客户端 reducer 和服务端副作用都需要幂等。生产化应增加递增事件序号、`Last-Event-ID`、事件日志或消息队列、重放窗口，以及明确“至少一次/至多一次/尽力而为”的语义。

### 2.4 为什么同一个会话不能同时发两条前台任务？不同会话能并发吗？

**建议回答：** `backend/main.py` 用 `_ACTIVE_RUNS` 按 `session_id` 保存活动 run，用进程内 `RLock` 保证检查和写入原子化；同会话已有运行时，同步接口返回 409，流式接口发出 `session_run_active` 错误事件，不同会话可以并发。这样可以避免同一会话的历史、工具事件和追加指令交叉写入。

**当前边界：** `_ACTIVE_RUNS` 和锁只在一个 Python 进程内有效。Gunicorn 多 worker、多个容器或重启后会出现重复运行、锁失效和状态不一致。生产化需要 Redis lease/分布式锁、数据库唯一约束或队列，并处理 lease 过期和 owner fencing。

### 2.5 “运行中追加指令”是如何做到的？能中断正在生成的 token 吗？

**建议回答：** 追加队列以 `(session_id, run_id)` 隔离，命令带 sequence 和状态。`AppendAwareChatModel` 在下一次模型调用前消费队列，把内容作为额外消息注入；运行已结束时标记 `not_applied`，run ID 过期则拒绝。它是模型调用边界上的协作式注入，不会强行打断正在进行的网络请求或 token 流。

**背后知识点：** 这是 cooperative cancellation/coordination，而不是 preemptive interruption。强制中断需要供应商支持取消、任务可恢复状态和幂等清理；否则容易留下半写入文件或未完成外部副作用。

### 2.6 上下文压缩如何避免把工具历史压坏？

**建议回答：** 先根据估算 token 数和模型窗口的剩余容量判断是否触发；`partition_history()` 按用户 turn 切分，并把相关原生工具事件成组保留。压缩摘要要求固定章节（事实结论、文件产物、未完成项、工具证据），额外保存 exact literal ledger，保证 URL、路径、版本和编号不被改写。缓存同时校验 schema、prompt 版本、模型名、历史 fingerprint、覆盖消息边界和 turn 数；每个 session 有异步锁避免并发生成两个摘要。

**当前限制：** 默认 token 估算是通用近似，中文和不同 tokenizer 会有偏差；压缩锁字典长期不清理；如果受保护的工具历史本身超出预算，系统只能报容量错误。生产化应使用目标模型 tokenizer、限制摘要并发、清理锁、持久化 compaction 版本，并设置可观测的压缩前后 token 与事实保真指标。

**这里的“丢弃”要精确定义：** 被移除的是旧 turn 的原始 `assistant_tool_calls/tool` 消息，不是所有历史事实。旧消息会先作为带工具事件的完整单元交给压缩器，最终在模型上下文中由一个 `context_summary` 替代；最近保留的 turn 仍按原生工具协议重放。因此这是“原始记录丢弃、摘要事实保留”的有损压缩，而不是同时把同一批原始 tool use 继续完整保留。`exact literal ledger` 也不是所有数值和错误码的白名单：代码只抽取 URL、日期、版本、路径、文件名、特定代码/标识符和数字等模式；不匹配模式的普通数字或错误码仍可能只依赖摘要语义，不能宣称百分之百保真。测试 `test_successful_compaction_sends_summary_and_recent_turns_only` 也明确验证了旧回答不再进入主 Agent，而最近请求仍然存在。

### 2.7 工具去重和幂等是怎么做的？能保证 exactly-once 吗？

**建议回答：** 工具参数先做归一化（路径转相对 workspace 表示、内容转 hash、默认值归一化），再以 canonical JSON 生成 key。当前 run 内缓存重复调用；只读工具可按 TTL 跨轮复用；有副作用的工具不重复执行，而是返回阻断信息。审计记录包含参数 hash、是否复用、来源和耗时。

**必须主动说明：** 这是应用层 best effort，不是 exactly-once。进程崩溃、网络重试、多进程缓存不共享、外部系统已成功但响应丢失时，都可能重复或不确定。真正的幂等需要业务幂等键、持久化状态机、唯一约束、outbox/inbox 或 provider 的幂等 API。

### 2.8 RAG 的完整链路是什么？为什么要两阶段导入/同步？

**建议回答：** 导入阶段只接收和登记文件，状态为 pending；同步阶段再解析、分块、生成 embedding、写入向量后端、构建 BM25 和准备 reranker。这样上传请求不会被长时间索引阻塞，也能展示逐文件失败并允许重试。检索时同时产生 lexical 和 dense 候选，RRF 融合，必要时对候选 rerank，最后补相邻 chunk 并返回 citation、阶段分数和候选数量。

**背后知识点：** 索引是离线/异步写路径，查询是在线/低延迟读路径；文档 manifest、content hash 和 retrieval signature 用来判断增量更新和旧索引是否失效。

### 2.9 BM25、dense、RRF、reranker 分别解决什么问题？

**建议回答：** BM25 擅长精确词、编号、罕见术语；dense embedding 擅长同义表达和语义相近；两者分数尺度不同，不能直接相加，所以用 Reciprocal Rank Fusion：

```text
RRF(d) = Σ 1 / (k + rank_i(d))
```

`k` 让高位和低位差异平滑，缺少某一路的文档只贡献其他路的分数。融合后的前若干候选再交给 cross-encoder/reranker，利用 query-document 交互提升精排质量，但会增加延迟和显存开销。

**深挖：** 如何选择 candidate depth、top-k 和 `k`？用离线 qrels 做 ablation，分别比较 lexical、dense、hybrid、hybrid+rerank 的 Recall@K、MRR、nDCG 和延迟；当前评测代码的 comparison 仍复用了同一 lexical ranking，不能把输出当成真实对比结果。

### 2.10 这个项目的“semantic chunking”是真正的语义分块吗？

**建议回答：** 当前主要是结构优先和启发式句子/窗口切分：识别标题、段落、代码块和表格，在同一 heading 下按 token 预算合并，超长块按句子或窗口切分。它保留结构和可引用的 offset，但没有根据 embedding 相似度寻找语义断点；配置里的 semantic threshold 不能直接等价为已完成的 embedding semantic chunking。

### 2.11 Agentic Research 与普通 ReAct 搜索有什么区别？

**建议回答：** Research coordinator 把证据路由从主 Agent 工具池中拿出来，先让 planner 生成受 schema、枚举、长度约束的 `ResearchPlan`，再按 personal knowledge/workspace/web 来源采集，交给 assessor 选择 `answer_ready`、`refine_same_source`、`try_next_source`、`report_conflict` 或 `evidence_gap`。来源次数、路由次数和 deadline 都有限，最后只把有引用的 bounded evidence 注入合成上下文，不暴露内部推理。

**安全要点：** 证据被明确标记为不可信参考资料，证据中的指令不能获得工具权限；研究工具从普通 ReAct 池移除，避免主 Agent 绕过研究预算。

**当前限制：** deadline 主要在 provider 调用前检查，单次 provider 已开始后没有强制取消；没有真实 benchmark 证明研究协调器一定提升答案质量。

### 2.12 子 Agent 的权限和生命周期如何控制？

**建议回答：** 内置类型包括通用、只读探索、计划和 verification；通过 allowed/disallowed tool names 过滤工具，后台任务用 daemon thread 执行，状态、transcript、mailbox 和通知写入 workspace/session。主 Agent 可以查询任务、发送消息和读取结果。

**当前边界：** 这是进程内线程管理，不是分布式 worker；`SUBAGENT_MAX_RUNTIME_SECONDS` 当前在任务结束后才检查，不能真正中断超时线程；多进程时 manager 的内存状态不共享。生产化应使用任务队列/worker、可取消 future、持久化状态机和租约。

### 2.13 Skill 系统如何发现和执行？安全边界在哪里？

**建议回答：** 先向模型注入轻量 catalog，模型决定需要某技能后再读取 `SKILL.md` 详情；有 `skill_runner.py` 时通过 subprocess 执行，没有 runner 时用 LLM 按 prompt 执行。这样可以减少无关 prompt 和资源加载。

**必须承认的风险：** frontmatter 解析器较简化；runner 没有强制 timeout，继承环境变量和进程权限，也不是安全沙箱；缺少签名、权限声明、版本兼容检查。生产化需要 manifest schema、签名/来源信任、最小权限、独立容器或 WASI 沙箱、CPU/内存/网络限制和审计。

### 2.14 如何防止 Agent 通过工具越权读写文件或执行代码？

**建议回答：** 一般文件工具有 workspace root 校验，路径会 resolve 后检查是否位于允许根目录；子 Agent 默认禁用写文件、删除和部分高风险工具；工具调用参数会被清理和审计。

**当前高风险点：** `run_python_file` 的文档和实现允许绝对脚本路径，且 `working_directory` 也可指向 workspace 外；这不是完整沙箱。URL 访问虽然有 public URL 检查，但仍需防 DNS rebinding、IPv6/私网地址、重定向和代理绕过。CORS 当前为 `allow_origins=["*"]` 且允许 credentials，无认证和租户隔离也不适合生产。

### 2.15 SessionStore 为什么不是企业级持久化？

**建议回答：** 会话主要保存为 JSON，工具事件、任务计划和部分运行记录使用 JSONL；普通 `save_session()` 直接写目标文件，只有上下文压缩路径使用临时文件替换。进程内 `_SESSION_FILE_LOCK` 只能保护同一进程，无法解决多进程竞争、崩溃恢复、部分写入、索引和查询性能。

**改进：** 使用关系数据库或文档数据库保存会话状态，事件写入 append-only 表/消息流；以版本号或 compare-and-swap 做并发更新，事务提交消息与副作用 outbox，定期快照和重放事件。

### 2.16 前端如何把一个长 SSE 请求拆成可维护状态？

**建议回答：** `App.jsx` 使用 `sessionRunState` 中的纯函数管理每个 session 的 runId、状态、流式文本、tool/debug/activity/usage 和 appendCommands；SSE parser 只负责把 frame 分发到 reducer，文本、工具、研究、错误和 done 分开处理。服务端 run attribution 可以覆盖客户端预生成的 run ID，避免多个会话串流。

**当前限制：** `App.jsx` 超过 3000 行，网络、SSE reducer、知识库面板、登录面板和布局状态耦合在一起。面试时可以把拆分 hooks、API client、SSE reducer、feature components 作为明确的下一步。

### 2.17 出错时如何保证用户能继续？

**建议回答：** 工具错误会被序列化为受控结果并进入有限 repair pass；每个 run 有 error/debug/done 事件和持久化执行摘要。完成门会读取已持久化的 primary result、已完成/未完成任务，在模型给出不完整文本时尝试生成可继续的响应。

**深挖：** 不能把“重试”当成万能方案。读操作可重试，写操作必须带幂等键；模型 repair 也要限制次数、token 和总 deadline，并把原始异常与给用户的安全摘要分开。

### 2.18 现在如何证明系统质量？

**建议回答：** 源码已有后端单测、前端 Vitest、RAG 检索指标函数和报告输出结构；运行时还有工具审计、token usage、延迟、候选数量和研究预算等 trace 字段。但当前评测仍有明显占位：pipeline comparison 复用了同一 lexical ranking，RAGAS 分支在没有 judge 时返回 blocked，不能对外宣称完成真实离线对比。

面试时应给出自己的评测计划，而不是编造数字：固定数据集和版本、固定切分/embedding 配置、建立 lexical/dense/hybrid/rerank baseline，报告 Recall@K、MRR、nDCG、citation support、答案 EM/F1、p50/p95 延迟、token 成本和失败率。

## 3. 五条连续深挖路径

### 路径 A：从“追加指令”追到一致性

1. 追加命令如何定位到正确任务？答：`session_id + run_id`。
2. 如果客户端拿到旧 run ID？答：服务端校验 active run，stale run 拒绝。
3. 如果命令到达时模型正在生成？答：只能在下一次模型调用前注入。
4. 如果任务先结束？答：queued 命令落为 `not_applied` 并持久化事件。
5. 多 worker 怎么办？答：当前内存队列不够，需要 Redis/DB 队列和 owner lease。
6. 怎么防止重复注入？答：append_id/sequence 作为幂等键，持久化状态迁移只能单向进行。

### 路径 B：从“RAG 准确”追到可验证实验

1. 结构为什么影响召回？答：标题路径、代码和表格边界提供语义上下文和 citation offset。
2. BM25 与 dense 为什么互补？答：词法精确性与语义泛化能力不同。
3. 为什么 RRF？答：两路分数不可直接校准，按 rank 融合更稳健。
4. rerank 放在哪里？答：只对融合后的有限候选做交互式精排，控制成本。
5. 相邻 chunk 会不会引入噪声？答：需要按窗口和去重策略做 ablation，记录 citation 支持率。
6. 如何证明提升？答：同一 qrels、同一切分、同一 top-k，对 lexical/dense/hybrid/rerank 做独立 pipeline，报告质量和延迟。

### 路径 C：从“同会话互斥”追到生产部署

1. 当前锁在哪里？答：`_ACTIVE_RUNS` + `RLock`，只在进程内。
2. 两个 worker 会怎样？答：各自认为没有活动任务，可能双跑并交叉写历史。
3. Redis lock 就够了吗？答：还要 lease、续租、owner token/fencing 和过期后的安全恢复。
4. 状态放哪里？答：数据库保存 session/run 状态，队列保存事件，对象存储保存大产物。
5. 如何保证一次运行可恢复？答：事件日志 + checkpoint + 幂等工具状态机，重启后从已提交节点继续。

### 路径 D：从“能执行 Python”追到安全模型

1. prompt injection 能否让模型执行任意脚本？答：模型不应拥有无条件工具权限，必须由 policy/tool gateway 再检查。
2. workspace root 校验解决什么？答：解决普通读写路径穿越，不等于 OS 级隔离。
3. 当前 `run_python_file` 的问题？答：绝对路径和工作目录可越出 workspace，进程继承环境和权限。
4. 怎么改？答：脚本先登记并 hash，沙箱容器执行，禁网或 egress allowlist，限制 CPU/内存/文件系统，短超时并杀进程树。
5. 网页抓取有什么风险？答：SSRF、重定向、DNS rebinding、私网 IPv4/IPv6、超大响应和恶意压缩包。

### 路径 E：从“上下文太长”追到事实保真

1. 什么时候压缩？答：窗口剩余 token 低于阈值。
2. 压哪些内容？答：早期完整 turn；最近 turn 和原生工具事件受保护。
3. 摘要会不会改写路径和版本？答：exact literal ledger 保留格式敏感文本，并以 fingerprint 校验来源。
4. 新消息追加后缓存还能用吗？答：根据覆盖边界和 allow-extended 规则判断，历史 fingerprint 变化则失效。
5. 估算误差怎么办？答：目标模型 tokenizer、硬上限和容量失败事件；不要只依赖字符数除以四。

## 4. 分类问题库（适合面试前自测）

### Agent/LLM

- ReAct 与 function calling 的边界是什么？
- 工具 schema 为什么要用 Pydantic？如何处理模型返回的 malformed arguments？
- 如何防止模型把工具结果中的指令当成系统指令？
- 如何设置 temperature、max tokens、stop condition 和 repair pass？
- 如何在不泄露思维链的情况下展示可解释进度？
- 模型供应商切换时，哪些接口必须抽象？

### 后端/并发/分布式

- FastAPI async endpoint 中哪些调用必须 `to_thread` 或异步化？
- `RLock`、`asyncio.Lock`、分布式锁各自保护什么？
- SSE 连接断开时如何释放 run、子进程和工具 lease？
- 如何设计 run 状态机：queued/running/cancelling/succeeded/failed/expired？
- 如何实现多租户限流、优先级和 backpressure？
- JSON 文件升级到数据库时如何做迁移和兼容？

### RAG/搜索

- chunk size、overlap、candidate depth 如何联合调参？
- BM25 的 `k1/b` 分别影响什么？
- 余弦相似度、点积和归一化 embedding 有什么关系？
- reranker 为什么不能对全库执行？
- 如何处理文档更新、删除、重复导入和旧向量残留？
- 如何做多语言、表格、代码和超长文档的切分？

### 前端/交互

- 为什么用 `fetch` 读取 SSE，而不是原生 `EventSource`？
- 如何避免多个 session 的事件串线？
- 前端如何处理半个 frame、网络断开和 done 丢失？
- append 模式与新 run 模式的状态转换是什么？
- 大量 tool/debug 事件如何虚拟化或限长？

### 安全/评测/运维

- 工具权限是按用户、session、run 还是 skill 绑定？
- 如何防 SSRF、任意文件读取、命令执行和敏感环境变量泄露？
- 哪些日志可以留，哪些 prompt/文件内容必须脱敏？
- 如何定义“任务成功”而不只看 HTTP 200？
- 如何做离线回归、线上 shadow、灰度和 baseline promotion？
- 发生 provider 超时、模型限流、Qdrant 不可用时，用户看到什么？

## 5. 知识点速查

### 5.1 ReAct 执行循环

```text
state = messages + runtime metadata
while not terminal:
    model_output = LLM(state)
    if final text: terminal
    if tool calls:
        validate arguments
        execute tool under policy/timeout
        append tool result + audit event
    if step budget exhausted: controlled failure
```

关键是把模型决策和工具执行分开。模型只能提出意图，工具层负责 schema 校验、权限、超时、幂等和审计。

### 5.2 SSE 的可靠性

SSE frame 通常是 `event: <type>\ndata: <payload>\n\n`。网络层可能切在任意字节位置，所以 parser 必须有 buffer；JSON payload 需要容错。若要可恢复，应给每个事件序号和 `id`，服务端保留事件窗口，客户端用 `Last-Event-ID` 请求补发；若副作用不能重复，则服务端必须用幂等键。

### 5.3 并发控制

- 进程内互斥：线程锁/async lock，只对当前进程有效。
- 分布式锁：要处理 TTL、续租、锁丢失和旧 owner 继续写入，常用 fencing token。
- 数据库并发：乐观锁用版本号，悲观锁用行锁；状态迁移必须是合法的单向转换。
- 队列背压：限制每租户/每 session 的 in-flight 数量，避免模型和工具把系统内存打满。

### 5.4 RAG 指标

- Recall@K：前 K 个结果覆盖相关文档的比例。
- MRR：第一个相关结果排名的倒数平均值。
- nDCG@K：考虑排名位置和相关等级的折损累计收益。
- MAP：多个相关文档下的平均精确率。
- citation support rate：回答中的引用是否真的支持对应 claim。
- EM/F1：答案与 gold 的严格匹配或 token 重叠，不能代替事实 groundedness。

### 5.5 Token 与上下文

模型上下文不是“字符数上限”。它包含 system prompt、历史、工具 schema、工具结果和当前输入。压缩要区分可摘要事实与不可变 literal；缓存必须绑定 prompt/model/schema 版本，否则会把旧摘要误用于新上下文。最终还要保留硬容量失败路径，因为摘要本身也可能超限。

### 5.6 幂等、重试和副作用

重试适用于超时/网络错误，但“请求失败”不等于“服务端未执行”。对写操作使用 `idempotency_key`，服务端保存 `started/succeeded/failed/unknown` 状态；对外部系统采用 outbox/inbox 或 provider 幂等 API。不要把内存缓存命中描述成 exactly-once。

### 5.7 SSRF 与代码执行

URL 校验至少需要限制 scheme、解析后的 IP、IPv4/IPv6 私网与 loopback、DNS 解析结果、重定向链和响应大小；检查发生在每次跳转前后。代码执行若不是可信用户专用功能，就应采用独立沙箱、最小权限、无默认网络、资源配额和进程树清理。workspace 路径检查只是应用层防穿越。

## 6. 当前实现的压力面与改进优先级

| 优先级 | 当前事实 | 面试时的诚实表述 | 推荐改进 |
|---|---|---|---|
| P0 | 无完整认证、租户隔离；CORS 为通配来源且允许 credentials | “demo/内网原型，尚未完成生产安全边界” | OIDC/JWT、租户与资源 ACL、严格 CORS、审计和密钥托管 |
| P0 | Python 执行工具允许绝对路径和 workspace 外工作目录 | “有普通路径校验，但不是沙箱” | 容器/WASI、无网或 egress allowlist、资源配额、脚本登记和 hash |
| P0 | URL 校验仍需覆盖 DNS rebinding、IPv6/私网和重定向 | “有基础 public URL 校验，SSRF 防护未闭环” | 解析后 IP 校验、每跳重校验、响应/压缩包上限 |
| P1 | active run、append queue、tool cache、subagent manager 都是进程内状态 | “支持单进程同会话互斥，不是分布式多 Agent” | Redis/DB 状态、队列 worker、lease/fencing、持久化事件 |
| P1 | 普通 SessionStore 直接写 JSON；文件锁只在进程内 | “文件型会话存储适合原型” | DB 事务、原子写、版本号、事件表、快照/重放 |
| P1 | 子 Agent timeout 在结束后才检查 | “有运行预算字段，但没有真正的硬中断” | 可取消 worker、进程级 timeout、资源配额和任务租约 |
| P1 | Research deadline 主要在调用前检查 | “有预算控制，但 provider 调用中的取消还不完整” | provider 超时、取消 token、熔断、逐来源延迟预算 |
| P1 | 评测 pipeline comparison 复用同一路 lexical ranking，RAGAS 是占位/blocked | “有评测骨架，不对外宣称完成真实对比” | 独立 pipeline、固定数据集、judge、回归门禁和 baseline |
| P2 | `App.jsx` 超过 3000 行 | “功能已集中实现，但前端单体组件需要拆分” | hooks、SSE reducer、API client、面板组件和虚拟列表 |
| P2 | Python 依赖主要是 `>=`，缺少 lockfile | “环境可安装，但可复现性不足” | uv/Poetry/pip-tools lock、镜像构建和 SBOM |
| P2 | system prompt 规划规则存在重复/语义冲突风险 | “提示词有策略，但需要版本化和冲突测试” | 单一规范、schema-first、prompt version、golden tests |
| P2 | 代码中仍可发现特定实体/域名分支 | “需要按 provider adapter 和配置驱动重构” | 泛化实体提取、可插拔 provider、通用 URL 结构评分；不要新增实体硬编码 |

## 7. 面试时不应过度声称的内容

- 不要说“分布式多 Agent”：当前子 Agent 是进程内 daemon thread，状态虽写文件但没有分布式调度。
- 不要说“exactly-once 工具执行”：当前是 canonical key、缓存和副作用阻断的应用层 best effort。
- 不要说“企业级持久化”：普通会话路径是 JSON 文件，缺少数据库事务和多实例协调。
- 不要说“完整沙箱”：workspace root 校验不能替代 OS/容器隔离，Python 执行边界仍需收紧。
- 不要说“RAGAS/benchmark 已完成”：评测代码中有占位和同一 ranking 复用，必须先补真实实验。
- 不要把 README 中的“支持”自动等价为“生产可用”；回答时区分功能存在、测试覆盖和生产保证。

## 8. 面试前建议补齐的指标和材料

至少准备一张实验表，包含配置、数据集版本、样本数、硬件和失败样本链接：

1. Agent：任务完成率、工具调用成功率、repair rate、平均/最大步数、p50/p95 延迟、输入/输出 token 和单请求成本。
2. 并发：不同 session 的吞吐、同 session 冲突率、SSE 断线率、取消后的资源释放时间。
3. RAG：Recall@5/10、MRR、nDCG@10、citation support、答案 EM/F1、rerank 增量延迟和索引耗时。
4. Research：各 policy 的来源调用次数、evidence gap/conflict 比例、provider 超时率和证据支持率。
5. 可靠性：工具重复调用率、缓存命中率、写操作不确定状态、session 恢复成功率和错误分类。
6. 安全：路径穿越、SSRF、prompt injection、敏感信息泄露和越权工具调用的负向测试集。

### 当前验证快照

本次在当前工作区执行的结果也应如实保留：后端 pytest 为 169 passed、11 failed、1 skipped（失败主要集中在临时目录权限和 FAISS 加速相关用例）；前端 Vitest 为 5 个测试文件、9 passed、15 failed，失败集中在测试环境未提供 `React` 全局。它们不能被表述为“全量测试通过”，应先修复测试环境/实现后再将结果写入简历。

推荐准备两段现场演示：

- 一次需要知识库引用的请求，展示 `research → tool_call → citation → done` 事件和最终会话记录。
- 一次长任务中追加指令并让任务失败/超时，展示 `queued/injected/not_applied`、run attribution 和恢复路径。

## 9. 源码证据索引

以下链接用于面试前逐段复习。行号是当前工作区快照附近的位置，若代码继续变更，以函数名为准。

- [backend/main.py#L55](../backend/main.py#L55)：FastAPI、CORS、`_ACTIVE_RUNS`、`/chat/stream`、`/chat`、SSE 事件归属与 session API。
- [backend/agent.py#L135](../backend/agent.py#L135)：`build_agent`、`create_react_agent`、`AppendAwareChatModel`、`stream_agent_events`、`recursion_limit`、repair pass、上下文容量与压缩接入。
- [backend/run_append.py#L25](../backend/run_append.py#L25)：按 `(session_id, run_id)` 隔离的追加命令队列、sequence 和状态迁移。
- [backend/context_compaction.py#L54](../backend/context_compaction.py#L54)：历史分区、触发阈值、fingerprint、exact literal ledger、摘要缓存校验和锁。
- [backend/tools.py#L163](../backend/tools.py#L163)：工具 canonical key、参数 hash、TTL/副作用策略、workspace 路径、`run_python_file`、URL 校验和工具审计。
- [backend/rag/chunking.py#L41](../backend/rag/chunking.py#L41)：标题/段落/代码/表格解析、超长块切分和 chunk signature。
- [backend/rag/service.py#L1558](../backend/rag/service.py#L1558)：导入同步、BM25/dense 候选、RRF、rerank、相邻 chunk、citation 和 retrieval metadata。
- [backend/rag/config.py#L41](../backend/rag/config.py#L41)：chunk、hybrid、RRF、embedding、vector backend、reranker 和 retrieval signature 配置。
- [backend/rag/evaluation.py#L106](../backend/rag/evaluation.py#L106)：Recall/MRR/nDCG/MAP、答案指标、RAGAS 占位和 pipeline comparison。
- [backend/agentic_research/models.py#L90](../backend/agentic_research/models.py#L90)：来源枚举、ResearchPlan 校验、ResearchBudget、trace 和 assessment schema。
- [backend/agentic_research/orchestrator.py#L194](../backend/agentic_research/orchestrator.py#L194)：plan → collect → assess → refine/next source 的主循环。
- [backend/agentic_research/runtime.py#L56](../backend/agentic_research/runtime.py#L56)：只读证据工具绑定、从 ReAct 工具池移除 evidence tools、bounded synthesis context。
- [backend/subagents.py#L45](../backend/subagents.py#L45)：子 Agent 类型、工具过滤、后台线程、状态/transcript、mailbox 和运行预算。
- [backend/skills.py#L89](../backend/skills.py#L89)：`SKILL.md`/JSON 发现、catalog/detail、runner subprocess 或 LLM 执行。
- [backend/session_store.py#L551](../backend/session_store.py#L551)：JSON 会话、JSONL 事件/任务、普通保存和仅压缩路径的原子替换。
- [backend/tests/test_context_compaction.py#L197](../backend/tests/test_context_compaction.py#L197)：验证旧 turn、工具事件分组、`ERR-42` 等字面量抽取，以及压缩后只向主 Agent 发送摘要和最近 turn。
- [frontend/src/App.jsx#L2684](../frontend/src/App.jsx#L2684)：请求、SSE parser 分发、append 交互、知识库面板和当前单体组件边界。
- [frontend/src/sessionRunState.js#L1](../frontend/src/sessionRunState.js#L1)：按 session 管理 run 状态、事件、追加命令和服务端 run attribution。

## 10. 最后用三句话自检

1. 我能否不看代码，画出一次请求从 HTTP 到模型、工具、SSE、持久化的时序？
2. 面试官问“多 worker、断线、重复执行、超时、越权”时，我能否先说当前实现，再说缺口和改进？
3. 我是否准备了真实指标；如果没有，是否明确说“目前没有测量，下一步这样测”，而不是猜一个数字？
