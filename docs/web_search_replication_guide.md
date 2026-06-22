# Claude Code Web Search 全链路复现指南

本文档以两个真实案例（F1 车手积分查询、蔚来 ES9 参数查询）为线索，详细拆解从用户输入 prompt 到返回最终回答的完整处理过程，帮助你复现同等能力的搜索管线。

---

## 目录

1. [架构概览](#1-架构概览)
2. [阶段一：Prompt 接收与意图识别](#2-阶段一prompt-接收与意图识别)
3. [阶段二：搜索必要性判决](#3-阶段二搜索必要性判决)
4. [阶段三：搜索词构造](#4-阶段三搜索词构造)
5. [阶段四：执行 WebSearch](#5-阶段四执行-websearch)
6. [阶段五：搜索结果评估](#6-阶段五搜索结果评估)
7. [阶段六：深度获取判断](#7-阶段六深度获取判断)
8. [阶段七：信息综合与回答生成](#8-阶段七信息综合与回答生成)
9. [关键决策树总览](#9-关键决策树总览)
10. [复现建议](#10-复现建议)

---

## 1. 架构概览

```
用户 Prompt
    │
    ▼
┌─────────────────────────────┐
│ 阶段一：意图识别             │  解析问题类型、提取实体、判断语言
├─────────────────────────────┤
│ 阶段二：搜索必要性判决        │  知识边界自检 → 决定是否搜索
├─────────────────────────────┤
│ 阶段三：搜索词构造            │  关键词提取 + 语言选择 + 时间补全
├─────────────────────────────┤
│ 阶段四：执行 WebSearch       │  调用搜索 API，获取标题+URL 列表
├─────────────────────────────┤
│ 阶段五：搜索结果评估          │  信源分级 + 时效过滤 + 交叉验证
├─────────────────────────────┤
│ 阶段六：深度获取判断          │  决定是否需要 WebFetch 读取全文
├─────────────────────────────┤
│ 阶段七：信息综合与回答生成    │  数据整合 + 矛盾处理 + 格式化输出
└─────────────────────────────┘
    │
    ▼
最终回答（含引用来源）
```

---

## 2. 阶段一：Prompt 接收与意图识别

### 2.1 输入

来自用户或系统的原始 prompt，可能携带附加上下文：

```
用户 prompt: "搜索今年f1车手谁的积分最多"

系统上下文（隐性）:
  - 当前日期：2026/06/16
  - 会话语言：中文
  - 之前对话历史：无相关前置
```

### 2.2 处理步骤

#### 步骤 1：实体提取

从 prompt 中识别关键实体：

| 实体类型 | 提取结果 | 方法 |
|----------|----------|------|
| 领域 | 赛车 / F1 | 模式匹配 + 语义理解 |
| 时间 | "今年" → 2026 | 锚定系统当前日期 |
| 目标 | 积分最多的人（排名查询） | 问题意图解析 |
| 对象类型 | 车手（driver） | 语义角色标注 |

#### 步骤 2：问题类型分类

将 prompt 归入一个或多个查询类型：

```
查询类型分类：
  ├── 实时信息    ← "今年" 触发
  ├── 排名/比较   ← "谁的...最多" 触发
  ├── 数字/事实   ← 积分是数量指标
  └── 中文优先    ← prompt 语言
```

#### 步骤 3：先验知识自检

在决定搜索前，先检查自己训练数据中是否有答案：

```
内部检查：
  - F1 2026 赛季数据 → 训练截止日期之后的数据 → 不可用
  - F1 积分规则 → 已知，但不需要
  - 当前积分榜 → 实时数据 → 必须搜索

结论：训练数据不覆盖，必须搜索
```

---

## 3. 阶段二：搜索必要性判决

### 3.1 决策矩阵

每个 prompt 都经过以下决策矩阵：

```
prompt 是否包含以下任一特征？
  ├── 实时/近期事件（今天、本周、今年）           → 是 → 搜索
  ├── 需要精确数字且确认训练数据没有               → 是 → 搜索
  ├── 涉及 2024 年之后的厂商/产品发布              → 是 → 搜索
  ├── 地点相关实时信息（天气、交通、营业时间）      → 是 → 搜索
  ├── 自己不确定或记忆中无此信息                    → 是 → 搜索
  └── 以上皆否 → 不搜索，直接基于训练数据回答
```

### 3.2 两个案例的判决

```
案例 1: "今年f1车手谁的积分最多"
  特征匹配: ✅ 实时事件（今年） + ✅ 数字排名 + ✅ 训练数据外
  判决: 搜索

案例 2: "2026蔚来es9的尺寸，重量，价格？"
  特征匹配: ✅ 2026 新品 + ✅ 精确数字（尺寸/价格） + ✅ 训练数据外
  判决: 搜索

案例 3（假设）: "Python 怎么读取 CSV 文件？"
  特征匹配: 全部否
  判决: 不搜索，直接回答
```

### 3.3 实现要点

```python
# 伪代码：搜索必要性判断
def should_search(prompt: str, current_date: str, model_knowledge_cutoff: str) -> bool:
    # 规则 1：时间锚定
    time_keywords = ["今天", "今日", "今年", "本周", "最新", "最近", "刚刚"]
    if any(kw in prompt for kw in time_keywords):
        return True

    # 规则 2：未来/超近期年份
    years = extract_years(prompt)
    if any(y > model_knowledge_cutoff_year for y in years):
        return True

    # 规则 3：价格/参数等易变数据
    volatile_patterns = ["价格", "售价", "多少钱", "股价", "汇率", "天气", "排名", "积分"]
    if any(p in prompt for p in volatile_patterns):
        return True

    # 规则 4：模型的自我不确定性（LLM 内部评估）
    if model_self_uncertainty(prompt) > THRESHOLD:
        return True

    return False
```

---

## 4. 阶段三：搜索词构造

### 4.1 构造原则

| 原则 | 说明 | 示例 |
|------|------|------|
| **关键词优先** | 提取名词和专有名词，去掉虚词 | "f1 driver standings" 而非 "who has the most points" |
| **时间补全** | "今年" → 显式年份 "2026" | "2026 F1" |
| **语言匹配** | 英文信息源用英文搜，中文信息源用中文搜 | ES9 → 中文搜；F1 → 英文搜 |
| **具体化** | 模糊词替换为精确词 | "积分" → "standings points" |

### 4.2 两个案例的搜索词生成

```
案例 1: "今年f1车手谁的积分最多"
  实体: F1, driver, standings, points
  时间: 今年 → 2026
  语言决策: F1 赛事的优质信息源以英文为主
  搜索词: "2026 F1 driver standings points leader"

案例 2: "2026蔚来es9的尺寸，重量，价格？"
  实体: 蔚来 ES9, 尺寸, 重量, 价格
  时间: 2026（已显式给出）
  语言决策: 蔚来是中国品牌，中文源最权威
  搜索词: "2026 蔚来 ES9 尺寸 重量 价格"
```

### 4.3 搜索词生成流程

```python
def build_search_query(prompt: str, entities: dict, current_date: str) -> tuple[str, str]:
    """
    返回 (search_query, language_choice)
    """
    # 1. 确定搜索语言
    if is_chinese_brand(entities) or prompt_is_chinese(prompt):
        language = "zh"
    elif info_sources_primarily_english(entities):
        language = "en"
    else:
        language = detect_user_language(prompt)

    # 2. 补全时间
    year = extract_or_resolve_year(prompt, current_date)

    # 3. 提取关键词
    keywords = extract_key_nouns(prompt)  # e.g., ["F1", "driver", "standings", "points"]

    # 4. 组装查询
    # 去掉问句结构，保留关键词序列
    query = f"{year} {' '.join(keywords)}" if language == "en" else f"{year} {' '.join(keywords)}"

    return query, language
```

---

## 5. 阶段四：执行 WebSearch

### 5.1 调用链路

```
Claude Code CLI
    │
    │  将 query 打包为 server-side tool call
    │  schema: { type: "web_search_20250305", query: "2026 F1 driver standings..." }
    │
    ▼
Anthropic Messages API (api.anthropic.com/v1/messages)
    │
    │  服务端执行搜索
    │  使用 Anthropic 自己的搜索管线（具体搜索引擎未公开）
    │
    ▼
返回 web_search_tool_result:
    [
      { "title": "...", "url": "https://...", "page_age": "...", "encrypted_content": "..." },
      ... 最多 10 条
    ]
    │
    │  CLI 提取 title + url，丢弃 page_age 和 encrypted_content
    │
    ▼
我拿到精简后的结果列表（只有标题 + URL）
```

### 5.2 工具参数

```
WebSearch 输入:
  - query:         string (必填, ≥2 字符)
  - allowed_domains: string[] (可选, 白名单)
  - blocked_domains:  string[] (可选, 黑名单)
  - 白名单和黑名单互斥

WebSearch 输出（我看到的）:
  - url:   链接地址
  - title: 页面标题

限制:
  - 每次最多 10 条结果
  - 本地 15 分钟 TTL 缓存
```

---

## 6. 阶段五：搜索结果评估 ← 核心去噪环节

### 6.1 三层评估框架

拿到标题+URL 列表后，我对每条结果进行三层评估：

```
层级 1: 来源可信度
层级 2: 时效性
层级 3: 交叉一致性
```

### 6.2 层级 1：来源可信度分级

```python
# 伪代码：可信度评分
def score_credibility(url: str, title: str) -> float:
    domain = extract_domain(url)

    # 官方/一手来源
    if domain in OFFICIAL_SOURCES:     # 厂商官网、工信部备案、官方发布会
        return 0.95

    # 垂直行业媒体
    if domain in VERTICAL_MEDIA:       # 汽车之家、58che、Motorsport、Crash.net
        return 0.85

    # 综合科技媒体
    if domain in TECH_MEDIA:           # ZOL、SI.com、Sporting News
        return 0.70

    # 聚合/转载平台
    if domain in AGGREGATORS:          # Yahoo Sports、DIRECTV Insider
        return 0.50

    # 博客/自媒体
    if is_personal_blog(domain) or is_self_media(domain):
        return 0.20

    # 未知来源
    return 0.30
```

**F1 案例中的实际判断：**

| 搜索结果 | 来源类型 | 可信度 |
|----------|----------|--------|
| si.com/onsi/f1/... | 综合体育媒体 | 中高 |
| motorsport.com/... | 垂直赛车媒体 | 高 |
| sportingnews.com/... | 体育聚合 | 中 |
| crash.net/f1/... | 垂直赛车媒体 | 高 |
| directv.com/insider/... | 聚合/转载 | 中低 |

→ 有多个垂直媒体 + 综合体育媒体 → 总体可信

**ES9 案例中的实际判断：**

| 搜索结果 | 来源类型 | 可信度 |
|----------|----------|--------|
| 58che.com/param.shtml | 汽车参数平台（垂直） | 高 |
| auto.zol.com.cn/... | 科技媒体发布页 | 中高 |
| news18a.com/... | 汽车新闻聚合 | 中 |
| edgen.tech/... | 科技媒体 | 中 |

→ 有垂直汽车平台 + 科技媒体 → 总体可信

### 6.3 层级 2：时效性过滤

```python
def check_timeliness(url: str, title: str, expected_year: int) -> bool:
    """
    检查结果是否在时效窗口内。
    WebSearch 返回的结果没有显式日期字段（CLI 丢弃了 page_age），
    所以只能从 URL/标题推断。
    """
    # 方法 1：URL 中包含年份
    year_in_url = extract_year_from_url(url)
    if year_in_url and year_in_url < expected_year:
        return False  # 过期数据，丢弃

    # 方法 2：标题中包含年份
    year_in_title = extract_year_from_title(title)
    if year_in_title and year_in_title < expected_year:
        return False

    # 方法 3：标题中包含"历史"、"回顾"、"archive"等词
    if any(kw in title.lower() for kw in ["历史", "回顾", "archive", "历年"]):
        return False  # 历史数据，非当前信息

    # 方法 4：关键事件时间锚定
    # 例如 F1 标题中提及具体大奖赛名称，可以推断赛季
    if "2025" in title and "2026" not in title:
        return False

    return True
```

**F1 案例实际过滤：**

搜索结果中出现了一个 2025 赛季相关的链接：
```
标题: "F1 standings 2025: updated driver..." 
→ 时效性检查失败 → 过滤掉，不采用
```

其余结果都明确是 2026 赛季内容 → 保留。

### 6.4 层级 3：交叉一致性验证（最关键）

这是**确定最终答案准确性**的核心环节。原理：

```
如果多个独立来源报告相同的事实，则该事实可信度大幅提升。
如果来源之间存在矛盾，需要追查原因或降低置信度。
```

**F1 案例的交叉验证：**

```
来源 A（Motorsport）：Antonelli 领先，Hamilton 第2
来源 B（SI.com）：Antonelli 第一，156分
来源 C（Crash.net）：Antonelli 领先
来源 D（SportingNews）：Antonelli 第一

→ 四个独立来源一致确认 Antonelli 领先 → 高置信度
→ 积分数字（156）有多源交叉验证 → 高置信度
```

**ES9 案例的交叉验证与矛盾处理：**

```
尺寸：
  源 A（58che）：5365 × 2029 × 1870mm，轴距 3250mm
  源 B（ZOL）：5365 × 2029 × 1870mm，轴距 3250mm
  → 完全一致 → 高置信度

价格：
  源 A（58che）：49.80万起
  源 B（News18a）：49.80万起
  源 C（Edgen）：49.80万起（较预售价低5.7%）
  → 三个源一致 → 高置信度

  但 ZOL 标题写"39万起"

矛盾！→ 追查：
  - 点进 ZOL 文章标题 → 正文写明 39万是"电池租用价格"
  - 49.80万是"整车购买价格"
  - 两个口径都对，不是数据错误

处理方式：
  → 两个价格都呈现，区分整车价和 BaaS 方案价
```

```python
def cross_validate(facts: dict[str, list[tuple[str, float]]]) -> dict:
    """
    facts = {
        "尺寸": [("5365×2029×1870", 0.85), ("5365×2029×1870", 0.70), ...],
        "价格": [("49.80万", 0.85), ("39万", 0.70), ...],
        ...
    }
    返回: { field: (value, confidence, notes) }
    """
    results = {}
    for field, value_sources in facts.items():
        # 去重并统计每个值被多少来源支持
        value_counts = count_by_value(value_sources)

        if len(value_counts) == 1:
            # 所有来源一致 → 高置信度
            value = list(value_counts.keys())[0]
            results[field] = (value, "high", "")
        else:
            # 存在分歧 → 追查原因
            # 检查是否不同口径（如整车价 vs 租用价）
            if is_different_measurement(field, value_counts):
                results[field] = (value_counts, "high_with_notes",
                                  "不同口径，均正确")
            else:
                # 真正矛盾 → 降低置信度，采用多数来源
                majority = max(value_counts, key=value_counts.get)
                results[field] = (majority, "medium",
                                  f"存在分歧: {value_counts}")

    return results
```

---

## 7. 阶段六：深度获取判断

搜索结果摘要不足以回答时，决定是否需要 **WebFetch** 读取全文。

### 7.1 决策逻辑

```python
def need_webfetch(search_results: list, info_needed: list) -> bool:
    """
    仅凭标题+URL 是否已足够回答问题？
    """
    # 如果搜索结果标题中已包含完整的答案数据 → 不需要
    # 如果标题只暗示有答案但未显示具体数字 → 需要
    # 如果多源结果完全一致 → 可能不需要
    # 如果不同源存在矛盾 → 需要深入阅读

    for info in info_needed:
        found_in_titles = any(info in r.title for r in search_results)
        if not found_in_titles:
            # 标题没有具体数据，需要读正文
            return True

    return False
```

### 7.2 两个案例的判断

```
F1 案例：
  标题含 "Antonelli leads"，"156 points" 等具体信息
  → 多个结果的标题已有足够数据
  → 不需要 WebFetch
  
  （如果你问的是"Antonelli 在摩纳哥站每一圈的具体时间"，
    那标题不可能包含 → 需要 WebFetch）

ES9 案例：
  58che 标题/URL 明确是参数页 → 暗示有完整参数表
  但标题本身不含具体数字
  
  实际处理中：由于多条结果的标题摘要已经包含了关键数字
  （价格在标题、尺寸在标题），我判定不需要额外 WebFetch。
  
  但如果只有 1 条结果，或者多条结果矛盾，
  我会对具体页面（如 58che param.shtml）执行 WebFetch。
```

---

## 8. 阶段七：信息综合与回答生成

### 8.1 数据整合

```python
# 伪代码：信息整合
def synthesize(final_facts: dict, sources: list) -> str:
    """
    将验证后的事实整合为最终回答
    """
    answer_parts = []

    # 1. 核心答案先行
    answer_parts.append(f"**{main_answer}** （{confidence_note}）")
    answer_parts.append("")  # 空行

    # 2. 结构化呈现（表格优先于段落）
    if is_tabular_data(final_facts):
        answer_parts.append(render_table(final_facts))
    else:
        answer_parts.append(render_paragraphs(final_facts))

    # 3. 补充上下文（如有必要）
    if has_notable_context():
        answer_parts.append(f"\n关键信息：{context}")

    # 4. 来源标注
    answer_parts.append("\nSources:")
    for s in sources[:5]:  # 最多 5 个
        answer_parts.append(f"- [{s.title}]({s.url})")

    return "\n".join(answer_parts)
```

### 8.2 输出格式选择

```
数据类型            → 输出格式
─────────────────────────────
排名/比较           → 表格（排名 | 车手 | 车队 | 积分）
技术参数            → 表格（项目 | 数据）
时间线/事件         → 时间线或分节段落
关系/因果           → 段落叙述
价格（多版本）       → 分表（整车价 | BaaS 价）
```

### 8.3 两个案例的输出策略

```
F1案例：
  数据类型: 排名 → 表格 + 关键亮点（Antonelli 退赛、Hamilton 追分）
  URL 数量: ~10 个来源交叉验证后整合为一张统一排名表
  
ES9案例：
  数据类型: 参数 → 分表呈现（尺寸/重量/价格/动力）
  矛盾处理: 整车价和 BaaS 价分成两个子表，标注口径差异
  额外信息: 补充动力参数作为上下文（用户问"车"时通常也关心性能）
```

---

## 9. 关键决策树总览

```
┌─────────────────────────────────────────────────────────────────┐
│                    输入：用户 Prompt                              │
└─────────────────────────────────────────────────────────────────┘
                              │
                              ▼
                   ┌─────────────────┐
                   │ 是否需要搜索？    │
                   └──────┬──────────┘
                          │
              ┌───────────┴───────────┐
              │ 否                     │ 是
              ▼                        ▼
       直接回答              ┌─────────────────┐
                            │ 搜索词如何构造？   │
                            │ - 语言选择        │
                            │ - 关键词提取      │
                            │ - 时间补全        │
                            └──────┬──────────┘
                                   │
                                   ▼
                            ┌─────────────────┐
                            │ 执行 WebSearch   │
                            │ 获得标题+URL列表  │
                            └──────┬──────────┘
                                   │
                                   ▼
                     ┌─────────────────────┐
                     │ 层级1：信源可信度     │
                     │ 层级2：时效性过滤     │
                     │ 层级3：交叉一致性     │
                     └──────┬──────────────┘
                            │
                  ┌─────────┴─────────┐
                  │ 矛盾？              │
                  │   是 → 追查原因     │
                  │   否 → 通过        │
                  └─────────┬─────────┘
                            │
                            ▼
                   ┌─────────────────┐
                   │ 需要 WebFetch？  │
                   └──────┬──────────┘
                           │
               ┌───────────┴───────────┐
               │ 是                     │ 否
               ▼                        ▼
          WebFetch 读取           ┌─────────────────┐
          页面全文               │ 综合信息生成回答   │
               │                 │ - 数据整合        │
               ▼                 │ - 格式选择        │
          提取关键信息            │ - 来源标注        │
               │                 └─────────────────┘
               ▼                        │
          加入原有信息池 ←───────────────┘
               │
               ▼
         最终回答
```

---

## 10. 复现建议

如果你想自己实现这套能力，以下是核心组件和推荐方案：

### 10.1 最小可行管线

```
组件                    推荐实现
──────────────────────────────────────────
搜索引擎 API            Brave Search API / SerpAPI / Tavily
信源分级                手动维护域名白名单（分三级即可）
时效过滤                基于 URL/标题的年份正则 + page_age
交叉验证                多数投票（majority voting）+ 矛盾标记
全文获取                requests + BeautifulSoup + readability-lxml
输出格式化              LLM 将结构化数据渲染为 Markdown
```

### 10.2 信源分级的最小规则集

至少区分三级：

```
Tier 1（默认采信，单源即可）：
  厂商官网、政府备案、官方发布会通稿、交易所公告

Tier 2（需要 2+ 独立源确认）：
  垂直行业媒体、知名科技/体育媒体、大型门户

Tier 3（仅作为 Tier1/2 的补充佐证，不独立采信）：
  聚合站、论坛、自媒体、个人博客、未知域名
```

### 10.3 关键实现注意事项

1. **不要只依赖一个源的标题作结论** — 跨 2-3 个独立源验证后再确认
2. **矛盾比一致更重要** — 发现矛盾时追查原因，往往能发现信息的关键 nuance（比如 ES9 的整车价 vs BaaS 价）
3. **区分事实和观点** — 只采信事实性信息（数字、日期、排名），过滤掉评价性内容（"很好"、"第一梯队"）
4. **时效窗口要按领域区分** — 天气预报小时级、股价分钟级、汽车参数月级、历史知识永远有效
5. **不确认的信息透明呈现** — 宁可标明"未经独立验证"也不虚构
6. **搜索词的质量决定了结果的上限** — 好的关键词提取比复杂的后处理更重要

### 10.4 进阶方向

- **对抗验证**：给每个声明配置反驳者角色，主动寻找推翻声明的证据
- **来源经验记忆**：跨会话记住各域名的可靠性，持续更新信源分级
- **多轮搜索**：第一轮搜索结果中发现新关键词时，自动发起第二轮补充搜索
- **代码过滤**：对于涉及数字计算的搜索结果，用代码执行而非纯推理来验证
