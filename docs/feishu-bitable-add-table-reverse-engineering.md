# 飞书 Base 新增数据表接口逆向文档

> 基于 2026-07-01 在 `https://nio.feishu.cn/wiki/UrxTwZQp0ijF0okPcfXcL3Hqnuh` 下真实抓包得到。
> 本文仅覆盖 **在现有 Base 中新增一个数据表（table）** 的能力。
>
> 参数状态标记：
> - `✓` 已知：已通过真实抓包或真实回放验证
> - `~` 部分已知：能用，但含义/约束未完全确定
> - `?` 未知：当前只是经验值，未完成边界验证
> - `opt` 可选：可省略或可能由服务端补全

---

## 目录

1. [能力结论](#能力结论)
2. [通道概览](#通道概览)
3. [真实抓包结论](#真实抓包结论)
4. [写请求结构](#写请求结构)
5. [AddTableV2 操作结构](#addtablev2-操作结构)
6. [Snapshot 初始化结构](#snapshot-初始化结构)
7. [回放验证结论](#回放验证结论)
8. [当前已知约束](#当前已知约束)
9. [与 create_bitable 的关系](#与-create_bitable-的关系)

---

## 能力结论

在现有 Base 中新增一个“数据表”并不是通过简单 REST `create_table` 接口完成的，而是通过：

- **业务类型**：`BITABLE_BASE`
- **写入类型**：`USER_CHANGES`
- **底层操作**：`AddTableV2`

提交时一次性携带完整 `snapshot`，初始化：

- 默认字段
- 默认视图
- 默认记录
- 主键
- rank 信息

这意味着“新增数据表”本质上是一个 **OT / CRDT 风格的整表初始化操作**，而不是“先建空表再补字段”。

---

## 通道概览

### 1. 监听通道

页面会先对当前 Base 和当前 Table 建立观察：

```json
{
  "type": "COLLABROOM",
  "data": {
    "type": "WATCH",
    "entities": [
      {
        "route_key": "<base_token>",
        "route_type": "token",
        "type": "BITABLE_BASE",
        "token": "<base_token>",
        "schema_version": 5
      },
      {
        "route_key": "<base_token>",
        "route_type": "token",
        "type": "BITABLE_TABLE",
        "token": "<table_id>",
        "schema_version": 5
      }
    ]
  }
}
```

### 2. 写入通道

新增数据表使用：

- Endpoint: `POST /space/api/rce/messages?member_id=<member_id>`
- Outer `type`: `BITABLE_BASE`
- Inner `data.type`: `USER_CHANGES`

这和字段/记录写入使用 `BITABLE_TABLE` 的方式不同。

---

## 真实抓包结论

真实抓包中，创建新表后页面从：

- 原表：`tblXt65AhVSLksZR`

切换到了新表：

- 新表：`tbl1ifqIwboqD15l`
- 新视图：`vewkJIKRMU`

抓到的成功提交为：

```json
{
  "type": "BITABLE_BASE",
  "data": {
    "type": "USER_CHANGES",
    "token": "VUkpbryf6a7Eghs6NgZci1Q4nWG",
    "localRev": 13,
    "operations": "<gzip+base64>",
    "signature": "000f10e7-4944-4e68-a6ac-d23e9af307a3",
    "content_type": "gzip/base64"
  }
}
```

服务端返回：

```json
{
  "type": "BITABLE_BASE",
  "code": 0,
  "data": {
    "code": 0,
    "type": "ACCEPT_COMMIT",
    "rev": 14
  }
}
```

`ACCEPT_COMMIT` 是当前成功判定标准。

---

## 写请求结构

### Endpoint

`POST https://nio.feishu.cn/space/api/rce/messages?member_id=<member_id>`

### Headers

下列头部在真实请求中出现，建议尽量保留：

| Header | 状态 | 说明 |
|---|---:|---|
| `X-CSRFToken` | `✓` | 必需，缺失会被拒绝 |
| `Referer` | `✓` | 应指向当前 Base 页面 |
| `docs-host-id` | `✓` | 当前 wiki token |
| `docs-host-type` | `✓` | 当前抓包为 `Wiki` |
| `doc-platform` | `✓` | `web` |
| `doc-biz` | `✓` | `Lark` |
| `F-Version` | `~` | 前端版本号，建议保留 |
| `ccm-meta` | `~` | 抓包中存在，可能影响权限/路由 |
| `x-command` | `✓` | `api.rce.pandora` |
| `Content-Type` | `✓` | `application/json` |

### Body 外层结构

```json
{
  "type": "BITABLE_BASE",
  "data": {
    "member_id": 14775315853518,
    "user_ticket": "<ticket>",
    "type": "USER_CHANGES",
    "token": "<base_token>",
    "lang": "zh",
    "localRev": 13,
    "operations": "<gzip+base64 of OT JSON>",
    "signature": "<uuid-like string>",
    "content_type": "gzip/base64"
  },
  "version": 2,
  "req_id": 13,
  "context": {
    "os": "windows",
    "app_version": "1.0.19.5080",
    "os_version": "10",
    "platform": "web",
    "request_id": "<request id>"
  }
}
```

### Body 字段说明

| 路径 | 状态 | 说明 |
|---|---:|---|
| `type` | `✓` | 固定 `BITABLE_BASE` |
| `data.member_id` | `✓` | 当前会话 member id |
| `data.user_ticket` | `✓` | 当前会话用户票据 |
| `data.type` | `✓` | 固定 `USER_CHANGES` |
| `data.token` | `✓` | Base token，不是 table token |
| `data.lang` | `✓` | 抓包为 `zh` |
| `data.localRev` | `✓` | 当前 Base 版本号 |
| `data.operations` | `✓` | gzip+base64 编码的 OT JSON |
| `data.signature` | `~` | 抓包中存在，当前可复用 |
| `data.content_type` | `✓` | 固定 `gzip/base64` |
| `version` | `✓` | 固定 `2` |
| `req_id` | `~` | 请求序号 |
| `context.*` | `~` | 建议保留前端风格 |

---

## AddTableV2 操作结构

将 `operations` 解压后，核心操作如下：

```json
[
  {
    "command": "AddTableV2",
    "type": 1,
    "actions": [
      {
        "action": "base.addTableV2",
        "type": 1,
        "tableId": "tbl1ifqIwboqD15l",
        "isRecover": false,
        "contentCreation": false,
        "sourceTableId": null,
        "renewRecord": true,
        "data": {
          "index": 4,
          "blockIndex": 4,
          "name": "数据表 3",
          "exInfo": {
            "contentCreation": false
          },
          "parentId": null,
          "snapshot": { "...": "..." },
          "total": 4
        }
      }
    ],
    "authInfo": {
      "src_prod_type": "BITABLE_TABLE_IND"
    }
  }
]
```

### 字段说明

| 路径 | 状态 | 说明 |
|---|---:|---|
| `command` | `✓` | 固定 `AddTableV2` |
| `actions[0].action` | `✓` | 固定 `base.addTableV2` |
| `actions[0].tableId` | `✓` | 新表 ID，16 字符：`tbl` + 13 位 |
| `actions[0].isRecover` | `✓` | 抓包为 `false` |
| `actions[0].contentCreation` | `✓` | 抓包为 `false` |
| `actions[0].sourceTableId` | `✓` | 新建时为 `null` |
| `actions[0].renewRecord` | `✓` | 抓包为 `true` |
| `data.index` | `✓` | 插入索引 |
| `data.blockIndex` | `✓` | 与 `index` 相同 |
| `data.name` | `✓` | 新表名称 |
| `data.exInfo.contentCreation` | `✓` | 抓包为 `false` |
| `data.parentId` | `✓` | 当前抓包为 `null` |
| `data.snapshot` | `✓` | 新表初始化快照 |
| `data.total` | `~` | 当前抓包与 `index` 相同 |
| `authInfo.src_prod_type` | `✓` | 抓包为 `BITABLE_TABLE_IND` |

---

## Snapshot 初始化结构

`snapshot` 是成功创建的关键。它至少包含：

- `recordMap`
- `fieldMap`
- `primaryKey`
- `viewMap`
- `userMap`
- `recordMeta`
- `commentMap`
- `resourceMap`
- `milestoneMap`
- `recordsNum`
- `rankInfo`

### 1. 默认字段

抓包中的默认字段为一个文本列：

```json
"fieldMap": {
  "fldE42diAI": {
    "id": "fldE42diAI",
    "name": "文本",
    "type": 1,
    "property": null,
    "fieldUIType": "Text",
    "allowedEditModes": {
      "manual": true,
      "scan": false
    }
  }
}
```

### 2. 默认视图

抓包中的默认视图：

```json
"viewMap": {
  "vewkJIKRMU": {
    "id": "vewkJIKRMU",
    "name": "表格",
    "type": 1,
    "property": {
      "fields": ["fldE42diAI"],
      "records": [],
      "sortInfo": [],
      "group": [],
      "frozenColCount": 1
    },
    "index": 0
  }
}
```

### 3. 默认记录

抓包显示前端一次性带了 5 条空记录：

```json
"recordMap": {
  "recvo5H6y6yvVN": {},
  "recvo5H6y6gDSX": {},
  "recvo5H6y6kgoK": {},
  "recvo5H6y6DbYt": {},
  "recvo5H6y6HGcv": {}
},
"recordsNum": 5
```

### 4. rankInfo

抓包中的 rank 字符串不是简单线性十六进制累加，而是前端已有一套排序编码：

```json
"rankInfo": {
  "rankStep": 1048576,
  "nextRank": "i00034dfk",
  "rankMap": {
    "recvo5H6y6yvVN": "i00000000",
    "recvo5H6y6gDSX": "i0000mh34",
    "recvo5H6y6kgoK": "i00018y68",
    "recvo5H6y6DbYt": "i0001vf9c",
    "recvo5H6y6HGcv": "i0002hwcg"
  },
  "viewRankMap": {
    "vewkJIKRMU": {
      "rankMap": {}
    }
  }
}
```

当前已知：`rankMap` 不能随便用线性递增占位值替代，否则服务端可能返回 `invalid param`。

---

## 回放验证结论

已完成两类回放：

### 1. 抽象后生成 payload

结果：

- 命中正确写通道
- 服务端返回 `invalid param`

说明：

- 通道方向正确
- 但表/字段/视图/记录 ID 长度、索引、rank 等字段需要高度贴近前端真实结构

### 2. 贴近抓包结构后再回放

结果：

- 成功返回 `ACCEPT_COMMIT`
- 并已在样例 Base 中真实新增数据表 `逆向test`

样例验证结果（回读 `tableMap`）：

```json
"tblIGZlpD2n7aZRA": {
  "name": "逆向test"
}
```

---

## 当前已知约束

1. `BITABLE_BASE` 新增表与 `BITABLE_TABLE` 写记录/写字段不同，不能混用。
2. `user_ticket` 当前是必需的；空字符串方案仅适用于现有 `BITABLE_TABLE` 的部分写操作，不适用于新增表。
3. `localRev` 必须与当前 Base 修订号对齐。
4. `snapshot` 不能只带字段或只带视图，必须整体初始化。
5. `rankInfo.rankMap` 目前仍建议保持前端真实风格。

---

## 与 create_bitable 的关系

需要区分两个能力：

### 1. 新建一个 Base / 多维表格文件

这是 `create_node` / wiki 节点创建问题，属于：

- `obj_type = 8`
- wiki tree create node

### 2. 在已有 Base 中新增一个数据表

这是本文覆盖的问题，属于：

- `BITABLE_BASE`
- `USER_CHANGES`
- `AddTableV2`

两者不是同一个接口，也不是同一个协议层。

---

## 后续建议

下一步如果要扩展成“批量创建多个数据表”，建议：

1. 先验证 `operations` 数组里是否允许一次放多个 `AddTableV2`
2. 若不允许，则顺序提交多个 `USER_CHANGES`
3. 每次提交后刷新 `localRev`
4. 为每个新表独立生成：
   - `tableId`
   - `fieldId`
   - `viewId`
   - `recordIds`
   - `rankMap`

