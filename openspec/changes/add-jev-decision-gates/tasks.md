## 1. 决策网关与配置(P0)

- [x] 1.1 在 `config.py` 新增 `load_jev_config()`:读取 `jev` 配置段(`enabled`、`mode`、`base_url`、`api_key_env`、`model`、`timeout_seconds`、`gates.*`),默认全部关闭;`config.example.json` 同步补充带注释的示例段。
- [x] 1.2 实现 `backend/jev_client.py`:`JevDecision` 数据类、批量提问 `ask(state, questions)`、超时与一次退避重试、异常归一为 `JevUnavailable`、mock 模式返回配置的固定答案。
- [x] 1.3 实现统一决策日志:记录接入点、问题名、概率/置信度、采纳来源、模型版本、耗时、缓存命中,原文只记长度与哈希前缀。
- [x] 1.4 编写网关测试:默认关闭不发请求、mock 模式确定性、超时/HTTP 错误归一为 `JevUnavailable`、决策日志字段完整且不含原文全文。

## 2. 记忆写入预筛(P0)

- [x] 2.1 在 `MemoryExtractionScheduler` 命中后、`StructuredMemoryExtractor` 调用前接入 Jev 是非题预筛,阈值 `jev.gates.memory_write.min_probability`,低概率跳过抽取并记录,判断失败时执行抽取。
- [x] 2.2 扩展现有 `memory_extraction_*` 事件,附加预筛结果元数据(来源、概率),不含消息原文。
- [x] 2.3 编写三态测试:关闭时行为与现状一致;开启且低概率时跳过抽取(LLM 抽取桩零调用);开启且判断失败时仍执行抽取。

## 3. RAG 召回门控(P1)

- [x] 3.1 在 `PersonalKnowledgeBase.search` 重排后、邻块扩展前插入门控阶段:仅前 `jev.gates.rag.max_passages` 个候选,每(查询, 分块)一次请求并行三道是非题(相关/前提矛盾/注入),阈值默认 0.45/0.70/0.70 走配置。
- [x] 3.2 实现冲突证据通道:矛盾分块不进入正常证据注入,单独作为冲突段返回;注入判定分块剔除并记录警告日志。
- [x] 3.3 实现(查询规范化串, 分块 id)缓存与规范化,命中缓存不发请求;门控失败或关闭时全部候选按原顺序放行,邻块扩展仅对保留块执行。
- [x] 3.4 编写门控测试:关闭时检索结果与现状一致;低相关剔除;矛盾块进入冲突通道;注入块剔除;Jev 失败放行全部;缓存命中;阈值可配置生效。

## 4. 路由与研究证据评估(P1)

- [x] 4.1 改造 `workflow_runtime.ModelRouteAdapter`:Jev 并行回答任务类型/是否查资料/风险等级,置信度达 `jev.gates.routing.min_confidence` 时组装现有路由载荷,置信度填入现有字段;不达标或失败走原 LLM 调用;`enforce_route` 零改动。
- [x] 4.2 改造 `agentic_research/orchestrator.ModelEvidenceAssessor`:五选一选择题,达标直接返回,否则回退 `DefaultEvidenceAssessor`。
- [x] 4.3 编写测试:高置信采纳(mock 固定高置信验证载荷组装与 `enforce_route` 兼容);低置信升级 LLM(升级路径被调用一次);Jev 不可用回退原路径;关闭时两次改造点行为与现状逐字节一致。

## 5. Skill 预选(P2)

- [x] 5.1 在系统提示组装阶段接入技能预选:用户消息 + 技能目录为状态,选择题返回;达 `jev.gates.skill.min_confidence` 时追加通用建议消息(不自动执行技能),否则不追加。
- [x] 5.2 编写测试:高置信命中注入建议;低置信/失败/关闭时系统提示与现状一致;建议消息不含技能正文且不改变工具可用性。

## 6. 压缩灰区与记忆读取过滤(P2)

- [x] 6.1 在 `_compact_history_if_needed` 增加灰区逻辑:剩余 token 落入灰区时由 Jev 判断近期历史是否承载未完成任务状态,判断为否则提前压缩,为真或失败维持原触发时机;硬阈值行为不变。
- [x] 6.2 在 `MemoryContextProvider.get_context` 增加读取过滤:条数超 `jev.gates.memory_read.filter_threshold` 时按当前用户消息做相关性判断,只注入达标条目;未超限/失败/关闭时全量注入。
- [x] 6.3 编写测试:灰区内为否时提前压缩且摘要缓存语义不变;灰区判断失败保持原时机;硬阈值始终生效;记忆过滤三态与全量注入上限不回归。

## 7. 文档、验证与验收(P2)

- [x] 7.1 更新 README/用户文档:Jev 功能说明、配置项、隐私边界(live 模式外发内容)、mock 模式用法、各阈值含义与默认值。
- [x] 7.2 运行全部新增测试与既有回归(路由、RAG、压缩、记忆、agent 流式、API),确认关闭状态下无行为差异。
- [x] 7.3 检查 AGENTS.md 合规:问题文本、阈值、配置无任何实体硬编码;运行 Python 编译与 `git diff --check`。
- [x] 7.4 mock 模式下端到端手工验证四个区域各一个场景,确认决策日志、降级路径与关闭态一致性;live 模式验证待 API key 就绪后补充执行。
  - 执行记录(2026-09-25):全量后端测试 477 passed / 3 skipped;mock 冒烟覆盖路由、证据、技能、记忆写入、记忆读取、压缩灰区、RAG 七个门控;
  live 模式验证 pending(TYPESAFE_API_KEY 未就绪)。
