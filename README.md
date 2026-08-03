# Chat Agent - AI 智能助手系统

基于 LangGraph 构建的全功能 AI 智能助手系统，支持多轮对话、知识库检索、技能扩展和多模态交互。

## 项目概述

Chat Agent 是一个企业级 AI 助手框架，采用现代化的架构设计，提供以下核心能力：

- **智能对话**：基于 LangGraph ReAct Agent 的多轮对话引擎
- **知识库检索**：RAG（检索增强生成）系统，支持文档导入和智能检索
- **技能扩展**：可插拔的技能系统，支持自定义技能开发
- **多模态交互**：支持图片分析和视觉理解
- **会话管理**：多会话并行，完整的对话历史管理
- **飞书集成**：支持飞书扫码登录和数据访问

## 技术栈

### 后端
- **Python 3.11+**
- **LangGraph**：Agent 编排框架
- **LangChain**：LLM 集成框架
- **FastAPI**：高性能 Web 框架
- **Uvicorn**：ASGI 服务器
- **Pydantic**：数据验证

### 前端
- **React 18**
- **Vite**：构建工具
- **Markdown 渲染**：react-markdown

### 支持的 LLM
- DeepSeek（默认）
- OpenAI 兼容接口
- 多模态视觉模型

## 项目结构

```
chat_agent/
├── backend/                    # 后端核心代码
│   ├── adapters/              # 搜索适配器
│   │   ├── base.py           # 抽象基类与类型定义
│   │   ├── bing_adapter.py   # Bing 搜索适配器
│   │   ├── tavily_adapter.py # Tavily 搜索适配器
│   │   └── factory.py        # 适配器工厂
│   ├── rag/                   # RAG 知识库系统
│   │   ├── service.py        # 知识库服务
│   │   ├── chunking.py       # 文档分块
│   │   ├── evaluation.py     # 检索评估
│   │   └── tools.py          # RAG 工具
│   ├── prompts/               # 提示词模板
│   │   ├── system_prompt.md  # 系统提示词
│   │   └── agent_policy.md   # Agent 策略
│   ├── tests/                 # 测试用例
│   ├── agent.py              # Agent 核心逻辑
│   ├── config.py             # 配置管理
│   ├── main.py               # FastAPI 入口
│   ├── session_store.py      # 会话存储
│   ├── skills.py             # 技能系统
│   ├── subagents.py          # 子代理管理
│   ├── tools.py              # 工具定义
│   └── vision.py             # 视觉分析
├── frontend/                  # 前端代码
│   ├── src/                  # 源代码
│   ├── index.html            # 入口 HTML
│   └── package.json          # 依赖配置
├── skills/                    # 内置技能
│   ├── pdf/                  # PDF 处理
│   ├── pptx/                 # PPT 生成
│   ├── xlsx/                 # Excel 处理
│   └── architecture-diagram-generator/  # 架构图生成
├── docs/                      # 项目文档
├── config.json               # 配置文件
├── CLAUDE.md                 # 开发规范
└── README.md                 # 项目说明
```

## 快速开始

### 环境要求

- Python 3.11 或更高版本
- Node.js 18 或更高版本（用于前端）
- Git

### 1. 克隆项目

```bash
git clone <repository-url>
cd chat_agent
```

### 2. 后端配置

#### 创建配置文件

在项目根目录创建 `config.json`：

```json
{
  "base_url": "https://api.deepseek.com",
  "api_key": "your-api-key-here",
  "model": "deepseek-chat",
  "tavily_api_key": "your-tavily-key",
  "tavily_base_url": "https://api.tavily.com"
}
```

#### 安装依赖

```bash
cd backend
pip install -r requirements.txt
```

#### 启动后端服务

```bash
python -m backend.main
```

服务将在 `http://localhost:8000` 启动。

### 3. 前端配置

```bash
cd frontend
npm install
npm run dev
```

前端将在 `http://localhost:5173` 启动。

## 配置说明

### 环境变量

| 变量名 | 说明 | 默认值 |
|--------|------|--------|
| `CHAT_AGENT_RUNTIME_CONFIG` | 运行时配置文件路径 | `runtime_config.json` |
| `TAVILY_API_KEY` | Tavily API 密钥 | - |
| `TAVILY_BASE_URL` | Tavily API 地址 | `https://api.tavily.com` |
| `WEB_SEARCH_ADAPTER` | 搜索适配器类型 | `bing` |
| `VISION_ENABLED` | 启用视觉功能 | `false` |
| `VISION_BASE_URL` | 视觉模型地址 | - |
| `VISION_MODEL` | 视觉模型名称 | - |
| `VISION_API_KEY` | 视觉模型 API 密钥 | - |

### 配置文件

主要配置文件为 `config.json`，包含以下配置项：

```json
{
  "base_url": "LLM API 地址",
  "api_key": "API 密钥",
  "model": "模型名称",
  "model_context_window": 128000,
  "tavily_api_key": "Tavily 搜索 API 密钥",
  "tavily_base_url": "Tavily API 地址",
  "vision": {
    "enabled": true,
    "base_url": "视觉模型地址",
    "model": "模型名称",
    "api_key": "API 密钥"
  }
}
```

**配置项说明：**

| 配置项 | 类型 | 必填 | 说明 |
|--------|------|------|------|
| `base_url` | string | 是 | LLM API 地址 |
| `api_key` | string | 是 | API 密钥 |
| `model` | string | 是 | 模型名称 |
| `model_context_window` | int | 否 | 模型上下文窗口大小（token 数），用于监控剩余容量 |
| `tavily_api_key` | string | 否 | Tavily 搜索 API 密钥 |
| `tavily_base_url` | string | 否 | Tavily API 地址 |

## API 接口

### 对话接口

#### 流式对话

```http
POST /chat/stream
Content-Type: application/json

{
  "message": "你好，请介绍一下自己",
  "session_id": "default",
  "knowledge_mode": false
}
```

#### 同步对话

```http
POST /chat
Content-Type: application/json

{
  "message": "你好",
  "session_id": "default"
}
```

### 会话管理

#### 获取会话列表

```http
GET /sessions
```

#### 创建会话

```http
POST /sessions
Content-Type: application/json

{
  "session_id": "my-session",
  "title": "我的会话"
}
```

#### 删除会话

```http
DELETE /sessions/{session_id}
```

### 知识库接口

#### 导入文档

```http
POST /knowledge/import
Content-Type: multipart/form-data

collection=default
files=@document.pdf
```

#### 搜索知识库

```http
POST /knowledge/sync
Content-Type: application/json

{
  "collection": "default"
}
```

### 技能接口

#### 获取技能列表

```http
GET /skills
```

#### 安装技能

```http
POST /skills/install
Content-Type: application/json

{
  "name": "architecture-diagram-generator"
}
```

#### 执行技能

```http
POST /skills/{name}/execute
Content-Type: application/json

{
  "params": {
    "system_description": "系统描述"
  }
}
```

### 文件上传

```http
POST /uploads
Content-Type: multipart/form-data

session_id=default
files=@file1.txt
files=@file2.pdf
```

## 核心功能

### 1. 智能对话

基于 LangGraph ReAct Agent 实现的多轮对话系统：

- 支持上下文记忆
- 自动工具调用
- 错误恢复机制
- 流式响应输出

### 2. 知识库检索（RAG）

完整的检索增强生成系统：

- **文档导入**：支持 PDF、TXT、Markdown 等格式
- **智能分块**：自动文档分块和索引
- **语义检索**：基于向量的语义搜索
- **引用追踪**：自动标注信息来源

### 3. 技能系统

可扩展的技能框架：

- **内置技能**：
  - PDF 处理：文档解析、格式转换
  - PPT 生成：自动演示文稿生成
  - Excel 处理：数据表格操作
  - 架构图生成：系统架构可视化

- **自定义技能**：
  - 通过 `SKILL.md` 定义技能规范
  - 支持参数化配置
  - 独立的执行环境

### 4. 多模态支持

- **图片分析**：支持图片内容理解
- **视觉问答**：基于图片的问答交互
- **文档 OCR**：图片文字识别

### 5. 上下文容量监控

系统支持实时监控模型上下文窗口使用情况：

- **配置方式**：在 `config.json` 中设置 `model_context_window` 参数
- **容量检查**：在每次 Agent 运行开始时检查剩余容量
- **实时反馈**：通过 debug 事件返回以下信息：
  - `context_token_estimate`：当前上下文 token 估算值
  - `model_context_window`：模型上下文窗口大小
  - `remaining_tokens`：剩余可用 token 数
  - `is_context_exceeded`：是否已超出限制

**配置示例：**
```json
{
  "model": "deepseek-chat",
  "model_context_window": 128000
}
```

**Debug 事件示例：**
```json
{
  "event": "debug",
  "data": {
    "stage": "agent_start",
    "context_token_estimate": 50000,
    "model_context_window": 128000,
    "remaining_tokens": 78000,
    "is_context_exceeded": false
  }
}
```

### 6. 搜索适配器

支持多种搜索后端：

- **Bing 搜索**：免费，无需 API Key
- **Tavily 搜索**：高质量，需要 API Key

通过环境变量切换：
```bash
export WEB_SEARCH_ADAPTER=tavily  # 使用 Tavily
export WEB_SEARCH_ADAPTER=bing    # 使用 Bing（默认）
```

### 6. 飞书集成

- 扫码登录飞书
- 访问飞书多维表格
- 数据同步和查询

## 开发指南

### 添加新工具

在 `backend/tools.py` 中定义新工具：

```python
from langchain_core.tools import tool

@tool
def my_custom_tool(param1: str, param2: int) -> str:
    """工具描述"""
    # 实现逻辑
    return result
```

然后在 `get_all_tools()` 函数中注册。

### 创建新技能

1. 在 `skills/` 目录创建技能文件夹
2. 创建 `SKILL.md` 定义技能规范
3. 实现技能逻辑
4. 通过 API 安装和调用

### 配置 LLM

修改 `config.json` 中的 LLM 配置：

```json
{
  "base_url": "https://your-llm-api.com",
  "api_key": "your-api-key",
  "model": "model-name"
}
```

支持任何 OpenAI 兼容的 API 接口。

## 测试

### 运行测试

```bash
cd backend
python -m pytest tests/
```

### 测试覆盖

- 单元测试：核心功能模块
- 集成测试：API 接口测试
- 工具测试：各个工具的独立测试

## 部署

### 生产环境配置

1. **环境变量配置**：
   ```bash
   export CHAT_AGENT_APP_BACKEND_PORT=8000
   export CHAT_AGENT_APP_BACKEND_BIND_HOST=0.0.0.0
   ```

2. **使用 Gunicorn**：
   ```bash
   pip install gunicorn
   gunicorn backend.main:app -w 4 -k uvicorn.workers.UvicornWorker
   ```

3. **Docker 部署**：
   ```dockerfile
   FROM python:3.11-slim
   WORKDIR /app
   COPY backend/ ./backend/
   COPY config.json .
   RUN pip install -r backend/requirements.txt
   CMD ["python", "-m", "backend.main"]
   ```

### 性能优化

- 启用搜索缓存（默认 15 分钟）
- 配置合适的并发数
- 使用 Redis 作为会话存储（可选）

## 常见问题

### Q: 如何切换搜索后端？

A: 设置环境变量 `WEB_SEARCH_ADAPTER=tavily` 或 `WEB_SEARCH_ADAPTER=bing`。

### Q: 如何添加自定义 LLM？

A: 修改 `config.json` 中的 `base_url`、`api_key` 和 `model` 字段，支持任何 OpenAI 兼容接口。

### Q: 知识库支持哪些文件格式？

A: 目前支持 PDF、TXT、Markdown、DOCX 等常见文档格式。

### Q: 如何扩展技能系统？

A: 在 `skills/` 目录创建新的技能文件夹，按照 `SKILL.md` 规范实现即可。

## 贡献指南

1. Fork 项目
2. 创建功能分支：`git checkout -b feature/my-feature`
3. 提交更改：`git commit -m 'Add my feature'`
4. 推送分支：`git push origin feature/my-feature`
5. 提交 Pull Request

### 代码规范

- 遵循 PEP 8 Python 代码规范
- 使用类型注解
- 编写清晰的文档字符串
- 禁止硬编码具体实体（参见 `CLAUDE.md`）

## 许可证

本项目采用 MIT 许可证。详见 [LICENSE](LICENSE) 文件。

## 联系方式

- 项目主页：[GitHub Repository]
- 问题反馈：[Issues]
- 邮箱：[your-email@example.com]

## 更新日志

### v1.0.0 (2026-07-29)
- 初始版本发布
- 实现核心对话功能
- 支持知识库检索
- 集成技能系统
- 支持多模态交互

---

**Chat Agent** - 让 AI 助手更智能、更强大
