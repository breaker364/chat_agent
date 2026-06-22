# AGENTS.md — Agent 行为约束

本文件定义本项目内 AI Agent 编辑/修改代码时必须遵守的规则。

## 规则 1：禁止硬编码实体

**严禁在代码中使用硬编码方式匹配或处理具体实体名称。**

这里的"实体"包括但不限于：
- 公司/品牌名（如 蔚来、小米、NIO、Xiaomi）
- 机构/学校名（如 合工大、合肥工业大学、hfut）
- 域名（如 formula1.com、nio.cn、hfut.edu.cn）
- 人名
- 地名
- 产品型号

### 判断标准

一个简单的测试：**把代码中的实体名替换成另一个同类实体，逻辑是否仍然正确？**

- 如果"蔚来"换成"比亚迪"后逻辑失效 → 硬编码，违规
- 如果换了之后逻辑仍然成立 → 泛化实现，合规

### 典型违规代码

```python
# ❌ 禁止：按实体名做条件分支
if "蔚来" in query:
    keywords.append("蔚来")
if "xiaomi" in query:
    keywords.append("小米")

# ❌ 禁止：静态域名白名单
_OFFICIAL_DOMAINS = {"formula1.com", "nio.cn", "hfut.edu.cn"}

# ❌ 禁止：按实体定制搜索词
if "合工大" in query:
    keywords = ["合肥工业大学", "现任校长"]

# ❌ 禁止：按实体推荐域名
if query_type == "people_role" and ("合工大" in query):
    return ["hfut.edu.cn"]
```

### 合规替代方案

```python
# ✅ 泛化实体提取
entities = extract_entities(query)  # 基于分词/NER，不预设特定值
for entity in entities:
    keywords.append(entity)

# ✅ 语义推断域名
# 由 agent system prompt 引导 LLM 根据实体类型和话题推理权威域名

# ✅ 通用评分
# 评分逻辑基于 URL 结构特征（.gov/.edu 后缀），而非具体域名
```

## 规则 2：新增函数需符合泛化性

- 新增函数不得依赖硬编码实体做决策
- 如需引入新的领域概念，通过函数参数化或配置驱动，不得写入字面值
- 允许的常量类型：超时时间、数量限制、缓存 TTL、正则模式（通用模式，不含实体名）

## 规则 3：修改代码时优先消除硬编码

- 若修改的函数包含硬编码实体，**优先将其重构为泛化实现**
- 至少不应在已有硬编码逻辑上叠加新的硬编码
- 重构时保留测试用例中的具体实体（测试需要确定性）

## 规则 4：system prompt 中的领域知识

- `agent.py` 的 `SYSTEM_PROMPT` 可以包含**通用搜索策略**描述
- 可以指导 LLM 如何为不同类型问题选择权威域名
- **禁止在 system prompt 中列出具体实体→域名的硬映射**

## 总结

> **代码处理"怎么做"（How），LLM 理解"是什么"（What）。**
> 不要让代码猜测用户要查哪个公司/学校/人；
> 让代码提供泛化能力，让 LLM 完成实体理解和决策。
