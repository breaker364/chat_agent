# Feishu Tenant Host Generalization Design

## Goal

让 Feishu 个人用户使用自己的 `*.feishu.cn` 资源 URL 时，CLI、文档读取/写入、表格、知识库、白板、文件和登录 session 校验都访问该 URL 对应的租户 host，不再依赖固定租户 host。历史抓包记录和示例文档不属于运行时逻辑，保留原样。

## Scope

- 修改 `skills/feishu-personal` 运行时代码中的租户 host 处理。
- 修改 `backend/feishu_web_login.py` 的 session 服务端校验，使探测地址可配置并默认使用公共 Feishu host。
- 保留 Feishu 登录、网关、上传等平台级公共服务 host；仅消除固定租户 host 的依赖。
- 不修改历史文档、抓包记录和用于展示历史结果的固定样例。
- 不读取、提交或输出 `sessionss/feishu_web_session.json` 中的 session 值。

## Design

### 1. Centralized host resolution

在 `skills/feishu-personal/lark_tools/config.py` 增加通用 host 解析和 URL 构造能力：

- 默认 host 由环境变量配置，未配置时使用公共 `www.feishu.cn`。
- 从资源 URL 提取 hostname，并只接受 `feishu.cn` 或其子域名；显式的非 Feishu host 拒绝作为请求目标，避免把 session cookie 发往外部站点。
- 保留 `DOC_HOST`、`BITABLE_HOST` 作为兼容默认值，但它们不再代表固定租户；新调用优先使用当前资源解析出的 host。
- 所有解析结果统一为不带 scheme、path、query 或尾部斜杠的 hostname。

### 2. Per-call host propagation

按资源入口解析 host，并作为可选参数传给底层请求函数：

- `inspect`、`doc`、`bitable`、`sheet`、`wiki`、`minutes` 的 URL/token 解析器返回或派生 host。
- 文档、表格、知识库、白板、导入导出和写操作的 HTTP 函数接收 `host` 参数；未传入时回退到配置默认值。
- 生成规范 URL、Markdown 图片/引用 URL、响应中的 `url` 和 `sheetsUrl` 使用本次调用的 host。
- 网关请求的 `Origin` 和 `Referer` 使用调用传入的 host；聊天、用户等没有资源 URL 的命令使用配置默认值。
- 不使用进程级可变 host，避免并行请求之间串租户。

### 3. Session server validation

`is_feishu_session_server_valid` 增加可选探测 URL/host 参数：

- 默认探测地址来自环境变量或配置的公共 Feishu URL。
- 相对重定向使用初始探测 host 解析，绝不拼接固定租户 host。
- 登录页判断保留通用的 `accounts`、`passport`、`login` 路径特征。
- 现有调用保持兼容；需要验证特定个人租户时可传入用户资源 URL 的 host。

### 4. Compatibility

- 保持现有 CLI 命令参数和主函数返回结构，尽量通过新增可选参数和返回字段完成改造。
- 裸 token 仍可工作，但使用可配置默认 host；当服务端无法从公共 host 解析资源时，返回现有错误并提示用户提供完整 URL。
- 仅对显式 URL 做租户切换，避免不同请求共享隐式状态。

## Error handling

- 空 host、非法 host 或非 `feishu.cn` host 触发明确的 `ValueError`/CLI 错误，不发起网络请求。
- 缺少 session、session 过期、探测超时和重定向到登录页继续返回现有结构化结果。
- 任何错误信息不包含 session cookie 值。

## Testing

先写失败测试，再实现：

1. host 解析测试覆盖个人租户 URL、公共默认值、查询参数/尾斜杠清理和外部 host 拒绝。
2. `inspect`/URL resolver 测试验证 fake HTTP getter 收到个人租户 host，生成 URL 不出现固定租户 host。
3. 文档/表格/网关请求测试验证 host、Origin、Referer 和生成链接来自调用参数。
4. session server validation 测试验证自定义探测 host、相对重定向拼接和登录重定向判定。
5. 运行 Feishu skill 的完整 pytest 集合，再使用现有 session 文件对用户提供的个人 wiki URL 做只读集成验证；不执行写操作。

## Alternatives considered

### 全局修改 `DOC_HOST`

改动最少，但并发调用会共享隐式状态，容易把不同资源发到错误租户，不采用。

### 为每个命令复制一套 URL 解析

局部实现快，但会再次产生不一致和新的硬编码，不采用。

### 集中解析并显式传递 host

需要调整多个底层函数的可选参数，但边界清晰、可测试、兼容裸 token，并能覆盖请求头和生成链接，作为推荐方案。
