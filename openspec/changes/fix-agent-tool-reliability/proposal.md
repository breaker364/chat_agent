## 为什么

三轮基准测试(26 个任务、约 440 次工具调用)暴露了 agent 运行时的一组系统性缺陷,合计造成 25 次工具调用失败、2 次运行被误标 blocked、1 次后端进程静默崩溃。按严格审计后的影响排序:

1. **bash 工具环境无 Python 解释器**(14 次失败,5 个任务):bash 工具在 Git Bash 中执行,PATH 不含项目虚拟环境,agent 用 `python`/`python3` 运行脚本全部 exit 127。专用工具 `run_python_file` 能正确解析解释器,agent 却倾向走 bash。
2. **源调用预算过硬且不可配置**(11 次拒绝,8/20 个长任务受影响):`tools.py` 硬编码 `{personal_knowledge: 1, workspace: 2, web: 2}`,一次运行中 read_file 全程只允许成功 2 次,长任务的合理文件读取被 `budget_denied` 拒绝,agent 被迫绕行。
3. **失败命令原样重试**(7 条唯一失败命令每条恰好重复调用一次):`python: command not found` 后 agent 未改变参数或换工具,直接重发同一条命令,失败次数翻倍。
4. **持久化竞争导致回复丢失**(2 次运行被误标 blocked):已修复的改名重试(028562d)解决了保存失败本身,但失败发生时恢复候选文件 retained 后前端永远读不到——阻塞运行的完整上下文躺在 `session.tmp-*.json` 里,产品层面不可见。
5. **知识栈加载偶发击穿进程**(1 次,致命):重排序模型权重加载完成后后端无 traceback 静默退出,无法定位原因,再发即全服务不可用且杀掉在途运行。

## 变更内容

- bash 工具子进程环境自动注入当前虚拟环境(由 `sys.executable` 推导,不写死路径),使 `python`/`python3`/`pip` 在 bash 中可用。
- 源调用预算改为 runtime_config 可配置(`agent.source_call_limits`),默认值放宽为适配长任务(workspace: 6,personal_knowledge: 3,web: 4),缺失时回退到安全默认。
- bash 工具记录运行内失败命令指纹:同一命令第二次失败时,在结果中附加明确提示(禁止原样重试、建议改用 run_python_file);系统提示(agent_policy)同步增加"失败命令不得原样重试、运行 Python 优先用 run_python_file"两条约束。
- 会话加载时采纳恢复候选:`load_session` 发现同名 `session.tmp-*.json` 恢复候选比 session.json 更新时,自动将其提升为正式会话并记录日志,使被阻塞运行的回复在下次查看时可见。
- 启动时启用 `faulthandler` 并预热知识栈(重排序/嵌入模型),把原生崩溃暴露在无流量阶段并留下可诊断输出。

## 能力

### 新增能力

- `agent-tooling-reliability`:bash 工具环境、源调用预算配置、失败命令反馈三者的可靠性约束与验收标准。
- `agent-runtime-resilience`:会话恢复候选采纳、知识栈启动预热与故障可诊断性的可靠性约束与验收标准。

### 已修改能力

无。

## 影响

- 后端:`backend/tools.py`(bash 环境注入、失败指纹提示、预算读取)、`backend/config.py` 或 `backend/runtime_context.py`(预算配置读取)、`backend/session_store.py`(恢复候选采纳)、`backend/main.py` 或启动路径(faulthandler、预热)、`backend/prompts/agent_policy.md`(两条行为约束)。
- 测试:预算配置回退/覆盖、bash 环境解析解释器、失败指纹提示、恢复候选采纳共四组新单测;既有测试不改变语义。
- 基准复测:修复后重跑此前触发失败的 6 个任务(LP1/LP3/LP10/MT2/MT7/MT8),验收标准为工具调用成功率不低于 99%、零 budget_denied、零 blocked。
- 不改变 SSE 协议、前端行为与模型配置;不引入新依赖。
