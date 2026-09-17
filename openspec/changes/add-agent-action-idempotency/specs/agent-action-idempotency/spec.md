## ADDED Requirements

### Requirement: 工具失败结果携带错误分类
任何工具调用失败时,返回给模型的结果与运行内失败记录 SHALL 携带 `error_category` 与 `retryable` 字段。分类 SHALL 由异常类型与状态语义经通用映射得出:超时与网络类 SHALL 判为可重试;参数错误、权限拒绝、策略拒绝、预算拒绝、目标不存在与业务拒绝 SHALL 判为不可重试;无法识别的异常 SHALL 默认判为可重试。映射 SHALL 为通用模式,SHALL NOT 依赖任何具体实体。

#### Scenario: 超时错误可重试
- **WHEN** 某工具调用因超时失败
- **THEN** 结果 SHALL 携带可重试类别的 `error_category` 且 `retryable` 为真

#### Scenario: 权限拒绝不可重试
- **WHEN** 某工具调用因权限拒绝失败
- **THEN** 结果 SHALL 携带不可重试类别的 `error_category` 且 `retryable` 为假

#### Scenario: 未知异常默认可重试
- **WHEN** 某工具调用抛出映射未覆盖的异常
- **THEN** 结果 SHALL 判为可重试,修复路径与现状一致

### Requirement: 不可重试错误不重复占用修复轮次
agent 修复循环 SHALL 读取失败的 `retryable` 字段:为假且同一动作指纹在本运行内已失败时,SHALL NOT 将相同调用再次注入下一轮修复;为真时 SHALL 维持既有有界修复路径,修复轮次上限行为不变。

#### Scenario: 参数错误后不原样重发
- **WHEN** 某调用因参数错误失败且模型再次发出完全相同的调用
- **THEN** 运行时 SHALL NOT 为该调用开启新一轮修复,错误 SHALL 直接进入失败汇总

#### Scenario: 超时后维持有界修复
- **WHEN** 某调用因超时失败
- **THEN** 修复循环 SHALL 按既有上限注入错误上下文并续跑

### Requirement: 失败动作指纹的一次性反馈
工具包装层 SHALL 以运行内动作指纹为键记录失败次数;同一指纹第二次失败时,结果 SHALL 追加一次性反重试提示,第三次及以后 SHALL 不重复堆叠;该指纹成功后 SHALL 清空计数。该机制 SHALL 覆盖全部工具,bash 命令指纹为其特例。

#### Scenario: 同一动作第二次失败
- **WHEN** 同一工具以产生相同动作指纹的参数再次失败
- **THEN** 第二次结果 SHALL 包含反重试提示,且该提示只出现一次

#### Scenario: 首次失败与成功后重置
- **WHEN** 某动作首次失败,或在失败后同指纹调用成功
- **THEN** 首次失败结果 SHALL 不含提示,成功后计数 SHALL 清空

### Requirement: 动作标识确定性推导且可审计
副作用工具调用的结果与全部工具审计事件 SHALL 携带 `action_id`。`action_id` SHALL 由工具/提供方、资源、目标、操作与规范参数哈希确定性推导,SHALL NOT 采用模型生成的调用标识;同一逻辑动作重复派发时 `action_id` SHALL 保持稳定,拦截、复用、执行三类审计动作均 SHALL 携带。

#### Scenario: 同一动作重复派发标识稳定
- **WHEN** 模型以不同调用标识但相同规范参数重发同一副作用调用
- **THEN** 两次调用的 `action_id` SHALL 相同,审计事件可据其关联

#### Scenario: 参数不同标识不同
- **WHEN** 两次调用的规范参数不同
- **THEN** 其 `action_id` SHALL 不同

### Requirement: 变更成功必须经执行后校验
mutating skill 派发成功后 SHALL 执行校验回调,重读目标状态并与期望比对:比对通过方可以 `verified=True` 记入成功台账;回读失败或不一致 SHALL 将结果标记 `verification_failed` 返回,SHALL NOT 记入成功台账,运行按既有 blocked 路径处理;无回读通道的资源 SHALL 以 `verified=False` 入账并在结果中明示未验证。台账的 `verified` 字段 SHALL NOT 取自派发结果的自我报告。

#### Scenario: 回读一致入账
- **WHEN** 某变更派发后重读目标状态与期望一致
- **THEN** 该变更 SHALL 以 `verified=True` 记入成功台账,同身份重复派发 SHALL 复用台账结果

#### Scenario: 回读不一致不入账
- **WHEN** 某变更派发后重读目标状态与期望不一致
- **THEN** 结果 SHALL 标记 `verification_failed`,成功台账 SHALL 不含该变更

#### Scenario: 无回读通道明示未验证
- **WHEN** 目标资源无读取通道
- **THEN** 变更 SHALL 以 `verified=False` 入账,结果 SHALL 明示未验证

### Requirement: 目标已满足的变更跳过派发
目标状态可读取时,mutating skill 派发前 SHALL 检查当前状态是否已满足期望:精确匹配 SHALL 跳过派发,结果标记 `already_satisfied` 并按查重命中语义记入台账;非精确匹配 SHALL 正常派发;目标不可读取时 SHALL 跳过检查直接派发,SHALL NOT 因检查失败阻塞。

#### Scenario: 重复请求已完成的变更
- **WHEN** 某变更请求的目标状态已与期望精确一致
- **THEN** 运行时 SHALL 跳过派发,结果 SHALL 标记 `already_satisfied`

#### Scenario: 状态不匹配正常执行
- **WHEN** 目标当前状态与期望不一致或无法读取
- **THEN** 派发 SHALL 正常执行,不因 pre-check 受阻
