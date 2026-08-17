## ADDED Requirements

### Requirement: 向主 Agent 暴露已注册的证据工具
当个人知识和 Agent 研究功能启用时，系统 SHALL 将每个已注册的只读证据工具加入主 Agent 可调用的工具 schema。系统 MUST NOT 仅因为启用了规划能力就移除证据工具。

#### Scenario: Agent 可以看到并调用知识库搜索
- **当** RAG 已启用且 `knowledge_search` 注册成功
- **则** 主 Agent 工具 schema 必须包含 `knowledge_search`，且 Agent 正常调用该工具时必须产生标准工具调用事件

#### Scenario: RAG 关闭时不暴露知识库搜索
- **当** RAG 已关闭
- **则** 主 Agent 工具 schema 不得包含 `knowledge_search`

### Requirement: 将 ModelResearchPlanner 提供为有边界的 Agent 工具
当 Agent 研究功能启用时，系统 SHALL 暴露只读的 `plan_research_route` Agent 工具。该工具 MUST 调用 `ModelResearchPlanner`、校验返回的计划，并且只返回有边界的路由字段或失败类别。该工具 MUST NOT 调用知识库、工作区、联网或任何修改状态的工具。

#### Scenario: planner 返回有效路由建议
- **当** Agent 针对不明确请求调用 `plan_research_route`
- **则** 工具必须返回经过 schema 校验的来源顺序、有边界的查询或范围、新鲜度要求和成功标准

#### Scenario: planner 失败可被观察
- **当** planner 调用或计划校验失败
- **则** 工具必须返回结构化的非成功状态和错误类别，不得调用任何证据服务，并产生会话可见的有边界工具结果

### Requirement: 由 Agent 负责研究路由
对于没有明确来源选择的请求，主 Agent SHALL 先判断能否直接回答。如果判断需要外部证据，MUST 先调用 `plan_research_route`，再选择证据来源；是否执行 planner 建议的来源仍由主 Agent 负责。

#### Scenario: 直接回答跳过规划和检索
- **当** Agent 判断请求不需要外部证据即可回答
- **则** Agent 必须直接返回答案，不得调用 `plan_research_route` 或证据工具

#### Scenario: 不明确的研究请求使用二级规划
- **当** Agent 判断自动证据请求需要外部支持，且用户没有指定来源
- **则** Agent 必须在调用证据工具前调用 `plan_research_route`

### Requirement: 显式来源请求优先于规划
系统 SHALL 将用户明确选择个人知识或联网搜索视为显式路由。Agent MUST 直接调用对应且策略允许的证据工具，MUST NOT 在该来源调用前调用 `plan_research_route`。

#### Scenario: 必须使用知识库时跳过 planner
- **当** 聊天请求解析为 `knowledge_policy=required`
- **则** Agent 必须在任何可选证据来源之前调用 `knowledge_search`，不得通过 `plan_research_route` 决定是否调用它

#### Scenario: 用户明确要求联网搜索时跳过 planner
- **当** 用户明确要求 Agent 搜索网络，且请求策略没有禁止联网
- **则** Agent 必须直接调用 `web_search`，不得在调用前执行 `plan_research_route`

### Requirement: 在执行层强制证据边界
系统 SHALL 在暴露的证据工具执行路径上强制执行来源策略和调用预算。在回答生成期间，系统 MUST 拒绝通过文件或 Python 工具直接读取配置的知识库内部索引文件来替代 `knowledge_search`。

#### Scenario: 禁用知识库策略阻断服务调用
- **当** Agent 在 `knowledge_policy=disabled` 的请求中调用 `knowledge_search`
- **则** 工具包装器必须返回有边界的策略拒绝结果，且不得调用底层检索服务

#### Scenario: 内部索引回退被阻断
- **当** 回答生成运行尝试通过文件或 Python 工具读取配置知识库检索索引内的路径
- **则** 系统必须拒绝该操作，并说明索引知识必须通过 `knowledge_search` 访问

### Requirement: 持久化有边界的路由可观测性
系统 SHALL 将 planner 和证据调用作为标准 Agent 工具事件持久化，并在适用时记录有边界的路由结果，包括路由类别、策略、来源尝试和失败类别。路由结果 MUST NOT 保存 planner 推理、原始证据内容、原始工具 payload、向量或凭据。

#### Scenario: 自动路由可以在会话中审计
- **当** Agent 完成一次自动规划的研究路由
- **则** 会话历史必须显示 planner 工具调用、证据工具调用和有边界的路由结果，并足以区分 planner、服务商、无结果、策略和预算失败
