## ADDED Requirements

### Requirement: bash 工具可解析项目 Python 解释器
bash 工具构造子进程环境时 SHALL 将当前虚拟环境的解释器目录前插到 PATH 并设置 `VIRTUAL_ENV`,使 `python`、`python3`、`pip` 在 bash 中可直接执行,且解析到的解释器 SHALL 为当前运行后端的同一环境。

#### Scenario: bash 中运行 python
- **WHEN** agent 通过 bash 工具执行 `python -c "import sys; print(sys.executable)"`
- **THEN** 命令 SHALL 成功执行且输出的解释器路径位于后端当前虚拟环境目录内

#### Scenario: 无需绝对路径
- **WHEN** agent 在 bash 中运行 `python --version` 或 `pip --version`
- **THEN** SHALL 不再出现 `command not found`

### Requirement: 失败命令的反重试提示
bash 工具 SHALL 在运行内记录失败命令指纹;同一命令第二次失败时,返回结果 SHALL 携带明确的反重试提示(禁止原样重试,运行 Python 建议改用 run_python_file 工具),第三次及以后 SHALL 不重复堆叠提示。

#### Scenario: 同一命令第二次失败
- **WHEN** agent 以完全相同的命令再次触发失败
- **THEN** 第二次结果 SHALL 包含反重试提示,且该提示只出现一次

#### Scenario: 首次失败不加提示
- **WHEN** 某命令首次失败
- **THEN** 结果 SHALL 不包含反重试提示

### Requirement: 系统提示固化工具使用约束
系统提示 SHALL 包含两条约束:命令失败后禁止原样重试(必须先分析错误或改用其他工具);运行 Python 脚本优先使用 run_python_file 工具而非 bash。

#### Scenario: 提示加载
- **WHEN** agent 构造系统提示
- **THEN** 提示 SHALL 同时包含上述两条约束的表述

### Requirement: 源调用预算可配置
源调用预算 SHALL 从 runtime_config 的 `agent.source_call_limits` 读取,仅接受 personal_knowledge、workspace、web 三类整数键;未配置或非法条目 SHALL 回落到内置默认(默认值 SHALL 放宽为 personal_knowledge: 3、workspace: 6、web: 4)。生效值 SHALL 在启动日志中可见。

#### Scenario: 配置覆盖默认
- **WHEN** runtime_config 提供 `agent.source_call_limits` 的合法条目
- **THEN** 对应类别的预算 SHALL 采用配置值,未提供类别 SHALL 采用默认值

#### Scenario: 非法配置回落
- **WHEN** 配置中某类别为负数或非整数
- **THEN** 该类别 SHALL 回落默认值,服务 SHALL 正常启动
