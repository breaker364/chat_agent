## ADDED Requirements

### Requirement: 恢复候选在加载时自动采纳
会话目录中存在 `session.tmp-*.json` 恢复候选且其修改时间新于 `session.json` 时,`load_session` SHALL 将该候选提升为正式会话文件并加载其内容,同时记录 warn 日志;候选不新于正式文件时 SHALL 忽略候选。被阻塞运行的回复 SHALL 因此在下次查看会话时可见。

#### Scenario: 阻塞运行后查看会话
- **WHEN** 某运行因持久化竞争失败而留下恢复候选,之后该会话被加载
- **THEN** 系统 SHALL 采纳候选为正式会话,此前未落盘的 assistant 回复 SHALL 可见

#### Scenario: 正常会话不受影响
- **WHEN** 会话仅存在正常保存的 session.json 而无更新候选
- **THEN** 加载行为 SHALL 与现状完全一致

### Requirement: 原生崩溃可诊断
后端进程 SHALL 在启动时启用 faulthandler,使原生层崩溃在标准错误输出原生栈,替代当前的静默退出。

#### Scenario: 启动启用 faulthandler
- **WHEN** 后端启动
- **THEN** faulthandler SHALL 处于启用状态,后续原生崩溃 SHALL 在日志中留下可诊断输出

### Requirement: 知识栈启动预热
后端启动后 SHALL 在后台预热知识栈(触发重排序与嵌入模型权重加载);预热失败 SHALL 仅记录日志,SHALL 不阻塞服务就绪,也不影响后续首次检索时按需加载。

#### Scenario: 启动完成即完成预热
- **WHEN** 后端启动且知识栈预热成功
- **THEN** 首次知识检索 SHALL 不再触发权重加载,启动阶段暴露的加载问题 SHALL 在无流量时被观察到

#### Scenario: 预热失败不阻塞服务
- **WHEN** 预热过程抛出异常
- **THEN** 服务 SHALL 正常就绪,异常 SHALL 记录日志,后续检索 SHALL 可按需重新加载
