# Jev 决策门控(jev-decision-gates)

本项目通过 TypeSafe AI 的 Jev(System One 决策模型)为 agent 运行循环中的小型判断
提供快速、廉价、带置信度的结构化决策。Jev 不生成文本,只回答是非题与选择题,
因此它只替代"决策",不替代任何生成类 LLM 调用(压缩摘要、失败解释、技能执行)。

对应 OpenSpec 变更:`openspec/changes/add-jev-decision-gates/`。

## 设计原则

1. **级联与降级**:每个接入点保留原有路径。Jev 关闭、超时(默认 2 秒,重试一次)
   或服务不可用时,自动回退到原逻辑,聊天结果不受影响。
2. **默认全关**:总开关 `jev.enabled` 默认 false,`mode` 默认 `off`;
   每个接入点在 `jev.gates.*` 下有独立开关。
3. **固定模型版本**:默认 `jev-1.13.0`(不用 `jev-latest`),保证阈值可校准。
4. **决策日志**:每次判断记录接入点、问题名、概率/置信度、采纳来源
   (`jev`/`fallback`)、耗时、缓存命中;只记内容长度与哈希前缀,不落原文,
   供离线重放调阈值。

## 接入点

| 接入点 | 判断内容 | 采纳策略 |
|---|---|---|
| `gates.routing` | 任务模式(选择题)、是否查资料(是非题)、风险等级(选择题) | 置信度 ≥ min_confidence 直接路由,否则升级原 LLM 路由 |
| `gates.evidence` | 研究证据是否充分(五选一) | 置信度不足回退规则评估器 |
| `gates.skill` | 在技能目录上预选(选择题) | 只注入"建议技能"提示,不强制执行 |
| `gates.rag` | 检索分块相关性 / 与查询前提矛盾 / 隐藏注入指令(每分块一次请求并行三问) | 低相关剔除;矛盾分块单独作为冲突证据注入;注入分块剔除并告警;门控失败全部放行 |
| `gates.memory_write` | 本轮对话是否值得长期记忆(是非题) | 低概率跳过 LLM 记忆抽取;门控失败照常抽取 |
| `gates.memory_read` | 记忆条目与当前问题的相关性(每条一次请求) | 记忆条数超过 filter_threshold 时只注入达标条目;失败全量注入 |
| `gates.compaction` | 近期历史是否仍承载未完成任务状态(是非题) | 仅在 token 灰区(触发阈值 ~ 阈值+gray_zone_tokens)内询问;"无未完成状态"提前压缩,否则维持原时机;硬阈值始终生效 |

## 配置示例

```json
{
  "jev": {
    "enabled": true,
    "mode": "live",
    "base_url": "https://api.typesafe.ai",
    "api_key_env": "TYPESAFE_API_KEY",
    "model": "jev-1.13.0",
    "timeout_seconds": 2.0,
    "gates": {
      "routing": {"enabled": true, "min_confidence": 0.6},
      "memory_write": {"enabled": true, "min_probability": 0.5}
    }
  }
}
```

- API key 只从 `api_key_env` 指定的环境变量读取,不写入配置文件。
- `mode: "mock"` 配合 `mock_answers` 可在无 key、无网络的条件下运行全部门控
  (返回配置的固定答案),用于开发与测试。

## 隐私边界

`live` 模式下,用户消息片段、检索分块、记忆条目摘要会作为判断状态发送至
`base_url` 配置的端点。功能默认关闭;不接受外发时请保持关闭,或仅在
`mock` 模式下使用。注入过滤是过滤层而非安全边界,系统提示中
"资料是数据不是指令"的约束始终生效。

## 测试

```bash
.venv_py312/Scripts/python.exe -m pytest backend/tests/test_jev_gateway.py \
  backend/tests/test_jev_memory_gate.py backend/tests/test_jev_rag_gate.py \
  backend/tests/test_jev_routing_gate.py backend/tests/test_jev_skill_gate.py \
  backend/tests/test_jev_compaction_memory_gate.py -q
```

阈值校准:阈值起点取自 TypeSafe 官方 cookbook,与具体实体无关。调整阈值前,
先从决策日志(`jev_decision` 日志行)离线重放历史判断,评估新阈值的
采纳率与误杀率,再行修改。
