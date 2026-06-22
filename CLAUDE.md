# CLAUDE.md — 项目级 Agent 编码约束

## 核心原则：杜绝硬编码与硬匹配

**本项目严禁在工具代码、prompt 工程、搜索逻辑、结果评估等任何模块中使用硬编码/硬匹配方式处理具体实体（公司名、人名、地名、品牌、机构等）。**

### 什么是硬编码/硬匹配（反模式）

硬编码是指将具体实体的**字面值**直接写入逻辑分支、条件判断、关键词列表或域名映射中，而非通过**泛化机制**（语义推断、实体识别、LLM 推理）动态决定行为。

典型反模式示例：

```python
# ❌ 反模式：硬编码具体公司名
if "蔚来" in query or "nio" in lowered:
    keys.append("蔚来")
if "xiaomi" in lowered or "小米" in query:
    keys.append("小米")

# ❌ 反模式：硬编码具体机构名
if "合工大" in query:
    keywords = ["合肥工业大学", "现任校长"]

# ❌ 反模式：硬编码具体域名
_OFFICIAL_DOMAINS = {
    "formula1.com", "nio.cn", "hfut.edu.cn", "xiaomi.com",
}
```

### 正确做法（泛化模式）

```python
# ✅ 泛化：通过实体识别 + 通用规则
entities = extract_entities(query)  # NER / 关键词提取
for entity in entities:
    keywords.append(entity)

# ✅ 泛化：通过 query_type 语义推断，而非具体实体匹配
if query_type == "people_role":
    # 从查询中动态提取人名、机构名、角色词
    person_names = extract_person_names(query)
    org_names = extract_org_names(query)
    role_terms = extract_role_terms(query)

# ✅ 泛化：域名由 LLM 根据实体和话题推理，不做静态映射
# 让 agent 的 system prompt 引导模型自己决定权威域名
```

### 适用范围

此约束适用于本项目所有模块：

| 模块 | 禁止项 |
|------|--------|
| `tools.py` 搜索/抓取 | 实体名、域名、关键词的硬编码条件分支 |
| `tools.py` 查询构造 | 按具体实体定制搜索词、site: 指令 |
| `tools.py` 结果评估 | 按具体域名做硬编码评分加减 |
| `agent.py` system prompt | 但可保留**通用规则**型的搜索策略描述 |
| `config.py` | 禁止硬编码实体相关的配置项 |

### 为何禁止

1. **泛化性为零**：问题从"蔚来 F1 工厂地址"变为"比亚迪工厂地址"时，所有硬编码分支立即失效
2. **维护成本爆炸**：每新增一个实体都需要修改代码逻辑
3. **与 LLM 架构矛盾**：本项目的核心优势是 LLM 推理，硬编码等同于用规则引擎替代 LLM，本末倒置
4. **领域耦合**：当前代码深度耦合汽车/高校领域，无法作为通用搜索工具复用

### 已有硬编码清单（需逐步消除）

以下为 `backend/tools.py` 中已识别的硬编码问题：

- L61-96: `_OFFICIAL_DOMAINS` / `_VERTICAL_DOMAINS` / `_TECH_MEDIA_DOMAINS` / `_AGGREGATOR_DOMAINS` — 静态域名集
- L444-525: `_extract_keywords_for_query_v2/v3` — 蔚来/小米/合工大/合肥工业大学 硬编码分支
- L536-572: `_extract_keywords_for_query` — 同上
- L765-777: `_infer_preferred_domains` — 按具体实体推荐域名
- L780-788: `_infer_preferred_domains_v2` — 同上 + baike.baidu.com
- L30-31: `_MAX_BING_RESULTS` / `_MAX_SEARCH_VARIANTS` — 这些属于**可调参数**，不违规

## 编辑代码规范

1. **任何新增的工具函数不得引入新的硬编码实体**
2. **修改现有函数时，应优先将硬编码逻辑替换为泛化实现**
3. **如需领域知识，通过 system prompt 引导 LLM 推理，而非代码中 if/else**
4. **配置项**（超时、数量限制、缓存 TTL）可用常量，**领域知识**不可
5. **测试用例**中的具体实体不在此限（测试需要确定性）
