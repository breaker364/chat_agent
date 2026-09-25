## 为什么

Agent 运行循环里存在大量"小决策",当前由两类机制承担,各有明确短板:

1. **完整 LLM 调用**:工作流路由(`workflow_runtime.ModelRouteAdapter`)、研究证据评估(`agentic_research/orchestrator.ModelEvidenceAssessor`)、记忆抽取(`memory.StructuredMemoryExtractor`)每次都是一次完整聊天模型调用,慢(秒级)、贵,而这些判断本身只是分类或是非题。
2. **关键词/阈值规则**:回答截断检测(`agent.looks_incomplete`)、错误分类(`error_taxonomy`)、变更冲突检测(`mutation_guard.append_conflicts_with_workflow`)、压缩触发(`context_compaction.should_compact_context`)全部依赖关键词表或纯算术阈值,对未列出的表述失效,且中英文混排需要维护两套词表。

TypeSafe AI 的 Jev(System One 决策模型,2026-09 发布)专为此类场景设计:输入状态文本与一组类型化问题,毫秒级返回带校准置信度的结构化答案,不生成自由文本,成本约为聊天模型的数百分之一。本变更在约定范围内(路由与 Skill 预选、RAG 召回门控、上下文压缩时机、持久化记忆写入/读取)以"级联 + 可降级"方式接入 Jev,所有现有路径保留为快路径与兜底。

## 变更内容

- 新增 Jev 决策网关 `backend/jev_client.py` 与 `load_jev_config()` 配置:支持 live/mock/off 三种模式,固定模型版本,独立于聊天模型连接,每个接入点有独立功能开关,默认全部关闭。
- **路由**:工作流路由与研究证据评估改为"Jev 优先、低置信升级 LLM"——Jev 一次请求并行回答任务类型(选择题)、是否需要查资料(是非题)、风险等级(选择题)、证据是否充分(五选一);置信度达标直接采纳,不达标或调用失败时回退现有 LLM 调用/规则评估器。
- **Skill 预选**:用户消息进入时由 Jev 在技能目录上做一次选择题预选,高置信命中时向系统提示注入"建议技能"提示(引导而非强制,主模型仍通过现有工具自选);低置信不注入,行为照旧。
- **RAG 召回门控**:在重排之后、邻块扩展之前,对前 N 个候选分块并行做语义判断——相关性(低则剔除)、与查询前提矛盾(标记为冲突证据单独注入)、隐藏指令注入(剔除);Jev 不可用时整层跳过,行为与现状完全一致;结果按(查询, 分块)缓存。
- **上下文压缩时机**:保留现有 token 阈值硬底线,新增"灰区"判断——在阈值临近区间内由 Jev 判断近期历史是否仍承载未完成任务状态,无则提前压缩,有则等待硬阈值;判断失败保持现有触发时机。
- **持久化记忆**:写入侧在记忆抽取调度后、LLM 抽取前加一道 Jev 预筛(本轮是否含值得长期记住的信息),低概率跳过抽取以省一次 LLM 调用;读取侧在记忆条数超过上限时,按当前用户消息对条目做相关性过滤,只注入相关条目。
- 所有判断点记录决策日志(问题名、概率、置信度、模型版本、耗时、决策来源),用于后续阈值校准;不记录消息原文全文。

## 能力

### 新增能力

- `jev-decision-gateway`:Jev 客户端、模式与配置、级联降级保证、决策日志与可测试性(mock/stub)。
- `jev-routing-decisions`:工作流路由、研究证据评估的 Jev 优先级联判断,以及技能预选建议注入。
- `jev-rag-passage-gating`:知识库检索候选在重排后的相关性、前提矛盾与注入语义门控。
- `jev-memory-compaction-gates`:记忆写入预筛、记忆读取过滤与上下文压缩灰区时机的判断约束。

### 已修改能力

无。

## 影响

- 后端新增:`backend/jev_client.py`(客户端、问题构造、降级、日志),`backend/config.py` 新增 `load_jev_config()`。
- 后端修改:`workflow_runtime.py`(路由适配器策略替换)、`agentic_research/orchestrator.py`(证据评估器策略替换)、`agent.py` 与 `skills.py`(技能建议注入)、`rag/service.py` 与 `rag/retrieval.py`(门控阶段与缓存)、`agent.py`/`context_compaction.py`(压缩灰区)、`memory.py`(写入预筛、读取过滤)。
- 配置:`config.json` 新增 `jev` 配置段,默认 `enabled: false`;API key 走环境变量,不写入代码或仓库。
- 测试:每个接入点新增"开启-关闭-失败降级"三态测试与 mock 模式确定性测试;既有路由、RAG、压缩、记忆测试语义不变。
- 不改变:聊天 SSE 协议与前端行为、session 存储格式、工具 transcript、现有技能执行路径;不引入 `langchain-typesafe` 等 SDK 依赖(仅用现有 HTTP 客户端栈)。
- 隐私边界:live 模式下用户消息片段会作为判断状态发送至配置的 Jev 端点;功能默认关闭,启用前需在文档中说明。
- 合规:所有问题文本与阈值均与具体实体无关,通过配置驱动,符合 AGENTS.md 泛化性约束。
