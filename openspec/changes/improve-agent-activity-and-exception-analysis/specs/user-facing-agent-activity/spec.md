## ADDED Requirements

### Requirement: 用户可读的实时工作动态事件
系统 SHALL 在请求生命周期内发送 `activity` SSE 事件。每个事件 SHALL 包含状态和自然语言摘要，并可包含已确认事实、判断、下一步、进度和已用时间。

#### Scenario: 请求开始
- **WHEN** 后端开始处理新的聊天请求
- **THEN** 系统 SHALL 发送 `understanding` 状态的工作动态，说明正在理解任务并准备处理

#### Scenario: 可观察执行阶段变化
- **WHEN** 工具启动、工具完成、结果被处理或发生修复重试
- **THEN** 系统 SHALL 发送或更新对应的工作动态，且内容只描述可观察的执行事实

### Requirement: 工作动态的隐私与频率控制
系统 SHALL 不在工作动态中展示模型隐藏思维链、原始工具参数、密钥、traceback 或每个模型 token。连续心跳 SHALL 更新当前工作项，而不是无限追加时间线条目。

#### Scenario: 长时间等待工具返回
- **WHEN** 请求在同一工具或阶段等待多个心跳周期
- **THEN** 前端 SHALL 显示更新后的当前工作状态，而不是为每个心跳新增一条用户时间线记录

### Requirement: 对话内实时活动时间线
前端 SHALL 在活跃助手回复内默认展开 `Agent 工作动态` 时间线，并实时展示当前操作、已确认事实、判断、下一步和进度（存在时）。Debug stream SHALL 保持独立可用。

#### Scenario: 前端收到活动事件
- **WHEN** 前端解析到 `activity` SSE 事件
- **THEN** 前端 SHALL 将其加入或更新本轮请求的对话内时间线，且不移除 Debug stream

#### Scenario: 正常完成
- **WHEN** 前端收到最终 `done` 事件
- **THEN** 前端 SHALL 将当前工作动态收束为完成状态，并保留最终回答和历史活动

#### Scenario: 终止失败
- **WHEN** 前端收到终止性 `error` 事件
- **THEN** 前端 SHALL 将当前工作动态收束为受阻状态，并显示后端提供的用户可读异常判断
