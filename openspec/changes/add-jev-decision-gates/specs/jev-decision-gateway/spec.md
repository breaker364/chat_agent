## ADDED Requirements

### Requirement: 可配置且默认关闭的 Jev 决策网关
系统 SHALL 提供统一的 Jev 决策网关模块,由 `jev` 配置段控制:`enabled` 总开关默认为 false,`mode` 仅为 `live`、`mock`、`off`,模型版本 SHALL 使用固定版本号而非浮动别名。API key SHALL 只从配置指定的环境变量读取,SHALL 不出现在代码、仓库或日志中。每个接入点(路由、证据评估、技能、RAG、记忆写入、记忆读取、压缩)SHALL 拥有独立开关,关闭即完全走原路径。

#### Scenario: 默认关闭
- **WHEN** 配置中不存在 `jev` 段或 `enabled` 为 false
- **THEN** 系统 SHALL 不发起任何 Jev 请求,所有接入点行为与现状完全一致

#### Scenario: mock 模式确定性
- **WHEN** `mode` 为 `mock` 且配置了固定答案
- **THEN** 网关 SHALL 不发起网络请求并按配置返回确定性答案,供无 key 开发与测试使用

#### Scenario: 独立开关
- **WHEN** 某接入点开关关闭而总开关开启
- **THEN** 仅该接入点 SHALL 走原路径,其他接入点不受影响

### Requirement: 级联降级保证
每个接入点 SHALL 遵循统一级联语义:Jev 超时、网络失败、无效响应或服务不可用时,SHALL 自动回退到该接入点的原路径(规则、保守放行或原 LLM 调用),SHALL 不阻塞当前聊天响应、不向用户暴露错误、不改变 run 终态。网关超时 SHALL 有默认上限且重试至多一次。

#### Scenario: 超时降级
- **WHEN** 某 Jev 请求超过配置超时时间且重试后仍失败
- **THEN** 该接入点 SHALL 按其定义的回退行为继续执行,聊天结果 SHALL 与未接入时语义一致

#### Scenario: 服务不可用
- **WHEN** Jev 端点持续不可达
- **THEN** 所有开启的接入点 SHALL 稳定运行在回退路径上,系统 SHALL 不因反复失败产生性能退化或错误风暴

### Requirement: 决策日志可观测且脱敏
网关 SHALL 为每次判断记录结构化决策日志:接入点名称、问题名与类型、返回概率/选项/置信度、实际采纳来源(如 jev、升级、回退)、响应中的模型版本、耗时与缓存命中情况。日志 SHALL NOT 包含消息原文、检索分块全文或其他原始内容,只允许记录长度与哈希前缀。

#### Scenario: 记录一次采纳的判断
- **WHEN** 某 Jev 判断被采纳
- **THEN** 决策日志 SHALL 包含完整的问题结果与采纳来源字段,且 SHALL NOT 包含任何原文全文

#### Scenario: 记录一次降级
- **WHEN** 某 Jev 判断因失败回退原路径
- **THEN** 决策日志 SHALL 记录失败类别与回退行为,便于统计降级率
