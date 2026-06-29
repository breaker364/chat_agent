# 麦当劳 MCP 接入说明

基于文档：

- `https://open.mcd.cn/mcp/doc`
- `https://open.mcd.cn/mcp/guide.md`

## 1. MCP 服务配置

文档给出的 MCP 配置为：

```json
{
  "mcpServers": {
    "mcd-mcp": {
      "type": "streamablehttp",
      "url": "https://mcp.mcd.cn",
      "headers": {
        "Authorization": "Bearer YOUR_MCP_TOKEN"
      }
    }
  }
}
```

当前本机 Codex 已写入：

- `C:\Users\hank.yu3\.codex\mcp\mcd_mcp_server\.mcp.json`

## 2. 当前 agent 系统接入方式

当前项目的后端工具加载集中在：

- `backend/tools.py`

其中 `load_12306_tools(...)` 是一个 `stdio MCP` 的例子。

麦当劳 MCP 是 `streamablehttp`，因此不能直接复用 `StdioConnection(command,args,...)` 的方式。

## 3. 建议接入方案

### 方案 A：通过 LangChain MCP 的 HTTP 连接器接入

如果当前 `langchain_mcp_adapters` 版本支持 HTTP / streamable HTTP transport，
新增类似：

```python
async def load_mcd_tools() -> list[Any]:
    connection = HttpConnection(
        transport="streamablehttp",
        url="https://mcp.mcd.cn",
        headers={
            "Authorization": "Bearer <TOKEN>",
        },
    )
    return await load_mcp_tools(session=None, connection=connection)
```

然后在 `get_all_tools()` 中：

```python
tools.extend(await load_mcd_tools())
```

### 方案 B：通过独立 MCP proxy 包装成 stdio

如果当前 LangChain MCP 适配器不支持 HTTP MCP，
建议增加一个本地 proxy：

- 输入：stdio MCP
- 输出：转发到 `https://mcp.mcd.cn`

然后当前 agent 系统仍然按 `stdio` 工具加载。

## 4. 认证头

请求头固定为：

```text
Authorization: Bearer <MCP_TOKEN>
```

## 5. 平台限制

根据官方文档：

- 协议：`Streamable HTTP`
- 地址：`https://mcp.mcd.cn`
- 限流：每个 token 每分钟最多 600 次请求
- 401：token 无效 / 过期 / 未提供
- 429：限流

## 6. 工具列表

官方文档列出的工具包括但不限于：

- 餐品营养信息
- 地址管理
- 外送门店查询
- 查询餐品列表/详情
- 价格计算
- 创建订单
- 查询订单
- 活动日历
- 优惠券查询/领取
- 积分与商城兑换
- 当前时间信息

适合在当前 agent 中作为餐饮生活服务类 MCP 工具集接入。
