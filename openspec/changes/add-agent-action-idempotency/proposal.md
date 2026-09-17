## 为什么

当前运行时已有四层局部幂等机制:工具层运行内去重与会话级结果缓存(`tools.py` 的 `_TOOL_DEDUPE_CACHE` 与 conversation cache)、副作用工具同参重发拦截(`duplicate_side_effect_tool_call`)、远端变更成功台账(`mutation_guard.py` 的 `MutationLedger`)、agent 循环有界修复重试(`MAX_AGENT_REPAIR_PASSES = 2`)。基准审计(约 440 次工具调用、25 次失败)暴露出这些机制合在一起仍留有四个缺口:

1. **错误不分类,重试不区分**。只有 RAG 子系统的 `RemoteSourceError.retryable` 做了错误分类;agent 主循环对任何异常走同一条修复路径,把参数错误、权限错误与超时、网络抖动同等对待。观测结果是 7 条唯一失败命令每条被原样重试一次,失败次数翻倍。
2. **失败指纹只覆盖 bash**(见未实施的 fix-agent-tool-reliability 草案第 3 项)。同一工具以相同参数反复失败时,其他工具没有等价的反重试反馈。
3. **变更校验是自报的**。`MutationLedger.record_success` 的 `verified` 字段直接取自 skill 结果负载的 `verified` 字段,没有任何真实的执行后校验(post-check),"验证是否真的成功写入"目前不成立。
4. **幂等决策不可观测**。去重键、拦截原因、复用来源只存在于内存与部分 audit 事件中,工具结果与审计链路没有统一的动作标识,排查"同一个动作为什么执行了/为什么被拦"缺乏依据。

## 变更内容

- 建立通用工具错误分类:失败的工具调用结果与异常统一携带 `error_category` 与 `retryable` 字段;分类由异常类型与结果状态码映射得出(超时、网络类可重试;参数、权限、策略拒绝、业务拒绝不可重试),不依赖任何具体实体。
- agent 修复循环消费错误分类:不可重试错误的同一调用不再进入下一轮修复重试;可重试错误维持既有有界修复路径。
- 失败调用指纹反馈泛化到全部工具:运行内同一调用指纹第二次失败时,结果追加一次性反重试提示,第三次起不再堆叠。
- 幂等键统一透出为 `action_id`:复用 `build_idempotency_key` 的确定性推导(工具/提供方、资源、目标、操作、规范参数哈希,排除模型生成的调用标识),在副作用工具结果与工具审计事件中携带,使拦截、复用、执行三类决策可追溯。
- 变更后校验(post-check)落地:mutating skill 派发后执行校验回调,重读目标状态并与期望比对;`verified` 只由真实校验写入,校验失败标记 `verification_failed` 且不记入成功台账。
- 变更前检查(pre-check):目标状态可读取时,派发前确认其是否已满足期望,精确匹配则跳过执行并记录 `already_satisfied`;不可读取时跳过 pre-check 直接执行,不阻塞。

## 能力

### 新增能力

- `agent-action-idempotency`:工具动作的确定性动作标识、错误分类与重试语义、失败指纹反馈、变更前后校验的可靠性约束与验收标准。

### 已修改能力

无。`stabilize-agent-execution-p0` 的 `safe-remote-mutation-guard` 与本变更互补:该能力约束远端标准化命令的幂等与预算,本变更把校验做实并把机制 observability 补齐;`fix-agent-tool-reliability` 草案的 bash 失败指纹是本变更失败指纹要求在 bash 上的特例,该草案先行实施时其要求被本机制覆盖。

## 影响

- 后端:`backend/tools.py`(错误分类字段、失败指纹泛化、action_id 透出)、`backend/mutation_guard.py`(校验回调与 pre-check 钩子)、`backend/skills.py`(派发前 pre-check、派发后真实校验)、`backend/agent.py`(修复循环消费 retryable)。
- 测试:错误分类映射、修复循环对不可重试错误的短路、失败指纹一次性提示、action_id 透出、post-check 通过/失败、pre-check 跳过共六组新单测;既有测试语义不变。
- 不改变 SSE 协议与前端行为;不引入新依赖;不改动模型配置。
