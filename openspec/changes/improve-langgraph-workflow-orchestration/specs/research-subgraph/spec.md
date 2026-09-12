## ADDED Requirements

### Requirement: Research 子图必须实现规划、收集和评估闭环

Research 子图 MUST 将研究请求转换为结构化查询、来源策略、停止条件和预算，并按 `research_plan -> collect_evidence -> assess_evidence -> research_finalize` 的预定义路径执行；评估结果可以有限地触发 refine 或 next-source 路径。

#### Scenario: 研究计划生成

- **WHEN** 父图分发一个 research 类型任务
- **THEN** 子图生成带查询、来源策略、停止条件和预算的 `ResearchPlan`

#### Scenario: 证据不足需要细化

- **WHEN** 评估发现证据缺口且仍有预算
- **THEN** 子图生成受限的细化查询或下一个来源计划，继续收集并重新评估

#### Scenario: 证据满足停止条件

- **WHEN** 证据满足任务要求、最低可信度和停止条件
- **THEN** 子图进入 `research_finalize`，不再无界扩展检索

### Requirement: 研究证据必须去重、可引用并受预算约束

系统 MUST 为证据保存来源标识、内容摘要、采集时间、相关性/可信度评估和引用关系；相同证据不得重复计入预算，来源调用数、查询数、token、时间和结果数量均不得超过配置上限。

#### Scenario: 重复证据合并

- **WHEN** 多次检索返回相同来源和相同内容摘要
- **THEN** 子图合并为一个证据记录，追加引用关系但不重复计费

#### Scenario: 预算耗尽

- **WHEN** 研究达到任一预算上限且尚未完全满足目标
- **THEN** 子图停止新增收集，输出当前证据、未解决缺口和 budget_exhausted 状态

#### Scenario: 证据可追溯

- **WHEN** ResearchResult 被父图消费
- **THEN** 结果中的每个关键结论都能关联到一个或多个证据标识，无法关联的内容被标记为未证实

### Requirement: Research 子图必须隔离检索能力与父图控制能力

Research Agent MUST 只获得研究任务授权的检索、读取和证据评估能力；不得调用顶层 planner、scheduler、finalize 或任意写入能力，也不得直接修改父图计划。

#### Scenario: 研究任务调用检索

- **WHEN** Research Agent 需要补充证据
- **THEN** 子图只能调用授权的只读检索/读取工具，并将结果转换为证据观察

#### Scenario: 研究任务请求写入

- **WHEN** Research Agent 试图调用未授权写入或父图控制能力
- **THEN** 系统拒绝调用并记录权限事件，不改变父图状态

### Requirement: ResearchResult 必须向父图返回有限结果契约

Research 子图 MUST 返回结论摘要、证据引用、可信度、未解决缺口、预算消耗、状态和审计事件；父图不得依赖子图内部完整推理消息来判断任务完成。

#### Scenario: 研究成功回传

- **WHEN** 子图完成研究并通过结果校验
- **THEN** `dispatch_task` 将 ResearchResult 映射为任务结果，父图可以在 `assess` 和 `validate` 中使用结论及证据引用

#### Scenario: 研究部分完成回传

- **WHEN** 研究因预算、冲突或来源不可用而未完全完成
- **THEN** 结果明确标记 partial/blocked，并携带缺口，父图决定是否重试、重规划或返回受限答案
