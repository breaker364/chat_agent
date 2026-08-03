# RAG 系统实现文档

本文档详细描述了 Chat Agent 项目中 RAG（检索增强生成）系统的实现细节，便于复现和二次开发。

## 1. 系统架构概览

```
┌─────────────────────────────────────────────────────────────────┐
│                      RAG 系统架构                               │
├─────────────────────────────────────────────────────────────────┤
│                                                                 │
│  ┌──────────────┐    ┌──────────────┐    ┌──────────────┐      │
│  │   文档导入    │    │   分块处理    │    │   索引存储    │      │
│  │  (Import)    │───▶│  (Chunking)  │───▶│  (Indexing)  │      │
│  └──────────────┘    └──────────────┘    └──────────────┘      │
│         │                   │                   │               │
│         ▼                   ▼                   ▼               │
│  ┌──────────────┐    ┌──────────────┐    ┌──────────────┐      │
│  │  文件解析器   │    │  语义分块器   │    │  向量/词法   │      │
│  │  (Parser)    │    │  (Chunker)   │    │   索引       │      │
│  └──────────────┘    └──────────────┘    └──────────────┘      │
│                                                                 │
├─────────────────────────────────────────────────────────────────┤
│                                                                 │
│  ┌──────────────┐    ┌──────────────┐    ┌──────────────┐      │
│  │   查询处理    │    │   混合检索    │    │   结果排序   │      │
│  │  (Query)     │───▶│  (Retrieval) │───▶│  (Ranking)   │      │
│  └──────────────┘    └──────────────┘    └──────────────┘      │
│         │                   │                   │               │
│         ▼                   ▼                   ▼               │
│  ┌──────────────┐    ┌──────────────┐    ┌──────────────┐      │
│  │  查询分词    │    │  词法+稠密   │    │  RRF 融合    │      │
│  │  (Tokenize)  │    │  混合检索    │    │  (Fusion)    │      │
│  └──────────────┘    └──────────────┘    └──────────────┘      │
│                                                                 │
└─────────────────────────────────────────────────────────────────┘
```

## 2. 核心模块说明

### 2.1 模块结构

```
backend/rag/
├── __init__.py          # 包初始化
├── config.py            # 配置定义和加载
├── models.py            # 数据模型定义
├── chunking.py          # 文档分块实现
├── service.py           # 知识库核心服务
├── tools.py             # Agent 工具定义
├── evaluation.py        # RAG 评估系统
├── model_assets.py      # 模型资源管理
└── tests/               # 测试用例
```

## 3. 数据模型 (models.py)

### 3.1 ParsedBlock - 解析块

```python
@dataclass
class ParsedBlock:
    text: str                              # 块文本内容
    block_type: str                        # 块类型：paragraph/code/table
    heading_path: list[str] = field(...)   # 标题层级路径
    start_offset: int = 0                  # 起始字符偏移
    end_offset: int = 0                    # 结束字符偏移
```

**用途**：文档解析后的中间表示，保留文档结构信息。

### 3.2 KnowledgeChunk - 知识块

```python
@dataclass
class KnowledgeChunk:
    chunk_id: str                    # 唯一标识符
    doc_id: str                      # 所属文档 ID
    collection: str                  # 集合名称
    ordinal: int                     # 块序号
    text: str                        # 块文本内容
    content_hash: str                # 内容哈希（用于去重/更新检测）
    heading_path: list[str]          # 标题层级路径
    source_ref: str                  # 源文件引用
    start_offset: int                # 起始字符偏移
    end_offset: int                  # 结束字符偏移
    token_count: int                 # Token 数量估算
    chunking_strategy: str           # 分块策略标识
    boundary_method: str             # 边界方法：paragraph/code/table/mixed
    overlap_from_previous: int = 0   # 与前一块的重叠 token 数
    overlap_to_next: int = 0         # 与后一块的重叠 token 数
    metadata: dict[str, Any] = ...   # 扩展元数据
```

**用途**：索引和检索的基本单位，包含完整的上下文信息。

### 3.3 DocumentManifest - 文档清单

```python
@dataclass
class DocumentManifest:
    doc_id: str                      # 文档唯一标识
    collection: str                  # 所属集合
    source_uri: str                  # 源文件路径
    source_type: str                 # 文件类型（txt/md/pdf 等）
    content_hash: str                # 内容哈希
    parser_version: str              # 解析器版本
    chunker_version: str             # 分块器版本
    chunking_signature: str          # 分块配置签名
    title: str                       # 文档标题
    status: str                      # 状态：indexed/failed/pending
    chunk_count: int                 # 分块数量
    metadata: dict[str, Any] = ...   # 扩展元数据
    latest_error: str = ""           # 最新错误信息
```

**用途**：文档级别的元数据管理，支持增量更新和变更检测。

## 4. 配置系统 (config.py)

### 4.1 SemanticChunkingConfig - 分块配置

```python
@dataclass
class SemanticChunkingConfig:
    target_tokens: int = 500                 # 目标 token 数
    max_tokens: int = 800                    # 最大 token 数
    overlap_ratio: float = 0.12              # 重叠比例
    semantic_boundary_threshold: float = 0.72 # 语义边界阈值
    chunker_version: str = "structure-first-semantic-v1"

    def signature(self) -> str:
        """生成配置签名，用于检测配置变更"""
        return (
            f"{self.chunker_version}:target={self.target_tokens}:max={self.max_tokens}:"
            f"overlap={self.overlap_ratio:.3f}:semantic={self.semantic_boundary_threshold:.3f}"
        )
```

### 4.2 HybridRetrievalConfig - 混合检索配置

```python
@dataclass
class HybridRetrievalConfig:
    fusion_strategy: str = "rrf"             # 融合策略：rrf
    lexical_candidate_depth: int = 30        # 词法检索候选深度
    dense_candidate_depth: int = 30          # 稠密检索候选深度
    rrf_k: int = 60                          # RRF 参数 k
    top_k: int = 8                           # 最终返回结果数
```

### 4.3 RerankerConfig - 重排序配置

```python
@dataclass
class RerankerConfig:
    enabled: bool = False                    # 是否启用重排序
    provider: str = "local_overlap"          # 提供者：local_overlap
    model: str = ""                          # 重排序模型
    candidate_top_k: int = 30               # 候选数量
```

### 4.4 RagConfig - 总配置

```python
@dataclass
class RagConfig:
    enabled: bool = False                              # 功能开关
    knowledge_store_path: Path = ...                   # 知识库存储路径
    evaluation_cache_path: Path = ...                  # 评估缓存路径
    report_dir: Path = ...                             # 报告目录
    embedding_provider: str = "local_hashing"          # 嵌入提供者
    vector_backend: str = "in_memory"                  # 向量存储后端
    sparse_backend: str = "in_memory_bm25"             # 稀疏存储后端
    embedding_model: str = ""                          # 嵌入模型
    embedding_dimension: int = 0                       # 嵌入维度
    model_cache_path: Path = ...                       # 模型缓存路径
    chunking: SemanticChunkingConfig = ...             # 分块配置
    hybrid: HybridRetrievalConfig = ...                # 混合检索配置
    reranker: RerankerConfig = ...                     # 重排序配置
```

### 4.5 配置加载

```python
def load_rag_config(overrides: dict[str, Any] | None = None) -> RagConfig:
    """加载 RAG 配置

    优先级：overrides > runtime_config.json > 环境变量 > 默认值

    Args:
        overrides: 配置覆盖字典

    Returns:
        RagConfig 实例
    """
```

**配置示例** (runtime_config.json):

```json
{
  "rag": {
    "enabled": true,
    "knowledge_store_path": "knowledge_base",
    "embedding_provider": "local_hashing",
    "chunking": {
      "target_tokens": 500,
      "max_tokens": 800,
      "overlap_ratio": 0.12
    },
    "hybrid": {
      "fusion_strategy": "rrf",
      "top_k": 8
    },
    "reranker": {
      "enabled": false
    }
  }
}
```

## 5. 文档分块 (chunking.py)

### 5.1 StructureFirstSemanticChunker - 结构优先语义分块器

这是系统的核心分块器，采用"结构优先"策略：

#### 5.1.1 设计理念

1. **保留文档结构**：优先识别标题、段落、代码块、表格等结构边界
2. **语义完整性**：在同一段落内尽可能保持语义完整
3. **大小控制**：确保分块大小在 [target_tokens, max_tokens] 范围内
4. **重叠机制**：支持分块间重叠以保留上下文连续性

#### 5.1.2 分块流程

```
原始文本
    │
    ▼
┌─────────────────────────────────────┐
│ 1. 结构解析 (parse_blocks)          │
│    - 识别标题层级                   │
│    - 识别段落边界                   │
│    - 识别代码块 (```)               │
│    - 识别表格 (|)                   │
└─────────────────────────────────────┘
    │
    ▼
┌─────────────────────────────────────┐
│ 2. 超大块拆分 (_split_oversized)    │
│    - 按句子拆分                     │
│    - 按 Token 窗口拆分              │
└─────────────────────────────────────┘
    │
    ▼
┌─────────────────────────────────────┐
│ 3. 小块合并 (chunk_text)            │
│    - 相同标题路径的块合并           │
│    - 不超过 target_tokens           │
│    - 保留表格/代码块独立            │
└─────────────────────────────────────┘
    │
    ▼
KnowledgeChunk[]
```

#### 5.1.3 核心方法

```python
class StructureFirstSemanticChunker:
    def __init__(self, config: SemanticChunkingConfig | None = None):
        self.config = config or SemanticChunkingConfig()

    def parse_blocks(self, text: str) -> list[ParsedBlock]:
        """解析文档结构

        识别以下结构：
        - 标题：# ## ### 等
        - 段落：连续非空行
        - 代码块：``` 包围的内容
        - 表格：| 开头的行

        Returns:
            ParsedBlock 列表
        """

    def _split_oversized_block(self, block: ParsedBlock) -> list[ParsedBlock]:
        """拆分超大块

        策略：
        1. 按句子拆分（英文句号、中文句号等）
        2. 如果句子太少，按 Token 窗口拆分

        Returns:
            拆分后的 ParsedBlock 列表
        """

    def chunk_text(
        self,
        text: str,
        *,
        source_ref: str = "",
        doc_id: str = "",
        collection: str = "",
    ) -> list[KnowledgeChunk]:
        """主入口：将文本分块

        Returns:
            KnowledgeChunk 列表
        """
```

#### 5.1.4 Token 估算

```python
_TOKEN_RE = re.compile(r"[A-Za-z0-9_.-]+|[一-鿿]")

def tokenize(text: str) -> list[str]:
    """分词

    规则：
    - 英文/数字：连续字母数字作为一个 token
    - 中文：每个汉字作为一个 token
    """

def _estimate_tokens(text: str) -> int:
    """估算 Token 数量"""
    return max(1, len(tokenize(text)))
```

### 5.2 分块配置参数说明

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `target_tokens` | 500 | 目标分块大小 |
| `max_tokens` | 800 | 最大分块大小 |
| `overlap_ratio` | 0.12 | 重叠比例 |
| `semantic_boundary_threshold` | 0.72 | 语义边界阈值（预留） |

## 6. 知识库服务 (service.py)

### 6.1 PersonalKnowledgeBase 类

这是 RAG 系统的核心服务类，提供完整的文档管理、索引和检索功能。

#### 6.1.1 初始化

```python
class PersonalKnowledgeBase:
    def __init__(
        self,
        *,
        workspace_root: str | Path,        # 工作区根目录
        store_path: str | Path,            # 存储路径
        chunker_config: SemanticChunkingConfig | None = None,
        reranker_enabled: bool = False,
    ) -> None:
        # 创建目录结构
        self.store_path = Path(store_path).resolve()
        self.documents_path = self.store_path / "documents"   # 文档目录
        self.index_path = self.store_path / "index"           # 索引目录
        self.manifests_path = self.store_path / "manifests"   # 清单目录
        self.sync_reports_path = self.store_path / "reports"  # 报告目录

        # 初始化分块器
        self.chunker = StructureFirstSemanticChunker(self.chunker_config)

        # 加载已有数据
        self._load()
```

#### 6.1.2 存储结构

```
knowledge_base/
├── documents/                    # 文档目录
│   ├── default/                  # 默认集合
│   │   ├── document1.txt
│   │   └── document2.pdf
│   └── team_notes/               # 团队笔记集合
│       └── meeting.md
├── index/                        # 索引目录
│   └── chunks.json               # 所有分块数据
├── manifests/                    # 清单目录
│   └── manifests.json            # 文档清单
├── reports/                      # 同步报告
│   └── sync-1234567890.json
└── models/                       # 模型缓存（可选）
```

#### 6.1.3 数据持久化

```python
def _load(self) -> None:
    """从 JSON 文件加载清单和分块数据"""

def _save(self) -> None:
    """将清单和分块数据保存到 JSON 文件"""
```

### 6.2 文件导入功能

#### 6.2.1 支持的文件格式

| 格式 | 扩展名 | 解析方式 |
|------|--------|---------|
| 纯文本 | `.txt` | 直接读取 |
| Markdown | `.md`, `.markdown` | 直接读取 |
| CSV | `.csv` | 解析为表格格式 |
| HTML | `.html`, `.htm` | BeautifulSoup 提取文本 |
| PDF | `.pdf` | pypdf 提取文本 |
| Word | `.docx` | python-docx 提取文本 |

#### 6.2.2 文件读取实现

```python
def _read_supported_file(self, path: Path) -> str:
    suffix = path.suffix.lower()

    # PDF 解析
    if suffix == ".pdf":
        from pypdf import PdfReader
        reader = PdfReader(handle)
        pages = [page.extract_text() or "" for page in reader.pages]
        return "\n\n".join(page.strip() for page in pages if page.strip())

    # Word 解析
    if suffix == ".docx":
        from docx import Document
        doc = Document(str(path))
        paragraphs = [paragraph.text.strip() for paragraph in doc.paragraphs]
        # 提取表格内容
        for table in doc.tables:
            for row in table.rows:
                row_text = " | ".join(cell.text.strip() for cell in row.cells)
                paragraphs.append(row_text)
        return "\n\n".join(paragraphs)

    # CSV 解析
    if suffix == ".csv":
        rows = list(csv.reader(handle))
        return "\n".join(" | ".join(cell.strip() for cell in row) for row in rows)

    # HTML 解析
    if suffix in {".html", ".htm"}:
        return BeautifulSoup(raw, "html.parser").get_text("\n")

    # 默认：直接读取
    return html.unescape(raw)
```

#### 6.2.3 import_files - 导入文件

```python
def import_files(
    self,
    collection: str,
    paths: list[str | Path],
    *,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """将文件导入到知识库文档目录

    流程：
    1. 验证文件路径（必须在工作区内）
    2. 复制文件到 documents/{collection}/ 目录
    3. 返回导入结果

    Returns:
        {
            "collection": "default",
            "imported": [...],
            "skipped": [...],
            "counts": {"imported": N, "skipped": M}
        }
    """
```

#### 6.2.4 import_file_bytes - 导入文件内容

```python
def import_file_bytes(
    self,
    collection: str,
    filename: str,
    content: bytes,
    *,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """从字节内容导入文件

    用于上传场景，直接写入文件内容
    """
```

### 6.3 索引功能

#### 6.3.1 index_files - 索引文件

```python
def index_files(
    self,
    collection: str,
    paths: list[str | Path],
    *,
    refresh: bool = False,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """将文件索引到知识库

    流程：
    1. 验证文件路径和类型
    2. 读取文件内容
    3. 计算内容哈希
    4. 检查是否需要更新（哈希变更或配置变更）
    5. 调用分块器分块
    6. 创建文档清单
    7. 保存索引

    Returns:
        {
            "collection": "default",
            "indexed": [{"doc_id": "...", "source_uri": "...", "chunk_count": N}],
            "skipped": [{"path": "...", "reason": "..."}]
        }
    """
```

#### 6.3.2 增量更新机制

```python
# 检测是否需要更新
needs_refresh = (
    refresh                                    # 强制刷新
    or existing is None                        # 新文档
    or existing.content_hash != digest         # 内容变更
    or existing.chunking_signature != chunk_signature  # 配置变更
)
```

### 6.4 同步功能

#### 6.4.1 sync - 同步知识库

```python
def sync(
    self,
    collection: str | None = None,
    *,
    dry_run: bool = False,
) -> dict[str, Any]:
    """同步知识库文档目录与索引

    流程：
    1. 扫描 documents/ 目录下的所有文件
    2. 过滤支持的文件类型
    3. 检查每个文件的状态：
       - indexed: 已索引且内容未变
       - pending_sync: 新文件或内容已变
       - unsupported: 不支持的文件类型
       - failed: 处理失败
    4. 检测已删除的文件并清理索引
    5. 生成同步报告

    Returns:
        {
            "collection": "default",
            "dry_run": false,
            "counts": {"indexed": N, "refreshed": M, ...},
            "files": [...],
            "store_path": "...",
            "report_path": "..."
        }
    """
```

### 6.5 检索功能

#### 6.5.1 混合检索架构

```
查询
  │
  ├─── 词法检索 (Lexical) ───────┐
  │    - 分词                     │
  │    - Token 集合匹配           │
  │    - 计算重叠率               │
  │                               │
  ├─── 稠密检索 (Dense) ─────────┤
  │    - TF 向量化               ├──▶ RRF 融合 ──▶ 重排序 ──▶ 结果
  │    - 余弦相似度              │
  │                              │
  └──────────────────────────────┘
```

#### 6.5.2 词法检索

```python
def _lexical_rank(
    self,
    query_tokens: list[str],
    chunks: list[KnowledgeChunk],
) -> list[tuple[KnowledgeChunk, float]]:
    """词法检索

    算法：
    1. 将查询分词
    2. 对每个 chunk，计算与查询的 token 重叠率
    3. 按重叠率降序排序

    评分公式：
    score = overlap_count / query_token_count
    """
```

#### 6.5.3 稠密检索

```python
def _dense_rank(
    self,
    query: str,
    chunks: list[KnowledgeChunk],
) -> list[tuple[KnowledgeChunk, float]]:
    """稠密检索（基于向量）

    算法：
    1. 将查询和 chunk 文本向量化（词频向量）
    2. 计算余弦相似度
    3. 按相似度降序排序

    向量化方式：
    - 使用 tokenize 分词
    - 统计词频，构建稀疏向量
    """

def _vectorize(text: str) -> dict[str, float]:
    """将文本转换为词频向量"""
    vector: dict[str, float] = {}
    for token in tokenize(text):
        vector[token] = vector.get(token, 0.0) + 1.0
    return vector

def _cosine(
    left: dict[str, float],
    right: dict[str, float],
) -> float:
    """计算余弦相似度"""
    dot = sum(value * right.get(key, 0.0) for key, value in left.items())
    left_norm = math.sqrt(sum(value * value for value in left.values()))
    right_norm = math.sqrt(sum(value * value for value in right.values()))
    return dot / (left_norm * right_norm)
```

#### 6.5.4 RRF 融合

```python
def _rrf(
    self,
    ranked_lists: list[list[tuple[KnowledgeChunk, float]]],
    k: int = 60,
) -> dict[str, float]:
    """Reciprocal Rank Fusion (RRF)

    公式：
    RRF_score(d) = Σ 1 / (k + rank_i(d))

    其中：
    - k: 平滑参数（默认 60）
    - rank_i(d): 文档 d 在第 i 个排名列表中的排名
    """
    scores: dict[str, float] = {}
    for ranked in ranked_lists:
        for rank, (chunk, _) in enumerate(ranked, 1):
            scores[chunk.chunk_id] = scores.get(chunk.chunk_id, 0.0) + 1.0 / (k + rank)
    return scores
```

#### 6.5.5 相邻块扩展

```python
def _expand_with_adjacent_chunks(
    self,
    ranked: list[tuple[KnowledgeChunk, float]],
    chunks: list[KnowledgeChunk],
) -> tuple[list[tuple[KnowledgeChunk, float]], dict[str, str]]:
    """扩展相邻块

    策略：
    - 对于每个检索到的块，同时包含其前一个和后一个块
    - 相邻块的分数略低于原块（乘以 0.999）
    - 用于保留上下文连续性
    """
```

#### 6.5.6 完整检索流程

```python
def search(
    self,
    query: str,
    *,
    collection: str | None = None,
    filters: dict[str, Any] | None = None,
    top_k: int = 8,
    include_scores: bool = True,
) -> dict[str, Any]:
    """主检索方法

    流程：
    1. 获取候选 chunks（按 collection 过滤）
    2. 词法检索
    3. 稠密检索
    4. RRF 融合
    5. （可选）重排序
    6. 相邻块扩展
    7. 截取 top_k 结果
    8. 构建返回结果

    Returns:
        {
            "query": "...",
            "collection": "...",
            "results": [
                {
                    "chunk_id": "...",
                    "doc_id": "...",
                    "collection": "...",
                    "citation_id": "...",
                    "source_ref": "...",
                    "source_uri": "...",
                    "source_type": "...",
                    "title": "...",
                    "heading_path": [...],
                    "snippet": "...",      # 短摘要（360 字符）
                    "excerpt": "...",      # 长摘要（1200 字符）
                    "metadata": {...},
                    "scores": {
                        "fusion_score": 0.xxx,
                        "lexical_score": 0.xxx,
                        "dense_score": 0.xxx
                    }
                }
            ],
            "settings": {
                "fusion_strategy": "rrf",
                "reranker_enabled": false,
                "top_k": 8,
                "adjacent_chunk_expansion": true
            }
        }
    """
```

### 6.6 文档管理

#### 6.6.1 集合管理

```python
def list_collections(self) -> list[dict[str, Any]]:
    """列出所有集合

    Returns:
        [{"collection": "default", "document_count": 5, "chunk_count": 120}]
    """
```

#### 6.6.2 文档列表

```python
def list_documents(self, collection: str | None = None) -> list[dict[str, Any]]:
    """列出指定集合的文档"""
```

#### 6.6.3 文档详情

```python
def get_document_detail(self, doc_id: str) -> dict[str, Any] | None:
    """获取文档详情，包含所有分块

    Returns:
        {
            "document": DocumentManifest,
            "chunks": [KnowledgeChunk, ...]
        }
    """
```

#### 6.6.4 删除功能

```python
def delete_document(
    self,
    *,
    doc_id: str | None = None,
    collection: str | None = None,
    source_uri: str | None = None,
    remove_source: bool = False,
) -> dict[str, Any]:
    """删除文档及其分块

    Args:
        remove_source: 是否同时删除源文件
    """

def delete_source_file(self, source_uri: str) -> dict[str, Any]:
    """删除源文件及其索引"""
```

## 7. Agent 工具集成 (tools.py)

### 7.1 工具定义

```python
def build_knowledge_tools(
    *,
    workspace_root: str | Path,
    config_overrides: dict[str, Any] | None = None,
) -> list[Any]:
    """构建知识库工具集

    返回的工具：
    1. knowledge_import_files - 导入文件
    2. knowledge_sync - 同步知识库
    3. knowledge_index_files - 索引文件
    4. knowledge_search - 搜索
    5. knowledge_list_collections - 列出集合
    6. knowledge_list_documents - 列出文档
    7. knowledge_delete_document - 删除文档
    8. knowledge_evaluate - 运行评估
    9. knowledge_download_models - 下载模型
    """
```

### 7.2 工具参数

```python
class KnowledgeImportFilesInput(BaseModel):
    collection: str = Field("default", description="集合名称")
    paths: list[str] = Field(..., description="文件路径列表")

class KnowledgeSyncInput(BaseModel):
    collection: str | None = Field(None, description="集合名称")
    dry_run: bool = Field(False, description="是否预览模式")

class KnowledgeSearchInput(BaseModel):
    query: str = Field(..., description="搜索查询")
    collection: str | None = Field(None, description="集合过滤")
    filters: dict[str, Any] | None = Field(None, description="元数据过滤")
    top_k: int = Field(8, description="返回结果数")
    include_scores: bool = Field(True, description="是否包含分数")
```

## 8. 评估系统 (evaluation.py)

### 8.1 评估框架

```python
class EvaluationHarness:
    """评估框架

    支持的评估指标：
    - Retrieval: NDCG@10, Recall, MRR, MAP
    - Answer: Exact Match, Token F1, Citation Support
    """
```

### 8.2 评估数据集

```python
class BenchmarkDataset:
    """基准数据集

    字段：
    - dataset_id: 数据集 ID
    - kind: 类型（retrieval/answer）
    - corpus: 语料库 {doc_id: text}
    - queries: 查询 {query_id: query_text}
    - qrels: 相关性判断 {query_id: {doc_id: relevance}}
    """
```

## 9. 复现指南

### 9.1 环境准备

```bash
# 安装依赖
pip install pypdf python-docx beautifulsoup4
```

### 9.2 配置 RAG

在 `runtime_config.json` 中添加：

```json
{
  "rag": {
    "enabled": true,
    "knowledge_store_path": "knowledge_base",
    "embedding_provider": "local_hashing",
    "chunking": {
      "target_tokens": 500,
      "max_tokens": 800,
      "overlap_ratio": 0.12
    },
    "hybrid": {
      "fusion_strategy": "rrf",
      "top_k": 8
    }
  }
}
```

### 9.3 基本使用流程

```python
from backend.rag.service import PersonalKnowledgeBase
from backend.rag.config import load_rag_config

# 1. 初始化知识库
config = load_rag_config()
kb = PersonalKnowledgeBase(
    workspace_root="/path/to/workspace",
    store_path=config.knowledge_store_path,
    chunker_config=config.chunking,
)

# 2. 导入文件
result = kb.import_files("default", ["doc1.txt", "doc2.pdf"])

# 3. 同步索引
sync_result = kb.sync()

# 4. 搜索
search_result = kb.search("查询内容", top_k=5)
```

### 9.4 Agent 集成

```python
from backend.rag.tools import build_knowledge_tools

# 构建工具集
tools = build_knowledge_tools(
    workspace_root="/path/to/workspace",
    config_overrides={"enabled": True},
)

# 工具会自动集成到 Agent
```

## 10. 性能优化建议

### 10.1 分块策略调优

- **target_tokens**: 根据文档类型调整
  - 技术文档：400-600
  - 长篇文章：600-1000
  - 代码文档：200-400

- **overlap_ratio**: 根据上下文依赖调整
  - 高依赖：0.15-0.20
  - 中依赖：0.10-0.15
  - 低依赖：0.05-0.10

### 10.2 检索参数调优

- **top_k**: 根据应用场景调整
  - 精确问答：3-5
  - 综合分析：8-12
  - 探索性搜索：15-20

### 10.3 存储优化

- 对于大规模知识库，考虑：
  - 使用外部向量数据库（Milvus、Qdrant）
  - 使用外部搜索引擎（Elasticsearch）
  - 使用 HuggingFace 嵌入模型提升语义理解

## 11. 扩展开发

### 11.1 添加新的文件格式支持

在 `service.py` 的 `_read_supported_file` 方法中添加：

```python
if suffix == ".新格式":
    # 实现解析逻辑
    return parsed_text
```

并在 `_SUPPORTED_SUFFIXES` 中添加扩展名。

### 11.2 自定义分块器

```python
class CustomChunker:
    def chunk_text(self, text: str, **kwargs) -> list[KnowledgeChunk]:
        # 实现自定义分块逻辑
        pass
```

### 11.3 集成外部向量数据库

```python
class ExternalVectorBackend:
    def index(self, chunks: list[KnowledgeChunk]) -> None:
        # 实现向量存储
        pass

    def search(self, query_vector: list[float], top_k: int) -> list:
        # 实现向量检索
        pass
```

## 12. 测试用例

```python
# 测试分块
from backend.rag.chunking import StructureFirstSemanticChunker, SemanticChunkingConfig

chunker = StructureFirstSemanticChunker(
    SemanticChunkingConfig(target_tokens=100, max_tokens=200)
)
chunks = chunker.chunk_text("测试文本...")

# 测试检索
from backend.rag.service import PersonalKnowledgeBase

kb = PersonalKnowledgeBase(workspace_root=".", store_path="./test_store")
kb.import_files("default", ["test.txt"])
kb.sync()
results = kb.search("查询内容")
```

## 13. 常见问题

### Q1: PDF 解析失败

确保安装了 pypdf：
```bash
pip install pypdf
```

### Q2: Word 文档解析失败

确保安装了 python-docx：
```bash
pip install python-docx
```

### Q3: 检索结果不准确

尝试调整：
1. 增加 `overlap_ratio` 以保留更多上下文
2. 降低 `target_tokens` 以获得更细粒度的分块
3. 启用 `reranker` 以提升排序质量

### Q4: 如何使用外部嵌入模型

修改配置：
```json
{
  "rag": {
    "embedding_provider": "huggingface",
    "embedding_model": "BAAI/bge-m3",
    "embedding_dimension": 1024
  }
}
```

---

**文档版本**: v1.0
**最后更新**: 2026-07-29
**作者**: Chat Agent Team
