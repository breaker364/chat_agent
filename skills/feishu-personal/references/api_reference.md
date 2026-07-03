# 飞书 API 接口参数参考

> 逆向工程得到的接口文档，标注每个参数的已知/未知状态。
>
> - ✅ 已知：含义明确
> - ⚠️ 部分已知：能用但不完全理解
> - ❓ 未知：硬编码的魔法值，不清楚含义
> - 🔧 可选：非必需参数

---

## 目录

1. [通用协议](#通用协议)
2. [im/gateway 简单接口](#imgw-简单接口)
   - cmd 84 - 部门列表
   - cmd 83 - 子部门
   - cmd 80 - 部门成员
   - cmd 5031 - 用户详情（单个）
   - cmd 5032 - 用户详情（批量）
   - cmd 5030 - P2P 会话查找
   - cmd 58 - 消息位置映射
   - cmd 8 - 消息详情
   - cmd 11013 - 资源消息
3. [im/gateway 复杂接口](#imgw-复杂接口)
   - cmd 1000 - Feed Box（会话列表）
   - cmd 11021 - 搜索（通用）
   - cmd 11021 - 搜索（消息，含时间/过滤）
   - cmd 11021 - 搜索（群组，排序/过滤）
4. [REST API - 云文档](#rest-api---云文档)
   - client_vars - 文档内容
   - get_node - Wiki 解析
   - meta - 文档元数据
5. [REST API - 多维表格](#rest-api---多维表格)
   - clientvars - 表格列表
   - tablesv3 - 字段定义
   - records - 记录数据
6. [REST API - 内嵌表格](#rest-api---内嵌表格)
   - sheet client_vars - Sheet 数据
7. [Protobuf 编码注意事项](#protobuf-编码注意事项)
8. [响应字段速查](#响应字段速查)

---

## 通用协议

| 项目 | 值 |
|------|-----|
| Endpoint | `POST https://internal-api-lark-api.feishu.cn/im/gateway/` |
| Content-Type | `application/x-protobuf` |
| 认证 | session cookie (`.feishu.cn`), 存于 `session_cookies.json` |
| 请求体 | `improto.Packet` protobuf |
| 响应体 | `improto.Packet` protobuf |

### improto.Packet 结构

| 字段 | id | 类型 | 说明 |
|------|-----|------|------|
| sid | 1 | int64 | ✅ Session ID |
| payload_type | 2 | int32 | ✅ 固定为 1 |
| cmd | 3 | int32 | ✅ 命令号 |
| status | 4 | int32 | ✅ 响应状态，0=成功 |
| payload | 5 | bytes | ✅ 业务数据（嵌套 protobuf） |
| cid | 6 | string | ✅ Correlation ID |

---

## im/gw 简单接口

以下接口参数完全已知，没有未知字段。

### cmd 84 - 部门列表 (PULL_UNFOLD_DEPARTMENT_STRUCTURE)

**用途**: 获取顶层部门列表

**请求 payload**:
```
{ 1: '0' }
```

| 字段 | 值 | wire type | 说明 |
|------|-----|-----------|------|
| f1 | `'0'` | string | ✅ 部门ID，`'0'` = 顶层 |

**响应**: 部门列表，详见[响应字段速查](#dept-响应)

---

### cmd 83 - 子部门 (PULL_SUBORDINATE_DEPARTMENTS)

**用途**: 获取指定部门的子部门

**请求 payload**:
```
{ 1: deptId }
```

| 字段 | 值 | wire type | 说明 |
|------|-----|-----------|------|
| f1 | deptId | string | ✅ 父部门 ID |

---

### cmd 80 - 部门成员 (PULL_DEPARTMENT_STRUCTURE)

**用途**: 获取部门下的成员列表

**请求 payload**:
```
{ 1: deptId }
```

| 字段 | 值 | wire type | 说明 |
|------|-----|-----------|------|
| f1 | deptId | string | ✅ 部门 ID |

**响应结构**:
```
f1.f1 = 成员列表（叶子部门）
f1.f2 = 当前部门信息
f1.f4 = offset（分页）
f1.f5 = 子部门列表（非叶子部门）
```

---

### cmd 5031 - 用户详情 (PULL_USER_PROFILE_V2)

**用途**: 查询单个用户信息（也用于批量解析用户名）

**请求 payload**:
```
{ 1: 1, 2: userId }
```

| 字段 | 值 | wire type | 说明 |
|------|-----|-----------|------|
| f1 | 1 | varint | ✅ scene 枚举（1=BY_USER_ID, 2=BY_CONTACT_TOKEN） |
| f2 | userId | string | ✅ 用户 ID |

> ⚠️ **踩坑**: `{1: userId}` 不含 scene 参数会返回 null payload！必须用 `{1: 1, 2: userId}`。

**其他已知请求字段**:
```
f3 = contactToken  // scene=2 时使用
f4 = chatId        // 可选，群上下文
```

**响应 payload**:
```
f2: {                            // profile 对象
  f1: userId,                    // string ✅
  f2: "Tiandao DAI 戴天道",       // string ✅ 用户全名（中英文）
  f14: "Tiandao DAI",            // string ✅ 英文名
  f25: "tiandao.dai@nio.com",    // string ✅ 邮箱
  ...                            // 其他 profile 字段（UI 模板等）
}
```

| 字段路径 | 类型 | 说明 |
|----------|------|------|
| f2.f1 | string | ✅ 用户 ID |
| f2.f2 | string | ✅ 用户全名（中英文，如 "Tiandao DAI 戴天道"） |
| f2.f14 | string | ✅ 英文名（如 "Tiandao DAI"） |
| f2.f25 | string | ✅ 邮箱 |

---

### cmd 5032 - 批量用户 UI Profile (BATCH_PULL_USER_PROFILE_V2)

**用途**: ~~批量查询用户信息~~ 返回 profile card UI 模板（按钮标签多语言翻译），**不含实际用户名**

**请求 payload**:
```
{ 1: [userId1, userId2, ...] }
```

| 字段 | 值 | wire type | 说明 |
|------|-----|-----------|------|
| f1 | [userIds] | repeated string | ✅ 用户 ID 数组 |

**响应 payload**:
```
f1: [                          // repeated, 每个用户一项
  {
    f1: userId,                // string, 用户 ID
    f2: {                      // UI 模板对象（非用户资料！）
      f1: { ... },             // metadata (FROM_ID 等)
      f2: [                    // CTA 按钮数组（发消息/视频通话等）
        { f1: "Lark_Server_Profile_CTA-CHAT", f2: { f1: "Message", f2: [{f1:"zh_cn",f2:"发消息"}, ...] } },
        ...
      ],
      f3: [...]                // 其他 UI 字段定义（部门/邮箱/备注名 标签翻译）
    }
  }
]
```

> ⚠️ **重要**: cmd 5032 返回的是 profile card 的 UI 模板（按钮标签、字段标签的多语言翻译），**不包含用户名、邮箱等实际数据**。获取用户名应使用 cmd 5031（并发调用）。

---

### cmd 5030 - P2P 会话查找 (CHECK_P2P_CHATS_EXIST_BY_USER)

**用途**: 根据 userId 查找与其的 P2P 单聊 chatId

**请求 payload**:
```
{ 1: userId }
```

| 字段 | 值 | wire type | 说明 |
|------|-----|-----------|------|
| f1 | userId | string | ✅ 用户 ID |

**响应**:
```
f1.f2 = '1' 表示存在
f2.f2 = chatId
```

---

### cmd 58 - 消息位置映射 (PULL_MESSAGE_ID_BY_POSITION)

**用途**: 根据位置范围获取 position → messageId 的映射

**请求 payload**:
```
{ 1: chatId, 2: startPosition, 3: count }
```

| 字段 | 值 | wire type | 说明 |
|------|-----|-----------|------|
| f1 | chatId | string | ✅ 会话 ID |
| f2 | startPosition | varint | ✅ 起始位置 |
| f3 | count | varint | ✅ 获取数量 |

**响应**:
```
f1 = [{f1: position, f2: messageId}, ...]
```

**使用模式**:
- 探测最新位置: `{1: chatId, 2: 999999, 3: 1}` → 返回最近的有效位置
- 若探测失败则用二分搜索找 maxPosition
- 获取到 messageId 后配合 cmd 8 拉取消息内容

---

### cmd 8 - 消息详情 (PULL_MESSAGES_BY_IDS)

**用途**: 根据 messageId 批量拉取消息内容

**请求 payload**:
```
{ 1: [msgId1, msgId2, ...] }
```

| 字段 | 值 | wire type | 说明 |
|------|-----|-----------|------|
| f1 | [messageIds] | repeated string | ✅ 消息 ID 数组 |

**响应**: 消息对象数组，详见[响应字段速查](#message-响应)

---

### cmd 11013 - 资源消息 (PULL_CHAT_RESOURCE_MESSAGES)

**用途**: 获取会话中的资源/附件消息（图片、文件），**不包含普通文本消息**

**请求 payload**:
```
{ 1: chatId }
```

| 字段 | 值 | wire type | 说明 |
|------|-----|-----------|------|
| f1 | chatId | string | ✅ 会话 ID |

> ⚠️ 注意: 此接口只返回资源消息。获取完整聊天记录应使用 cmd 58 + cmd 8 两步法。

---

## im/gw 复杂接口

以下接口包含未知/部分已知的参数。

### cmd 1000 - Feed Box（会话列表）

**用途**: 获取最近活跃的会话列表（chatId），主要作为群组搜索排序的种子数据

**请求 payload**:
```
{ 1: 1, 2: 1, 3: 0, 4: count, 5: 0, 7: 1, 10: 1, 11: 0 }
```
> ⚠️ 注意: 所有字段均为 **varint** 编码（整数），不能用 string。payload 直接 encodeMessage，不需要 `{1: ...}` 外层包装。

| 字段 | 值 | wire type | 说明 |
|------|-----|-----------|------|
| f1 | `1` | varint | ✅ 主标签：1=消息，2=次标签 |
| f2 | `1` | varint | ✅ 方向：1=向前（从旧到新） |
| f3 | `0` | varint | ✅ 起始位置 |
| f4 | count | varint | ✅ 页大小（通常 20~50） |
| f5 | `0` | varint | ❓ 未知，必须设置为 0 |
| f7 | `1` | varint | ❓ 未知，必须设置为 1 |
| f10 | `1` | varint | ❓ 未知，必须设置为 1 |
| f11 | `0` | varint | ❓ 未知，必须设置为 0 |

**响应**:
```
f3 = [{f1: chatId, f2: type(?), f3: updateTimestamp(?)}, ...]
```

**未知总结**: f5/f7/f10/f11 四个字段是从 Playwright 抓包复制的，偏离这些值会导致返回空数据或异常。

---

### cmd 11021 - 搜索（通用）

**用途**: 统一搜索接口，支持联系人/消息/文档/群组/应用

**请求 payload**: `encodeMessage({ 1: searchRequest })`（外层包装 f1）

#### searchRequest 结构

```js
{
  1: sessionId,       // 'cli_xxx'
  2: seqId,           // 翻页递增: 1, 2, 3...
  3: query,           // 搜索关键词
  4: paginationToken, // 翻页token（来自上页响应 f1.f5）
  5: searchConfig,    // 搜索配置（见下）
  6: 'zh_CN',         // locale
  15: 2,              // ?
  16: 'Asia/Shanghai', // timezone
  18: '1',            // ?
}
```

| 字段 | 值 | wire type | 说明 |
|------|-----|-----------|------|
| f1 | sessionId | string | ✅ 会话标识，同一次搜索翻页保持不变 |
| f2 | seqId | varint | ✅ 页码序号，从 1 开始递增 |
| f3 | query | string | ✅ 搜索关键词，空搜索用 `' '`（空格） |
| f4 | paginationToken | string | ✅ 翻页 token，首页不传。值来自上页响应的 `f1.f5`（GeoStrategyInfos JSON 字符串原样传回） |
| f5 | searchConfig | message | ✅ 搜索配置对象（见下） |
| f6 | `'zh_CN'` | string | ✅ 语言/地区 |
| f15 | `2` | varint | ❓ 疑似"全量搜索模式"，不设置可能只返回部分结果 |
| f16 | `'Asia/Shanghai'` | string | ✅ 时区 |
| f18 | `'1'` | string | ❓ 未知，所有搜索都带此字段 |

#### searchConfig 结构

```js
{
  1: tagName,       // 搜索类型标识
  2: entityItems,   // 实体类型列表
  6: searchHint,    // 搜索提示/过滤器
}
```

| 字段 | 值 | wire type | 说明 |
|------|-----|-----------|------|
| f1 | tagName | string | ✅ 搜索类型标识符（见 tagName 表） |
| f2 | entityItems | repeated message | ✅ 实体类型列表（见 entity 表） |
| f6 | searchHint | message | ✅ 搜索提示，包含 query 和过滤器 |

#### searchHint 结构

```js
{
  1: query,       // 搜索关键词（同 searchRequest.f3）
  2: seqId - 1,   // 页偏移量（0-based）
  3: filters,     // 可选的过滤器对象
}
```

| 字段 | 值 | wire type | 说明 |
|------|-----|-----------|------|
| f1 | query | string | ✅ 搜索关键词 |
| f2 | offset | varint | ✅ 页偏移量 = seqId - 1 |
| f3 | filters | message | 🔧 可选，过滤器（见下方消息搜索） |

#### tagName 对照表

| --type | tagName | entityItems |
|--------|---------|-------------|
| contacts | `SEARCH_CHATTERS_IN_ADVANCE_SCENE` | `[{1:1}, {1:31}, {1:24}]` |
| messages | `SEARCH_MESSAGES` | `[{1:5}, {1:24}]` |
| docs | `SEARCH_DOC` | `[{1:7}, {1:8}, {1:24}]` |
| groups | `SEARCH_CHATS_IN_ADVANCE_SCENE` | `[{1:3}, {1:24}]` |
| apps | `SEARCH_OPEN_APP_SCENE` | `[{1:9}, {1:2}, {1:24}]` |
| smart | `SMART_SEARCH` | `[{1:1},{1:3},{1:4},{1:27},{1:9},{1:2},{1:10},{1:22},{1:31},{1:24}]` |

#### Entity 类型编号

| 编号 | 含义 | 状态 |
|------|------|------|
| 1 | 联系人 (Chatter) | ✅ |
| 3 | 群组 (Chat) | ✅ |
| 5 | 消息 (Message) | ✅ |
| 7 | 文档 (Doc) | ✅ |
| 8 | 文档 (Docx/新版?) | ⚠️ 可能是新版文档格式 |
| 9 | 应用 (App) | ✅ |
| 2 | ❓ 未知，出现在应用搜索和 smart 搜索 | ❓ |
| 4 | ❓ 未知，出现在 smart 搜索 | ❓ |
| 10 | ❓ 未知，出现在 smart 搜索 | ❓ |
| 22 | ❓ 未知，出现在 smart 搜索 | ❓ |
| 24 | ❓ 未知，**所有搜索类型都包含**，可能是某种通用/兜底类型 | ❓ |
| 27 | ❓ 未知，出现在 smart 搜索 | ❓ |
| 31 | ❓ 未知，出现在联系人搜索 | ❓ |

#### 翻页机制

1. 首页请求不传 f4
2. 响应中 `f1.f5` 是 GeoStrategyInfos JSON 字符串，如 `{"total":100,"HasMore":true,...}`
3. 下一页请求将此 JSON 字符串**原样**设为 `searchRequest.f4`
4. 保持相同 `sessionId`，`seqId` 递增
5. 没有 f4 或不正确的 f4 会导致每页返回相同结果

#### 响应结构

```
f1.f5 = GeoStrategyInfos JSON（翻页token）
f2 = [{搜索结果项}, ...]  — 具体结构因搜索类型不同
```

---

### cmd 11021 - 搜索（消息，含时间/过滤器）

**用途**: 搜索消息，支持时间范围、排除机器人、按会话类型过滤

在通用搜索基础上，消息搜索的 entityItem 和 searchHint 有额外字段。

#### entityItem 扩展（消息类型 f1=5）

当需要过滤时，entity `{1: 5}` 扩展为 `{1: 5, 2: {3: msgFilter}}`：

```js
// msgFilter 结构
{
  1: timeRangeBytes,  // encodeMessage({1: fromUnix, 2: toUnix}) 的 Buffer
  2: senderUserId,    // 发送者过滤（"from me"）
  6: atUserId,        // @提及过滤（"@me"）
  8: 1,               // 排除机器人
  10: 2,              // 会话类型: 2=P2P, 1=群聊
}
```

| 字段 | 值 | wire type | 说明 |
|------|-----|-----------|------|
| f1 | timeRangeBytes | bytes | ✅ 时间范围，嵌套 protobuf `{f1: 开始秒, f2: 结束秒}` |
| f2 | senderUserId | string | ✅ 发送者 userId 过滤，只返回该用户发送的消息 |
| f6 | atUserId | string | ✅ @提及 userId 过滤，只返回 @该用户 的消息 |
| f8 | `1` | varint | ✅ 排除机器人消息（设置=排除，不设置=包含） |
| f10 | `2` 或 `1` | varint | ✅ 会话类型过滤：2=仅P2P单聊，1=仅群聊，不设置=全部 |

#### searchHint.f3 过滤器标志

searchHint 的 f3 用于标记哪些过滤器处于激活状态：

```js
searchHint[3] = {
  11: { 1: '1' },        // 时间过滤激活
  12: { 1: userId },     // 发送者过滤提示（"from me"）
  15: { 1: '2' },        // 机器人过滤激活
  15: { 1: '1' },        // @me 过滤激活
  16: { 1: '2' },        // P2P 过滤
  16: { 1: '1' },        // 群聊过滤
}
```

| 字段 | 值 | 说明 |
|------|-----|------|
| f11.f1 | `'1'` | ⚠️ 时间过滤激活标志。值 `'1'` 的含义不完全清楚，可能是 boolean |
| f12.f1 | userId | ✅ 发送者过滤提示。值为当前用户的 userId，标记"我发送的"过滤器激活 |
| f15.f1 | `'2'` | ⚠️ 机器人过滤标志。为什么是 `'2'` 而不是 `'1'`？ |
| f15.f1 | `'1'` | ✅ @me 过滤激活标志。值 `'1'` 标记"@我的"过滤器激活 |
| f16.f1 | `'2'` | ⚠️ P2P 过滤。`'2'` = P2P |
| f16.f1 | `'1'` | ⚠️ 群聊过滤。`'1'` = 群聊 |

> 注意: f11/f12/f15/f16 的值是 **string** 类型（不是 varint），这与 msgFilter 中的 varint 不同。

---

### cmd 11021 - 搜索（群组，排序/过滤）

**用途**: 搜索群组，支持按最近消息更新排序、私有群过滤。用于 `chat today` 命令发现活跃群组。

这是整个系统中**最复杂的请求**，包含大量未知字段。

#### 完整 searchRequest

```js
{
  1: sessionId,
  2: seqId,
  3: ' ',                    // 空格=空搜索
  4: paginationToken,        // 翻页（可选）
  5: {                       // searchConfig
    1: 'SEARCH_CHATS_IN_ADVANCE_SCENE',
    2: entityItems,          // 见下
    3: { 1: 1, 8: 1, 9: 1 },  // ❓
    4: { 1: 6 },               // ❓
    6: { 1: ' ', 2: 4 },       // searchHint 变体
  },
  6: 'zh_CN',
  8: { 6: 1, 7: 1, 10: 0, 12: 1, 13: 1 },  // ❓
  9: { 2: 200 },                              // ❓
  10: { 2: 202 },                             // ❓
  16: 'Asia/Shanghai',
  18: '1',
}
```

#### searchRequest 字段（群搜特有）

| 字段 | 值 | wire type | 说明 |
|------|-----|-----------|------|
| f1~f6 | — | — | ✅ 同通用搜索 |
| **f8** | `{6:1, 7:1, 10:0, 12:1, 13:1}` | message | ❓ **完全未知**。5个子字段全部不明，但去掉会影响排序功能 |
| **f9** | `{2: 200}` | message | ❓ 未知，可能是结果数量限制？ |
| **f10** | `{2: 202}` | message | ❓ 未知，202 与 200 的关系？ |
| f15 | 不设置 | — | 群搜时不带 f15（通用搜索带 f15=2） |
| f16 | `'Asia/Shanghai'` | string | ✅ 时区 |
| f18 | `'1'` | string | ❓ 同通用搜索 |

#### searchConfig 字段（群搜特有）

| 字段 | 值 | wire type | 说明 |
|------|-----|-----------|------|
| f1 | tagName | string | ✅ `'SEARCH_CHATS_IN_ADVANCE_SCENE'` |
| f2 | entityItems | repeated message | ✅ 见下方 chatFilter |
| **f3** | `{1:1, 8:1, 9:1}` | message | ❓ 三个子字段全部未知，通用搜索没有此字段 |
| **f4** | `{1: 6}` | message | ❓ f1=6 含义不明。注意 **f4.f1 必须是 varint**（整数 6），不能用字符串 |
| f6 | `{1: ' ', 2: 4}` | message | ⚠️ searchHint 简化版。f2=4 而非 seqId-1，可能是某种固定偏移？ |

#### chatFilter（核心排序/过滤配置）

entityItem 的结构: `{1: 3, 2: {2: chatFilter}}`

```js
chatFilter = {
  1: { 2: 0 },     // 私有群过滤
  6: 2,             // 排序方式
  7: [chatIds],     // 排序种子
  8: 1,             // ?
  10: 1,            // ?
}
```

| 字段 | 值 | wire type | 说明 |
|------|-----|-----------|------|
| f1 | `{2: 0}` | message | ✅ 私有群过滤。f1.f2=0 = 只返回私有群，不设置 f1 = 返回所有群 |
| **f6** | `2` | **varint** | ⚠️ 排序方式。2=按最近消息更新降序。其他值的含义未知。**必须是 varint，用 string 会静默失效** |
| f7 | [chatIds] | repeated string | ✅ 排序种子，来自 cmd 1000 的最近会话 chatId 列表。是排序生效的**必要条件**，没有种子排序不工作 |
| **f8** | `1` | **varint** | ❓ 未知，但必须设置。**必须是 varint** |
| **f10** | `1` | **varint** | ❓ 未知，但必须设置。**必须是 varint** |

#### 响应中的 lastMsgTime

```
item.f9.f1[] 数组中：
  - f3 === '1' 的项：f1 = 描述文本（如"群消息更新于 "）
  - f3 === '2' 的项：f1 = 秒级时间戳（即 lastMsgTime）
```

#### 未知字段总结

群组搜索排序模式共有 **10个未知/部分未知字段**：
- searchRequest: f8 (5个子字段), f9, f10, f18
- searchConfig: f3 (3个子字段), f4
- chatFilter: f8, f10
- searchHint: f2=4 (为什么是4?)

这些值全部来自 Playwright 对比抓包（有筛选 vs 无筛选的请求 diff），能用但语义不明。

---

## REST API - 云文档

Host: `nio.feishu.cn`（企业租户域名）

### client_vars - 文档内容

**URL**: `GET /space/api/docx/pages/client_vars?id={docxToken}&mode=7&limit=50&cursor={cursor}`

| 参数 | 值 | 说明 |
|------|-----|------|
| id | docxToken | ✅ 文档 token |
| mode | `7` | ❓ 未知魔法值，其他 mode 值的效果未测试 |
| limit | `50` | ✅ 每页 block 数量 |
| cursor | string | 🔧 可选，翻页游标（来自上页响应 `data.cursor`） |

**响应**:
```json
{
  "code": 0,
  "data": {
    "block_map": { "blockId": { "id": "...", "data": { "type": "text", ... } } },
    "block_sequence": ["blockId1", "blockId2", ...],
    "has_more": true,
    "cursor": "xxx"
  }
}
```

**block 类型**: text, heading1-9, image, code, table, ordered, bullet, quote, inline_component (url_preview, @user), whiteboard, sheet

---

### get_node - Wiki Token 解析

**URL**: `GET /space/api/wiki/v2/tree/get_node/?wiki_token={token}&expand_shortcut=true`

| 参数 | 值 | 说明 |
|------|-----|------|
| wiki_token | token | ✅ Wiki token（wiki 链接中的 token） |
| expand_shortcut | `true` | ⚠️ 展开快捷方式，具体行为不完全清楚 |

**用途**: 将 wiki_token 转换为 docxToken (obj_token)，因为 client_vars API 只接受 docxToken。

**响应**: `data.obj_token` = 对应的 docxToken

---

### meta - 文档元数据

**URL**: `GET /space/api/meta/?token={docxToken}&type=22`

| 参数 | 值 | 说明 |
|------|-----|------|
| token | docxToken | ✅ 文档 token |
| type | `22` | ❓ 文档类型编号。22=docx，其他值（如 wiki/sheet/bitable）未测试 |

**响应**: `data.{title, create_time, edit_time, owner_user_name, edit_user_name, url, version}`

---

## REST API - 多维表格

Host: `www.feishu.cn`（公共域名，nio 会重定向到此）

### clientvars - 表格列表

**URL**: `GET /space/api/v1/bitable/{token}/clientvars?tableID=&viewID=&recordLimit=0&ondemandLimit=0&needBase=true&viewLazyLoad=true&ondemandVer=2&openType=1&noMissCS=true&optimizationFlag=1&removeFmlExtra=true`

| 参数 | 值 | 说明 |
|------|-----|------|
| needBase | `true` | ✅ **关键参数**，必须为 true 才能返回表格列表 |
| removeFmlExtra | `true` | ⚠️ 简化公式字段，具体简化了什么不完全清楚 |
| tableID | 空 | ✅ 不指定具体表格，获取全部 |
| viewID | 空 | ✅ 不指定视图 |
| recordLimit | `0` | ⚠️ 记录限制为0，表示不在此接口返回记录 |
| ondemandLimit | `0` | ❓ 未知 |
| viewLazyLoad | `true` | ❓ 视图懒加载？不影响功能 |
| ondemandVer | `2` | ❓ 未知版本号 |
| openType | `1` | ❓ 未知，打开方式？ |
| noMissCS | `true` | ❓ 未知 |
| optimizationFlag | `1` | ❓ 未知优化标志 |

**响应**: `data.base` = base64+gzip → JSON:
```json
{
  "blocks": ["tblXXX", "blkYYY", ...],  // 过滤 tbl* 前缀的即为表格ID
  "blockInfos": { "tblXXX": { "name": "表格名" } }
}
```

---

### tablesv3 - 字段定义

**URL**: `POST /space/api/bitable/{token}/tablesv3/`

**请求体**:
```json
{
  "tableIDList": ["tblXXX"],
  "tablePartitionFlagList": [0],
  "tablePartitionForNoRankFlagList": [],
  "encodingProtocol": { "compression": 1, "serialization": 0 }
}
```

| 字段 | 值 | 说明 |
|------|-----|------|
| tableIDList | [tableIds] | ✅ 要查询的表格 ID 列表 |
| tablePartitionFlagList | [0, 0, ...] | ❓ 每个表一个 0，含义不明（分区标志？） |
| tablePartitionForNoRankFlagList | [] | ❓ 空数组，含义不明 |
| encodingProtocol.compression | `1` | ⚠️ 1=gzip 压缩 |
| encodingProtocol.serialization | `0` | ⚠️ 0=JSON 序列化 |

**响应**: `data[tableId]` = base64+gzip → JSON:
```json
{
  "fieldMap": {
    "fldXXX": { "name": "字段名", "type": 1, "property": { ... } }
  },
  "meta": { "recordsNum": 100, "id": "tblXXX", "rev": 42 }
}
```

---

### records - 记录数据

**URL**: `GET /space/api/v1/bitable/{token}/records?tableId={tableId}&viewLazyLoad=true&offset={offset}&limit={limit}&tableID={tableId}&removeFmlExtra=true`

| 参数 | 值 | 说明 |
|------|-----|------|
| tableId | tableId | ✅ 表格 ID（出现两次，`tableId` 和 `tableID`，冗余但保留） |
| offset | number | ✅ 分页偏移 |
| limit | number | ✅ 每页数量 |
| viewLazyLoad | `true` | ❓ 同 clientvars |
| removeFmlExtra | `true` | ⚠️ 同 clientvars |

**响应**: `data.records` = base64+gzip → JSON:
```json
{
  "tableRecordNum": 100,
  "recordMap": {
    "recXXX": {
      "fldYYY": { "value": [...] }
    }
  }
}
```

---

## REST API - 内嵌表格

Host: `nio.feishu.cn`

### sheet client_vars - Sheet 数据

**用途**: 获取文档中内嵌的 Spreadsheet 数据（非独立多维表格）

**URL**: `POST /space/api/v3/sheet/client_vars`

**请求体**:
```json
{
  "memberId": 0,
  "schemaVersion": 9,
  "openType": 1,
  "token": "spreadsheetToken",
  "sheetRange": { "sheetId": "sheetId" },
  "clientVersion": "v0.0.1"
}
```

| 字段 | 值 | 说明 |
|------|-----|------|
| token | spreadsheetToken | ✅ 表格 token |
| sheetRange.sheetId | sheetId | ✅ 子表 ID |
| memberId | `0` | ❓ 未知，可能是协作者 ID？ |
| schemaVersion | `9` | ❓ 数据格式版本号？其他值的效果未知 |
| openType | `1` | ❓ 同 bitable clientvars 的 openType |
| clientVersion | `'v0.0.1'` | ❓ 伪造的客户端版本号 |

**响应**: `data.snapshot.blocks` = blockId → base64+gzip protobuf cell data

---

## REST API - 画板 (Whiteboard)

Host: `nio.feishu.cn`

画板是 docx 里的 block（`block_type=whiteboard`），有独立的 `blockToken`（25 字符），与 docx token 不同。

### whiteboard/block - 读取画板内容

**用途**: 拉取画板的 meta + 完整节点树

**URL**: `GET /space/api/whiteboard/block?blockToken=<bt>&reqVersion=1&clientVersion=12.4`

**响应**:
```json
{
  "code": 0,
  "data": {
    "meta": {"version": 0, "appliedVersion": 28, "theme": 5, "templateType": "", "createTime": 1779187064},
    "nodes": [ /* tree of shapes/text/syntax containers, each with info{baseV2,textV2,compositeShape,syntaxProps?...}, children?, id */ ]
  }
}
```

### whiteboard/block/create - 新建空画板

**URL**: `POST /space/api/whiteboard/block/create`

**请求体**: `{baseToken: <docx_token>, blockId: <client-gen 27 chars>, baseTokenType: 22, reqVersion: 1}`

**响应**: `{code:0, data: {blockToken: <server-gen 25 chars>}}`

⚠️ 创建后还需调 `POST /space/api/docx/blocks/user_change` 把 block 挂到 docx 的 children 树，否则画板不会出现在 docx 视图里。

### whiteboard/copy + whiteboard/block/clone - 整张复制

**两步**:
```
POST /space/api/whiteboard/copy           body: {blockToken: <source>, reqVersion: 1}
POST /space/api/whiteboard/block/clone    body: {baseToken, blockId, blockToken, mode:1, baseTokenType:22}
                                          resp: {blockToken: <new>}
```

只传 token，不传画板内容。服务端按引用复制全部子节点。

### whiteboard/parse_syntax - 代码 → 节点

**用途**: 把 PlantUML/Mermaid 源码翻译成 nodes JSON，不改画板

**URL**: `POST /space/api/whiteboard/parse_syntax`

**请求体**: `{code, blockToken, syntax: <1=PlantUML | 2=Mermaid>, reqVersion:1, parseType:0}`

**响应**: `{data: {data: "<stringified JSON of {nodes:[...]}>"}}`

### whiteboard/user_change - 提交节点修改

**URL**: `POST /space/api/whiteboard/user_change?member_id=<mid>` （HTTP 与 WS 同端点；交互编辑走 WS，CLI 用 HTTP 即可）

**必传 HTTP header**: `Req-Version: 1`。不带这个 header 服务端返回 `payload is nil [@from@] arg error [@from@] whiteboard` (code 4002000)。

**请求体包络**（已实测可工作；以下层级如果错任意一处，服务端会 panic 5000000）:
```json
{
  "data": {
    "member_id": <int 客户端生成>,
    "user_ticket": "<POST /space/api/pandora_ws/ws_ticket/ {} 拿>",
    "uplink": {
      "payload": {
        "body": {
          "data": {
            "nodeOperations": [<ops>],
            "type": 20,
            "extData": {"actionTriggerSource": 0}
          }
        },
        "meta": {"deviceId": "<member_id>", "userId": "<uid>", "type": 20, "pageId": "<UUID>"}
      },
      "whiteboardToken": "<blockToken>",
      "baseSeq": <meta.appliedVersion>,
      "opId": "<unique>",
      "editTime": <ms>,
      "pkgVersion": 1
    }
  },
  "type": "doc",
  "entities": [{"type":"WHITEBOARD", "token":"<同 whiteboardToken>", "isNewProtocol":true, "resource_type":"structure", "route_key":"<bt>", "route_type":"token"}],
  "version": 2,
  "req_id": <int>,
  "context": {"os":"mac", "app_version":"...", "platform":"web", "request_id":"<uniq>"}
}
```

**结构上的坑**（每条踩过都会 panic）:
- `type / entities / version / req_id / context` 是顶层 `data` 的**兄弟**，**不要嵌进 data**
- `whiteboardToken / baseSeq / opId / editTime / pkgVersion` 是 `uplink` 的直接子级，**不要嵌进 uplink.payload**
- `meta` 是 `uplink.payload` 的直接子级，**不要嵌进 body**

**nodeOperations op 类型**（实测）:

| outer `type` | inner | payload |
|---|---|---|
| 2 | `nodeCreate` | `{path:[...], id, data:<full>, index}` |
| 1 | `nodeUpdate` | `{ids:[], cap:<partial>, type: 0\|1, paths:[{path:[...]}]}` |
| 3 | `nodeDelete` | `{id, path:[...], index}` |

**nodeCreate 额外限制**: `data` 里**不要带 `children`**（即使原节点树是嵌套的）。每个节点要单独发一个 op，子节点通过 `path: [parent_id, ...]` 关联。带 `children` 会 panic。

### PlantUML / Mermaid 渲染的完整流程

`parse_syntax` 只返回**渲染图形**，不包含 syntaxProps 容器。客户端需要自己拼一个：

```json
// 在所有 parse_syntax 返回的节点之前插入这条 op
{
  "type": 2,
  "nodeCreate": {
    "path": [],
    "id": "t1:1",
    "index": 0,
    "data": {
      "baseV2": {"x":<min_x>, "y":<min_y>, "width":<...>, "height":<...>},
      "title": "",
      "theme": {"fillColorCode": 0, "borderStyleCode": 0},
      "extraTextInfo": {"info1": {"themeCapability": {"fillColorCode": 0}}, "info2": {"themeCapability": {"fillColorCode": 0}}},
      "syntaxProps": {
        "sourceCode": "<整段源码>",
        "syntaxType": <1=PlantUML | 2=Mermaid>,
        "diagramType": 0,
        "styleType": 0,
        "type": 31
      }
    }
  }
}
```

然后所有 parse_syntax 返回的节点变成 `t1:1` 的子节点（`path: ["t1:1", ...]`）。

### Overwrite（删旧写新）必须分两个 user_change

实测：单个 user_change 里混合 delete + create 时，如果新节点 id 跟被删的旧节点 prefix 相同（都是 `t1:1 / r1:* / c1:*`），服务端返回 `invalid option [@from@] arg error [@from@] whiteboard` (code 4002000)。

正确做法：
1. 第一次 user_change：只发 delete ops（后序遍历：叶子先、根最后；同层从右往左以保持 index 稳定）
2. 第二次 user_change：只发 create ops

每次 commit 前重新 GET `/whiteboard/block` 拿最新的 `meta.appliedVersion` 作 `baseSeq`。

### whiteboard 渲染图（PNG/JPEG）

**URL**: `GET /space/api/file/f/cdp-whiteboard-<blockToken>~noop/`

返回服务端渲染的 2560×2560 图像，**Content-Type=image/png 但实际字节是 JPEG**（`FF D8 FF E0` JFIF）。客户端按 magic bytes 判扩展名最稳。

### 其他辅助端点

| 端点 | 用途 |
|---|---|
| `GET /whiteboard/room/member?blockToken=X` | 当前协作者列表 |
| `GET /whiteboard/room/whiteboard_record?blockToken=X` | 实时光标 |
| `GET /whiteboard/comment?blockToken=X` | 评论 |
| `GET /whiteboard/resource/meta` | 资源库（图标/贴纸）类型清单 |
| `POST /whiteboard/resource/list` | 按 type+keys 拉资源详情 |
| `GET /whiteboard/node_prefix?blockToken=X` | 节点 id 前缀分配（id 形如 `t1:1`） |
| `GET /whiteboard/user/settings` / `POST` | 用户偏好（工具栏布局/主题/模板列表） |
| `POST /whiteboard/viewport/list` | 视口（缩放/位置） |

---

## Protobuf 编码注意事项

### Wire Type 关键规则

1. **varint vs string 不能混淆**: 群搜 chatFilter 的 f6/f8/f10 **必须是 varint**（整数），用 string 编码会**静默失效**（不报错但功能异常）
2. **genericDecode 的陷阱**: genericDecode 输出的 string 值可能原本是 varint，必须对照原始二进制确认 wire type
3. **二进制字段损坏**: AES 密钥/nonce 等二进制字段会被 genericDecode 误解析为嵌套 proto，需用 `extractRawField()` 手动提取

### 不同 cmd 的 payload 包装差异

| cmd | 包装方式 | 示例 |
|-----|---------|------|
| 11021 | `{1: searchRequest}` | 需要外层 f1 包装 |
| 1000 | 直接编码 | reqPayload 直接 encodeMessage |
| 80/83/84/5030/5031/5032/58/8 | `{1: value}` | 简单值直接包装 |

### 响应状态判断

```js
// protobufjs 的 toObject() 对 int32 默认值 0 返回 undefined
// 因此成功判断需要:
const success = packet && (!packet.status || packet.status === '0');
// 不能用 packet.status === 0（因为可能是 undefined）
```

---

## 响应字段速查

### <a id="dept-响应"></a>Department 字段

```
f1=id, f2=name, f3=parentId, f4=leaderId, f5=memberCount,
f6=status, f7=namePinyin, f8=chatId, f9=hasChild,
f10=i18nName [{f1:locale, f2:name}], f12=memberCountByDisplayRule
```

### Chatter (联系人) 字段

```
f1=userId, f2=name, f3=avatar, f4=status, f5=avatarUrl,
f9=type, f11=description, f14=enUsName, f15=tenantId,
f18=internationalName, f25=email, f34=department[],
f35=enterpriseEmail, f43=displayName
```
> 共 47 个字段，以上为常用字段

### <a id="message-响应"></a>Message 字段

```
f1=messageId, f2=type, f3=senderId, f4=timestamp(秒),
f5.f1=plainText, f5.f2=richContent(HTML),
f5.f2.f1=imageKey(type=5时), f13=position
```

消息类型 (f2):
| 值 | 类型 | Content (f5) 结构 |
|----|------|------|
| 2 | 富文本 | f5.f6.f3.f1[] 内嵌元素（含图片/链接） |
| 3 | 文件 | f5.f1=fileKey, f5.f2=filename, f5.f3=size, f5.f4=mime |
| 4 | 文本（plain） | f5.f1=plainText |
| 5 | 图片 | f5.f2.f1=imageKey, f5.f2.f3.f2=加密三件套 |

> 文件下载详见 `references/api/rest_get_download_messages_files.md`
> （URL: `GET internal-api-lark-file.feishu.cn/download/messages/{messageId}/keys/{fileKey}` — 不加密）

### 图片加密字段（AES-256-GCM）

```
独立图片消息: msg.f5.f2.f3.f2.f1=secretKey(32B), msg.f5.f2.f3.f2.f2=secretNonce(12B)
富文本内嵌图片: msg.f5.f6.f3.f1[].f2.f3.f17.f3.f2.f1/f2
```

> ⚠️ 必须用 extractRawField 提取，genericDecode 会损坏二进制数据

### CoD 枚举（富文本元素属性）

```
doc_type=46, thumbnail_url=47, secret_url=48, secret_type=49,
secret_key=50, secret_nonce=51
```

---

## 未知参数汇总

便于后续逆向研究时快速定位需要搞清楚的字段。

### 高优先级（影响核心功能）

| 接口 | 字段 | 当前值 | 猜测 |
|------|------|--------|------|
| cmd 1000 | f5, f7, f10, f11 | 0, 1, 1, 0 | 可能是过滤/排序标志 |
| cmd 11021 通用 | f15 | 2 | 全量搜索模式？ |
| cmd 11021 通用 | f18 | '1' | 完全不明 |
| cmd 11021 群搜 | searchConfig.f3 | {1:1, 8:1, 9:1} | 完全不明 |
| cmd 11021 群搜 | searchConfig.f4 | {1:6} | 完全不明，f1 必须 varint |
| cmd 11021 群搜 | chatFilter.f8 | 1 | 必须 varint，含义不明 |
| cmd 11021 群搜 | chatFilter.f10 | 1 | 必须 varint，含义不明 |
| cmd 11021 群搜 | searchRequest.f8 | {6:1,7:1,10:0,12:1,13:1} | 5个子字段全不明 |
| cmd 11021 群搜 | searchRequest.f9 | {2:200} | 结果数限制？ |
| cmd 11021 群搜 | searchRequest.f10 | {2:202} | 与 f9 的关系？ |

### 低优先级（不影响功能）

| 接口 | 字段 | 当前值 | 猜测 |
|------|------|--------|------|
| Entity 类型 | 24 | 所有搜索都带 | 通用/兜底类型？ |
| Entity 类型 | 2, 4, 10, 22, 27, 31 | 各搜索类型 | 可能是子类型 |
| doc client_vars | mode | 7 | 编辑模式？阅读模式？ |
| doc meta | type | 22 | docx=22，其他文档类型值？ |
| bitable clientvars | ondemandVer/openType/noMissCS/optimizationFlag | 2/1/true/1 | 优化标志 |
| bitable tablesv3 | tablePartitionFlagList | [0,...] | 分区相关？ |
| sheet client_vars | schemaVersion/openType/memberId/clientVersion | 9/1/0/'v0.0.1' | 客户端伪装 |


### Sheet 图片资源 (f12.f3)

**用途**: 从 sheet cell block 中提取嵌入的图片/附件资源信息，并下载原图。

#### 发现过程

通过逆向工程飞书浏览器客户端查看表格图片时的网络请求，发现：
1. Cell block protobuf 中 `f12.f3` 保存了所有图片资源的元数据列表
2. 图片单元格的 cell-meta 类型为 `f1=7`（附件/图片类型），`f2` 为 1-based 索引
3. 下载使用 `cover` 端点而非 `static-resource` 端点（后者返回 400）
4. 需要 `mount_node_token`（spreadsheet token）和 `mount_point=sheet_image` 上下文参数

#### Cell block protobuf 结构 (f12.f3 图片资源)

```
f12.f3: {                              // 图片资源容器
  f1: [                                // repeated，图片条目数组
    {                                  //   image_entry (index 0)
      f1: "U3Yxb5oMVojZLUxnqI1ch2EWnWT", // 图片 token (用于下载)
      f2: { f1: 918.0, f2: 595.0 },     // 原始尺寸 (width, height, double)
      f3: "..."                          // 可能的 URL 或元数据
    },
    { ... },                            // image_entry (index 1)
    ...
  ]
}
```

#### Cell meta 类型扩展

| f1 值 | 类型 | f2 含义 | f3 含义 |
|-------|------|---------|---------|
| 2 | 数字 | doubles_pool 索引 | — |
| 3 | 文本 | text_entries 索引 | — |
| 4 | 公式 | 依赖池索引 | — |
| 6 | 富文本 | rich_entries 索引 | — |
| **7** | **附件/图片** | **f12.f3 中的 1-based 索引** | **f6=5 或其他标志** |

#### Sheet 图片下载端点

**URL**: `GET https://internal-api-drive-stream.feishu.cn/space/api/box/stream/download/v2/cover/{image_token}/`

**查询参数**:

| 参数 | 值 | 说明 |
|------|-----|------|
| height | `4096` | ✅ 请求高度。4096 = "尽可能大"，服务端按原图分辨率上限裁切 |
| width | `4096` | ✅ 请求宽度。同上 |
| mount_node_token | spreadsheetToken | ✅ **必需**。表格 token，用于授权验证 |
| mount_point | `sheet_image` | ✅ **必需**。告诉服务端这是表格图片而非其他类型的资源 |
| policy | `equal` | ✅ 保持宽高比 |

**示例 URL**:
```
https://internal-api-drive-stream.feishu.cn/space/api/box/stream/download/v2/cover/U3Yxb5oMVojZLUxnqI1ch2EWnWT/?height=4096&mount_node_token=W0o2s5NaOhw36Vt8YjEck0Xwnxv&mount_point=sheet_image&policy=equal&width=4096
```

**响应**: 二进制图片数据（PNG 或 JPEG），Content-Type 为 image/png 或 image/jpeg。

#### 与聊天图片下载的对比

| 特性 | 聊天图片 | Sheet 图片 |
|------|---------|-----------|
| 端点 | `static-resource/v1/{key}~?...` | `box/stream/download/v2/cover/{token}/?...` |
| 是否需要 mount 上下文 | 否 | **是**（mount_node_token + mount_point） |
| 是否加密 | 可能（AES-256-GCM） | **否**（直接返回图片字节） |
| 分辨率控制 | `image_size=noop` | `width=N&height=N&policy=equal` |
| 下载函数 | `cmd_download_image` | `download_sheet_image` |

#### 关键发现

1. **static-resource 端点对 sheet 图片不适用**: 使用 `static-resource/v1/{token}~?image_size=noop&format=image` 请求 sheet 图片 token 返回 HTTP 400 (invalid token format)。

2. **cover 端点返回全分辨率**: 设置 `height=4096&width=4096` 可获取接近原始分辨率的图片（如 918×595），而非缩略图。

3. **图片 token 格式不同**: Sheet 图片 token（如 `U3Yxb5oMVojZLUxnqI1ch2EWnWT`，27 字符）与聊天图片 key（通常更短）格式不同。

4. **sheetId 匹配正则修复**: 原代码中 `_SHEET_ID_RE = re.compile(r'^[0-9a-f]{6}$')` 只匹配纯十六进制 sheetId，但实际存在如 `sdM9B5` 这样含非十六进制字母的 ID。已修复为 `r'^[0-9a-zA-Z]{6}$'`。

#### 使用示例

**CLI**:
```bash
# 列出所有图片
lark sheet images "https://nio.feishu.cn/sheets/W0o2s5NaOhw36Vt8YjEck0Xwnxv?sheet=sdM9B5"

# 列出指定单元格的图片
lark sheet images "https://nio.feishu.cn/sheets/W0o2s5NaOhw36Vt8YjEck0Xwnxv" --sheet "角窗SPR点检表" --cell E3

# 下载图片
lark sheet images "https://nio.feishu.cn/sheets/W0o2s5NaOhw36Vt8YjEck0Xwnxv" --cell E3 --download --out ./images
```

**Python**:
```python
from lark_tools.commands.sheet import fetch_sheet_images
from lark_tools.commands.img import download_sheet_image

# 提取图片元数据
data = fetch_sheet_images(cookies, "W0o2s5NaOhw36Vt8YjEck0Xwnxv", "sdM9B5")
for img in data['images']:
    cells = data['cell_map'].get(img['index'] + 1, [])
    print(f"[{img['index']}] token={img['token']} cells={cells} {img['width']}x{img['height']}")

# 下载指定图片
download_sheet_image(cookies, img['token'], spreadsheet_token, "output.png")
```

