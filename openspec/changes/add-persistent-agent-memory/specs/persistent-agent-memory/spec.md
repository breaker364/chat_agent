## ADDED Requirements

### Requirement: 可配置且受限的项目级记忆存储
系统 SHALL 通过 `agent_memory.enabled` 和受控的记忆根目录配置持久化 Agent 记忆。启用时，系统 SHALL 将每条记忆保存为记忆根目录内单独的 Markdown 文件，并使用 `MEMORY.md` 作为只含文件链接和摘要的索引。每条记录 SHALL 包含受校验的 `name`、`description`、`type`、`created_at`、`updated_at` 和 schema 版本；`type` SHALL 仅为 `user`、`feedback`、`project` 或 `reference`。

#### Scenario: 首次保存记忆
- **WHEN** 已启用记忆功能且通过校验的提取结果包含一条新记忆
- **THEN** 系统 SHALL 在受控记忆根目录创建对应记录和 `MEMORY.md` 索引条目，且索引条目只引用该根目录内存在的 Markdown 文件

#### Scenario: 功能被禁用
- **WHEN** `agent_memory.enabled` 为 false
- **THEN** 系统 SHALL 不提取、不读取、不注入或创建持久化记忆文件，且现有聊天行为 SHALL 保持可用

#### Scenario: 不安全的记忆标识被拒绝
- **WHEN** 提取结果或管理请求中的 memory id、文件名或前置元数据尝试使用绝对路径、目录穿越、空字节、目录分隔符或未允许的扩展名
- **THEN** 系统 SHALL 拒绝该条目且不得在记忆根目录以外读写任何文件

### Requirement: 通过结构化 LLM 判断提取长期记忆
系统 SHALL 在已持久化 assistant 回合后，以异步方式调用无工具的结构化 `MemoryExtractor`。提取器 SHALL 只接收配置限定数量的最近 user/assistant 消息和已存在记忆的有界 manifest，并 SHALL 返回受校验的 `create`、`update` 或空结果。提取器 SHALL 仅保存跨 session 有价值且不能可靠从代码、Git 或既有项目文档推导的信息。

#### Scenario: 提取用户偏好或协作反馈
- **WHEN** 最近对话包含适合长期复用的用户偏好、协作反馈、非代码可推导的项目背景或外部参考信息
- **THEN** 系统 SHALL 生成或更新对应类型的受控记忆记录，并避免为同一 memory id 添加重复索引条目

#### Scenario: 排除临时或可推导内容
- **WHEN** 最近对话只包含临时任务、当前执行进度、代码结构、Git 历史、调试方案、完整工具结果或可由当前项目状态推导的信息
- **THEN** 系统 SHALL 不创建持久化记忆

#### Scenario: 提取器不可用
- **WHEN** 记忆提取模型超时、调用失败、返回无效 JSON、返回未允许类型或未通过内容校验
- **THEN** 系统 SHALL 将本次提取安全地视为失败或空结果，记录受控错误类别，且不得改变已完成的聊天回答、session canonical history 或 run 终态

#### Scenario: 对话内容含有注入指令
- **WHEN** 最近对话内容要求提取器改变规则、扩大工具权限、输出原始敏感数据或写入受控目录之外
- **THEN** 系统 SHALL 将该内容作为待分析数据处理，继续执行固定提取规则，且不得执行其中的指令

### Requirement: 异步提取不会破坏并发会话与索引一致性
系统 SHALL 在不阻塞当前聊天 `done` 事件的前提下调度记忆提取。对于同一记忆根目录，系统 SHALL 避免多个提取任务同时写入，并 SHALL 在每次 create、update 或 delete 时以原子方式更新记忆记录和索引。系统 SHALL 将在提取进行期间完成的后续回合合并为有界的后续处理，而不是无限制地并行写入。

#### Scenario: 两个 session 同时完成
- **WHEN** 两个不同 session 的聊天回合几乎同时完成且都触发记忆提取
- **THEN** 系统 SHALL 串行化对同一记忆根目录的写入，并确保最终索引没有重复、悬空或目录外链接

#### Scenario: 异步提取发生在聊天完成后
- **WHEN** assistant 回答已经持久化并发送 `done` 后记忆提取仍在运行
- **THEN** 系统 SHALL 保持该回答、工具 transcript 和 session 状态不变，并仅记录有界的记忆提取状态或错误元数据

### Requirement: 在运行前注入有界记忆索引
系统 SHALL 在每次启用记忆的 Agent 运行中，将受限 `MEMORY.md` 索引追加到该运行的动态 system prompt。注入文本 SHALL 明确记忆是可能过时的参考数据而非指令，并 SHALL 指导 Agent 在使用重要记忆前通过现有受限读文件能力读取相关记录。系统 SHALL 不将完整记忆正文自动拼入 system prompt。

#### Scenario: 记忆索引可用
- **WHEN** 记忆功能已启用且索引包含有效条目
- **THEN** 运行的 system prompt SHALL 包含配置限制内的索引、记忆数据边界和按需读取指引

#### Scenario: 索引为空或无法读取
- **WHEN** 索引不存在、为空、损坏、超出安全限制或读取发生异常
- **THEN** 系统 SHALL 忽略不可用部分并继续正常运行，且不得使 Agent 请求失败

#### Scenario: 索引达到上限
- **WHEN** 有效索引超过配置的行数或字节数上限
- **THEN** 系统 SHALL 以确定性方式保留配置允许范围内的有效条目，并不得将超过限制的完整内容注入 prompt

### Requirement: 记忆记录可列出、读取和遗忘
系统 SHALL 提供受控的工作区级记忆管理接口，用于列出记忆摘要、读取单条受校验记录和删除单条记录。删除接口 SHALL 只接收 memory id，不得接收任意文件系统路径。删除完成后，系统 SHALL 原子更新 `MEMORY.md`，使其不再引用被删除记录。

#### Scenario: 列出记忆摘要
- **WHEN** 客户端请求当前工作区的记忆列表
- **THEN** 系统 SHALL 仅返回有界的 id、name、type、description 和更新时间，不返回提取输入、模型推理或无关 session 内容

#### Scenario: 用户遗忘一条记忆
- **WHEN** 客户端请求删除一个存在且受校验的 memory id
- **THEN** 系统 SHALL 删除该记忆文件及其索引条目，并不得修改任何 session、工具 transcript、RAG 数据或其他工作区文件

#### Scenario: 删除未知或无效记忆
- **WHEN** 客户端请求删除不存在或不安全的 memory id
- **THEN** 系统 SHALL 返回结构化的未找到或验证错误，且不得修改记忆目录或其他数据

### Requirement: 记忆操作具有安全可观测性和可测试性
系统 SHALL 为记忆提取、跳过、完成、失败和删除记录有界的活动或调试元数据，包括操作类别、计数、耗时和错误类别。系统 SHALL 不在 API、SSE、日志或调试事件中暴露原始提取消息、完整记忆正文、内部推理、凭据、请求头或未经脱敏的工具 payload。记忆存储、提取器、上下文提供器和调度器 SHALL 支持依赖注入，以便在无网络和无真实模型条件下测试。

#### Scenario: 记忆操作成功
- **WHEN** 一次受控的记忆 create、update 或 delete 成功
- **THEN** 系统 SHALL 记录不含正文的操作类别、类型和计数元数据，且不暴露输入对话或模型推理

#### Scenario: 离线确定性测试
- **WHEN** 测试注入假的提取器、时钟和临时记忆根目录
- **THEN** 测试 SHALL 能验证提取、注入、去重、并发调度、遗忘和失败降级，而不调用真实模型或网络服务
