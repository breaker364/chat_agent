## 1. bash 工具环境(P0)

- [ ] 1.1 新增单测:bash 工具子进程环境包含虚拟环境解释器目录与 VIRTUAL_ENV;通过工具执行 `python -c "print(sys.executable)"` 解析到 venv 内。
- [ ] 1.2 实现 `backend/tools.py` bash 子进程环境的 PATH 前插与 VIRTUAL_ENV 设置(由 `sys.executable` 推导)。

## 2. 源调用预算配置化(P0)

- [ ] 2.1 新增单测:配置覆盖默认、部分配置合并、非法条目回落、未配置回退四类路径。
- [ ] 2.2 实现预算读取(`agent.source_call_limits`),默认放宽为 personal_knowledge: 3、workspace: 6、web: 4,启动日志打印生效值。

## 3. 失败命令反馈与提示词约束(P1)

- [ ] 3.1 新增单测:同一命令第二次失败结果携带一次性反重试提示;首次失败无提示;成功调用无提示。
- [ ] 3.2 实现 bash 工具运行内失败指纹记录与 `retry_hint` 追加。
- [ ] 3.3 在 `backend/prompts/agent_policy.md` 追加两条约束(失败命令禁止原样重试;运行 Python 优先 run_python_file),并确认提示加载测试覆盖。

## 4. 恢复候选采纳(P1)

- [ ] 4.1 新增单测:候选 mtime 新于正式文件时被采纳并加载;候选较旧时被忽略;无候选时行为不变。
- [ ] 4.2 实现 `load_session` 的恢复候选扫描、提升(改名)与 warn 日志。

## 5. 知识栈启动韧性(P2)

- [ ] 5.1 在启动路径启用 `faulthandler.enable()`。
- [ ] 5.2 新增启动后台预热知识栈的调用,失败仅记日志;以 mock 验证启动时被调用且异常不阻塞服务。

## 6. 复测与验收

- [ ] 6.1 运行后端全部测试与前端构建,确认零回归。
- [ ] 6.2 重跑此前触发失败的 6 个任务(LP1/LP3/LP10/MT2/MT7/MT8),严格审计工具调用:成功率不低于 99%、零 budget_denied、零原样重试、零 blocked。
- [ ] 6.3 openspec validate 通过并归档本变更。
