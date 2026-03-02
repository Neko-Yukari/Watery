# Phase 11 — 代码语义索引（Code Semantic Index） 详细任务分解

> **[ARCHITECT] 模式产出**  
> 日期：2026-03-02  
> 目标：为 Watery 项目构建一套零 LLM-Token 消耗的代码结构索引系统，  
> 使 AI（包括对话中的 Agent 和外部开发工具）能通过语义搜索在毫秒内  
> 精准定位到目标函数/类/模块的文件路径、行号范围和签名，  
> 大幅降低上下文注入的 Token 成本并提高修改准确率。
>
> **完成状态：组 A 🔲 | 组 B 🔲 | 组 C 🔲 | 组 D 🔲 | 组 E 🔲**

---

## 核心需求回顾

**用户核心诉求**：  
"我想要一种**数据库表**，AI 通过**语义匹配**定位到某个模块，然后得到**文件路径、代码行号、函数名**等辅助定位信息，从而降低 Token 消耗并加快 AI 阅读速度与准确率。"

**当前状态**：
1. AI 修改代码时需要 `read_file` 整个文件才能定位目标函数 → Token 消耗线性增长
2. 项目已有 25+ 个 Python 文件、100+ 个函数/类，AI 定位目标逻辑依赖"猜+搜"
3. 无任何结构化代码元数据索引，AI 每次都需要重新理解项目结构
4. 已有 ChromaDB 向量库基础设施（`memory_retriever.py`），可直接复用
5. 已有 SQLite + SQLModel 增量迁移机制（`db.py`），新表可无损接入

**翻译为工程需求**：
1. 使用 Python `ast` 模块解析项目所有 `.py` 文件，提取**类、函数、方法**的结构化元数据
2. 新增 `CodeSymbol` SQLite 表，存储 `(文件路径, 符号名, 类型, 起始行, 结束行, 签名, 文档字符串)`
3. 将符号的 `name + docstring + signature` 向量化存入 ChromaDB `code_index_vector` 集合
4. 提供 REST API：语义搜索 → 返回精准的文件路径 + 行号范围 + 符号签名
5. 提供全量重建 + 增量更新两种索引模式，**全程 AST 解析，零 LLM 调用**
6. 将索引系统封装为 Skill，Agent 对话时可自主调用查询代码结构

---

## 成本分析：索引维护的 Token 消耗

| 操作 | 机制 | LLM Token 消耗 | 耗时 |
|------|------|----------------|------|
| 全量索引重建 | Python `ast` 解析所有 .py 文件 | **0** | ~200ms（25 个文件） |
| 增量更新（单文件） | 检测文件 hash 变化 → 重新解析该文件 | **0** | ~5ms |
| 语义搜索查询 | ChromaDB 内置 Embedding 向量匹配 | **0** | ~10ms |
| 启动时自动索引 | 应用启动时扫描一次 | **0** | ~300ms |

**结论**：整套索引系统的构建和维护完全不消耗 LLM Token。  
ChromaDB 使用内置的 `all-MiniLM-L6-v2` 模型进行本地 Embedding，无需调用外部 API。

---

## 功能分组 & 优先级

| 组 | 名称 | 优先级 | 阻塞关系 |
|---|------|--------|---------|
| **A** | CodeSymbol 数据模型 + DB 迁移 | P0 | 无阻塞，可独立开发 |
| **B** | AST 解析引擎 + 索引构建服务 | P0 | 依赖 A（DB 模型） |
| **C** | REST API（查询 + 管理） | P0 | 依赖 A + B |
| **D** | 自动化维护（启动索引 + 文件变更监听） | P1 | 依赖 B |
| **E** | Agent Skill 封装（对话中可调用） | P1 | 依赖 C（API 就绪） |

**建议实施顺序**：A → B → C → D → E

---

## 组 A — CodeSymbol 数据模型 + DB 迁移

### 当前状态

- `app/models/database.py` 中有 `Task`、`SkillMetadata`、`Conversation`、`ConversationMessage`、`RuntimeSetting`、`PDFDocument`、`ErrorEntry` 七张表
- `app/core/db.py` 的 `_migrate_schema()` 已实现增量 `ALTER TABLE ADD COLUMN` 模式
- ChromaDB 已有 `skills_vector`、`error_ledger_vector` 两个集合
- 无任何代码结构索引相关的数据模型

### 目标

新增 `CodeSymbol` SQLite 表 + `code_index_vector` ChromaDB 集合，存储项目代码的结构化元数据。

---

### 任务 A-1：新增 CodeSymbol 数据模型

**改动文件**：`app/models/database.py`

**新增模型**（追加到文件末尾，`ErrorEntry` 类之后）：
```python
class SymbolType(str, Enum):
    """代码符号类型。"""
    MODULE = "module"          # 文件/模块级
    CLASS = "class"            # 类定义
    FUNCTION = "function"      # 顶层函数
    METHOD = "method"          # 类方法
    GLOBAL_VAR = "global_var"  # 模块级常量/变量


class CodeSymbol(SQLModel, table=True):
    """
    代码符号索引表。通过 AST 解析自动生成，零 LLM Token 消耗。

    每条记录代表一个可寻址的代码符号（函数、类、方法等），
    携带文件路径、行号范围、函数签名、文档字符串等定位信息。
    AI 通过语义搜索命中后，可直接用 (file_path, line_start, line_end) 精准读取代码。
    """
    id: str = Field(
        default_factory=lambda: str(uuid.uuid4()),
        primary_key=True,
    )
    file_path: str = Field(
        index=True,
        description="相对于项目根目录的文件路径，如 'app/services/worker.py'",
    )
    symbol_name: str = Field(
        index=True,
        description="符号名称，如 'WorkerAgent.execute_task'",
    )
    symbol_type: str = Field(
        description="符号类型：module / class / function / method / global_var",
    )
    parent_symbol: Optional[str] = Field(
        default=None,
        description="父符号名（方法所属的类名），顶层符号为 None",
    )
    line_start: int = Field(
        description="起始行号（1-based，含）",
    )
    line_end: int = Field(
        description="结束行号（1-based，含）",
    )
    signature: str = Field(
        default="",
        description="函数/方法签名，如 'async def execute_task(self, task_id: str) -> Dict'",
    )
    docstring: str = Field(
        default="",
        description="文档字符串（Docstring），用于语义搜索",
    )
    decorators: List[str] = Field(
        default_factory=list,
        sa_column=Column(JSON),
        description="装饰器列表，如 ['@router.post(\"/chat\")']",
    )
    imports: List[str] = Field(
        default_factory=list,
        sa_column=Column(JSON),
        description="该符号依赖的导入模块（仅 module 级别记录）",
    )
    file_hash: str = Field(
        default="",
        description="源文件的 SHA-256 哈希（用于增量更新检测）",
    )
    indexed_at: Optional[datetime] = Field(
        default_factory=datetime.utcnow,
        description="索引时间",
    )
```

**设计决策**：
- `file_path` 使用项目相对路径（而非绝对路径），保证 Docker 内外一致
- `symbol_name` 对于方法使用 `ClassName.method_name` 格式，全局唯一
- `line_start` / `line_end` 为 1-based（与编辑器行号一致），AI 可直接用于 `read_file`
- `signature` 保留完整函数签名（含参数类型和返回类型），AI 无需读取代码即可了解接口
- `docstring` 是语义搜索的核心字段，ChromaDB Embedding 基于此字段
- `file_hash` 用于增量更新：hash 未变 → 跳过解析，避免冗余计算
- `decorators` 记录装饰器信息，帮助 AI 快速判断路由端点、属性等
- `imports` 仅在 module 级别记录，用于依赖关系分析

---

### 任务 A-2：DB 迁移

**改动文件**：`app/core/db.py`

**改动点**：
```python
migrations = [
    # ... 已有迁移条目 ...
    # Phase 11 — 代码语义索引
    ("codesymbol", "decorators",    "JSON DEFAULT '[]'"),
    ("codesymbol", "imports",       "JSON DEFAULT '[]'"),
    ("codesymbol", "file_hash",     "TEXT DEFAULT ''"),
    ("codesymbol", "indexed_at",    "DATETIME"),
]
```

**验证**：
- `docker-compose up --build` 后 `PRAGMA table_info(codesymbol)` 包含所有列
- 新增表不影响已有 7 张表

---

### 任务 A-3：新增 CodeSymbol 相关 Schema

**改动文件**：`app/models/schemas.py`

**新增模型**：
```python
class CodeSymbolResponse(BaseModel):
    """代码符号查询结果（单条）。"""
    file_path: str
    symbol_name: str
    symbol_type: str
    line_start: int
    line_end: int
    signature: str
    docstring: str
    decorators: List[str] = []
    parent_symbol: Optional[str] = None
    relevance_score: float = 0.0  # 语义搜索相关度（0-1，越高越相关）


class CodeSearchRequest(BaseModel):
    """代码语义搜索请求。"""
    query: str = Field(..., description="搜索查询（自然语言描述），如 '处理 PDF 上传的函数'")
    top_k: int = Field(default=5, ge=1, le=20, description="返回最相关的前 K 条结果")
    symbol_types: Optional[List[str]] = Field(
        default=None,
        description="过滤符号类型，如 ['function', 'method']，不传则不过滤",
    )
    file_pattern: Optional[str] = Field(
        default=None,
        description="文件路径模式过滤，如 'app/services/' 只搜索 services 目录",
    )


class CodeSearchResponse(BaseModel):
    """代码语义搜索响应。"""
    results: List[CodeSymbolResponse]
    total_indexed: int = Field(description="索引中的总符号数")
    query: str


class IndexStatusResponse(BaseModel):
    """索引状态响应。"""
    total_files: int
    total_symbols: int
    last_indexed_at: Optional[str] = None
    stale_files: int = Field(description="文件已变更但索引未更新的文件数")
```

---

### 组 A 验证清单

- [ ] `codesymbol` 表创建成功，包含所有字段
- [ ] 已有 7 张表不受影响
- [ ] Schema 模型可正常序列化/反序列化

---

## 组 B — AST 解析引擎 + 索引构建服务

### 当前状态

- Python 标准库 `ast` 模块无需安装额外依赖
- 项目所有 Python 代码集中在 `app/`、`ms_agent/`、`projects/`、`skills/`、`scripts/` 目录下
- 无任何 AST 解析相关代码

### 目标

实现 `CodeIndexer` 服务类，能扫描项目目录、AST 解析每个 `.py` 文件、提取符号元数据、写入 SQLite + ChromaDB。

---

### 任务 B-1：AST 解析器核心

**新增文件**：`app/services/code_indexer.py`

**核心类设计**：
```python
class CodeIndexer:
    """
    代码语义索引引擎。

    职责：
    1. 扫描项目目录下所有 .py 文件
    2. 使用 Python ast 模块解析，提取类/函数/方法/常量
    3. 写入 SQLite (CodeSymbol 表) + ChromaDB (code_index_vector 集合)
    4. 支持全量重建和增量更新（基于文件 SHA-256 hash 对比）

    零 LLM 消耗：全流程为纯规则解析 + 本地 Embedding，不调用任何大模型 API。
    """

    # 需要索引的目录（相对于项目根）
    INDEX_DIRS = ["app", "ms_agent", "scripts"]

    # 需要排除的目录模式
    EXCLUDE_PATTERNS = ["__pycache__", ".git", "node_modules", "data", ".venv"]

    def __init__(self):
        self.chroma_client = get_chroma_client()
        self.code_col = self.chroma_client.get_or_create_collection(
            name="code_index_vector"
        )
        self._project_root = self._detect_project_root()
```

**核心方法**：

```python
def _parse_file(self, file_path: str) -> List[CodeSymbol]:
    """
    解析单个 Python 文件，返回 CodeSymbol 列表。

    解析流程：
    1. 读取文件内容，计算 SHA-256 hash
    2. ast.parse() 生成 AST
    3. 遍历 ast.ClassDef、ast.FunctionDef、ast.AsyncFunctionDef
    4. 提取 name、lineno、end_lineno、decorator_list、docstring
    5. 对方法类型，记录 parent_symbol（所属类名）
    6. 构建函数签名字符串（含参数类型注解和返回类型）
    """

def _extract_signature(self, node: ast.FunctionDef) -> str:
    """
    从 AST 函数节点提取完整签名。

    示例输出：
    - 'async def generate(self, messages: List[Message], model: str, tools: Optional[List]) -> Dict'
    - 'def _sanitize_name(skill_id: str) -> str'
    """

def _extract_imports(self, tree: ast.Module) -> List[str]:
    """
    提取模块级导入列表。

    示例输出：['fastapi', 'sqlmodel', 'app.services.model_router']
    """

async def build_full_index(self) -> Dict[str, int]:
    """
    全量索引重建。

    流程：
    1. 清空 SQLite CodeSymbol 表
    2. 清空 ChromaDB code_index_vector 集合
    3. 扫描 INDEX_DIRS 下所有 .py 文件
    4. 逐文件 AST 解析 → 写入 SQLite + ChromaDB
    5. 返回统计信息 {"files_scanned": N, "symbols_indexed": M, "elapsed_ms": T}
    """

async def update_incremental(self) -> Dict[str, int]:
    """
    增量索引更新。

    流程：
    1. 扫描所有 .py 文件，计算每个文件的 SHA-256
    2. 与 SQLite 中存储的 file_hash 对比
    3. hash 不同的文件 → 删除旧符号 → 重新解析写入
    4. 已删除的文件 → 清理其对应的所有符号
    5. hash 相同的文件 → 跳过
    6. 返回 {"updated_files": N, "new_symbols": M, "removed_symbols": K}
    """

async def search(
    self,
    query: str,
    top_k: int = 5,
    symbol_types: Optional[List[str]] = None,
    file_pattern: Optional[str] = None,
) -> List[Dict]:
    """
    语义搜索代码符号。

    流程：
    1. ChromaDB 向量搜索 top_k * 2 个候选（扩大召回）
    2. 按 symbol_types 和 file_pattern 过滤
    3. 从 SQLite 补充完整元数据（行号、签名等）
    4. 截断至 top_k 返回

    返回格式（AI 友好）：
    [
        {
            "file_path": "app/api/routes.py",
            "symbol_name": "chat_endpoint",
            "symbol_type": "function",
            "line_start": 68,
            "line_end": 240,
            "signature": "async def chat_endpoint(request: ChatRequest)",
            "docstring": "通过 ModelRouter 动态路由到合适的模型提供商...",
            "relevance_score": 0.92
        },
        ...
    ]
    """

def get_index_status(self) -> Dict:
    """
    获取索引状态摘要。

    返回：{"total_files": N, "total_symbols": M, "stale_files": K, "last_indexed_at": "..."}
    """
```

**实现要点**：

1. **AST 解析的容错性**：
   ```python
   try:
       tree = ast.parse(source, filename=file_path)
   except SyntaxError as e:
       logger.warning(f"AST parse failed for {file_path}: {e}")
       return []  # 跳过语法错误的文件，不中断全量索引
   ```

2. **签名提取的完整性**：
   ```python
   def _extract_signature(self, node) -> str:
       # 处理 async def
       prefix = "async def" if isinstance(node, ast.AsyncFunctionDef) else "def"
       # 提取参数（含类型注解）
       args = []
       for arg in node.args.args:
           arg_str = arg.arg
           if arg.annotation:
               arg_str += f": {ast.unparse(arg.annotation)}"
           args.append(arg_str)
       # 提取返回类型
       returns = ""
       if node.returns:
           returns = f" -> {ast.unparse(node.returns)}"
       return f"{prefix} {node.name}({', '.join(args)}){returns}"
   ```

3. **ChromaDB Embedding 文档构建**：
   ```python
   # 将 name + signature + docstring 拼接为 Embedding 文档
   # 这样语义搜索既能匹配函数名，也能匹配功能描述
   embed_doc = f"{symbol.symbol_name}\n{symbol.signature}\n{symbol.docstring}"
   ```

4. **SQLite 写入批量优化**：
   ```python
   # 每个文件的所有符号一次性 bulk insert，减少 DB 事务开销
   with Session(engine) as session:
       session.add_all(symbols_for_file)
       session.commit()
   ```

**设计决策**：
- `INDEX_DIRS` 默认只索引 `app/`、`ms_agent/`、`scripts/` 核心目录，排除 `projects/`（外部项目代码庞大且非核心）和 `skills/scripts/`（运行时脚本，结构简单）
- ChromaDB 搜索时 `top_k * 2` 扩大召回后再过滤，保证 type/pattern 过滤后结果数量足够
- 增量更新基于文件级 hash 检测（而非行级 diff），实现简单且覆盖所有变更场景
- 全局变量/常量仅索引 `UPPER_CASE` 命名的模块级赋值，避免噪音

**验证**：
- 调用 `build_full_index()` → 返回正确的文件数和符号数
- 修改一个文件后调用 `update_incremental()` → 只更新该文件的符号
- `search("处理PDF上传")` → 命中 `routes.py::upload_pdf()`

---

### 任务 B-2：项目根目录检测

**改动文件**：`app/services/code_indexer.py`（同上文件）

**改动点**：
```python
def _detect_project_root(self) -> str:
    """
    自动检测项目根目录。

    策略（按优先级）：
    1. 环境变量 PROJECT_ROOT（Docker 中显式设置）
    2. 向上搜索 docker-compose.yml / .git 所在目录
    3. 回退到 /app（Docker 容器默认工作目录）
    """
    # Docker 容器内
    if os.environ.get("PROJECT_ROOT"):
        return os.environ["PROJECT_ROOT"]
    # 向上搜索标志文件
    current = os.path.dirname(os.path.abspath(__file__))
    for _ in range(5):  # 最多向上 5 层
        if os.path.exists(os.path.join(current, "docker-compose.yml")):
            return current
        current = os.path.dirname(current)
    return "/app"  # Docker 默认
```

**设计决策**：
- Docker 容器中项目根为 `/app`，宿主机开发时根为 `watery/`
- 环境变量优先，保证显式控制

---

### 组 B 验证清单

- [ ] 对 `app/services/model_router.py` 单文件解析 → 提取出 `ModelRouter` 类及其所有方法
- [ ] 全量索引 → 25+ 文件全部成功解析（含容错跳过）
- [ ] 增量索引 → 仅修改文件被重新解析
- [ ] `search("Tool Calling 循环")` → 命中 `routes.py::chat_endpoint`
- [ ] `search("代理节点筛选")` → 命中 `proxy_manager.py` 相关函数
- [ ] 搜索耗时 < 50ms

---

## 组 C — REST API（查询 + 管理）

### 当前状态

- `app/api/routes.py` 已有 `/chat`、`/skills`、`/tasks`、`/pdf`、`/errors`、`/ms-agent` 等路由组
- 无代码索引相关的 API 端点

### 目标

新增 `/code-index` 路由组，提供语义搜索、索引管理、状态查询。

---

### 任务 C-1：代码搜索端点

**改动文件**：`app/api/routes.py`

**新增端点**：

```python
@router.post("/code-index/search", response_model=CodeSearchResponse, summary="语义搜索代码符号")
async def search_code(request: CodeSearchRequest):
    """
    通过自然语言查询定位项目代码中的函数/类/方法。

    AI 友好：返回文件路径 + 精确行号范围 + 函数签名，
    可直接用于 read_file(file_path, line_start, line_end) 精准读取。

    示例请求：
    {
        "query": "处理 PDF 上传并校验文件大小",
        "top_k": 3,
        "symbol_types": ["function", "method"]
    }

    示例响应：
    {
        "results": [
            {
                "file_path": "app/api/routes.py",
                "symbol_name": "upload_pdf",
                "symbol_type": "function",
                "line_start": 450,
                "line_end": 510,
                "signature": "async def upload_pdf(file: UploadFile)",
                "docstring": "上传 PDF 文件，流式写盘...",
                "relevance_score": 0.94
            }
        ],
        "total_indexed": 156,
        "query": "处理 PDF 上传并校验文件大小"
    }
    """
    results = await code_indexer.search(
        query=request.query,
        top_k=request.top_k,
        symbol_types=request.symbol_types,
        file_pattern=request.file_pattern,
    )
    total = code_indexer.get_index_status()["total_symbols"]
    return CodeSearchResponse(
        results=[CodeSymbolResponse(**r) for r in results],
        total_indexed=total,
        query=request.query,
    )
```

---

### 任务 C-2：索引管理端点

**改动文件**：`app/api/routes.py`

**新增端点**：

```python
@router.post("/code-index/rebuild", summary="全量重建代码索引")
async def rebuild_code_index():
    """
    清空并重建整个代码语义索引。

    全流程 AST 解析，零 LLM Token 消耗。通常 200ms 内完成。
    """
    stats = await code_indexer.build_full_index()
    return {"status": "ok", "stats": stats}


@router.post("/code-index/update", summary="增量更新代码索引")
async def update_code_index():
    """
    检测文件变更并增量更新索引。

    仅重新解析 hash 发生变化的文件，通常 < 10ms。
    """
    stats = await code_indexer.update_incremental()
    return {"status": "ok", "stats": stats}


@router.get("/code-index/status", response_model=IndexStatusResponse, summary="查询索引状态")
async def get_code_index_status():
    """返回索引统计信息：文件数、符号数、过期文件数。"""
    status = code_indexer.get_index_status()
    return IndexStatusResponse(**status)


@router.get("/code-index/symbols", summary="列出指定文件的所有符号")
async def list_file_symbols(file_path: str):
    """
    列出指定文件中的所有已索引符号。

    用于 AI 快速了解一个文件的结构概览（"目录页"），
    无需读取整个文件内容。

    示例：GET /code-index/symbols?file_path=app/services/worker.py
    """
    with Session(engine) as session:
        symbols = session.exec(
            select(CodeSymbol)
            .where(CodeSymbol.file_path == file_path)
            .order_by(CodeSymbol.line_start)
        ).all()
    return {
        "file_path": file_path,
        "symbols": [
            {
                "symbol_name": s.symbol_name,
                "symbol_type": s.symbol_type,
                "line_start": s.line_start,
                "line_end": s.line_end,
                "signature": s.signature,
            }
            for s in symbols
        ],
    }
```

---

### 任务 C-3：代码索引服务实例化

**改动文件**：`app/api/routes.py`（顶部 import 区域）

**改动点**：

```python
from app.services.code_indexer import code_indexer  # Phase 11 代码语义索引
```

同时在 `app/services/code_indexer.py` 底部导出单例：

```python
# 模块级单例
code_indexer = CodeIndexer()
```

---

### 组 C 验证清单

- [ ] `POST /code-index/rebuild` → 返回 `{"status": "ok", "stats": {"files_scanned": N, ...}}`
- [ ] `POST /code-index/search` — `{"query": "对话聊天接口"}` → 命中 `chat_endpoint`
- [ ] `POST /code-index/search` — `{"query": "PDF处理", "symbol_types": ["class"]}` → 类过滤生效
- [ ] `GET /code-index/status` → 返回正确的统计信息
- [ ] `GET /code-index/symbols?file_path=app/services/worker.py` → 列出该文件所有函数
- [ ] 搜索空结果 → 返回空列表（不报错）

---

## 组 D — 自动化维护（启动索引 + 文件变更检测）

### 当前状态

- `app/main.py` 使用 `lifespan` 异步上下文管理器处理启动/关闭事件
- 已有启动时的 Skills 加载、Worker 启动等初始化逻辑
- 无文件变更监听机制

### 目标

1. 应用启动时自动执行增量索引更新
2. 提供可选的文件监听后台任务（开发环境下生效）

---

### 任务 D-1：启动时自动索引

**改动文件**：`app/main.py`

**改动点**：

在 `lifespan()` 的启动阶段，已有初始化步骤之后追加：

```python
# Phase 11 — 代码语义索引：启动时增量更新
from app.services.code_indexer import code_indexer
try:
    stats = await code_indexer.update_incremental()
    logger.info(f"Code index updated: {stats}")
except Exception as e:
    logger.warning(f"Code index update failed (non-fatal): {e}")
```

**设计决策**：
- 使用增量更新（而非全量重建），首次启动自动全量，后续启动仅处理变更文件
- `try/except` 包裹：索引失败不影响主服务启动（非致命错误）
- 启动时间增加 < 500ms（可接受）

---

### 任务 D-2：文件变更监听后台任务（可选）

**改动文件**：`app/services/code_indexer.py`

**新增方法**：

```python
async def start_file_watcher(self, interval: float = 30.0):
    """
    后台定期检查文件变更并增量更新索引。

    不使用 watchdog 库（减少依赖），改用简单的定时轮询 + hash 对比。
    interval 默认 30 秒（开发环境）。

    适用场景：
    - Docker 开发环境下代码通过 volume mount 实时同步
    - AI Agent 通过 skill_crud 动态修改代码后，索引自动跟进
    """
    logger.info(f"Code index file watcher started (interval={interval}s)")
    while True:
        await asyncio.sleep(interval)
        try:
            stats = await self.update_incremental()
            if stats.get("updated_files", 0) > 0:
                logger.info(f"Code index auto-updated: {stats}")
        except Exception as e:
            logger.warning(f"Code index auto-update failed: {e}")
```

**改动文件**：`app/main.py`

在 `lifespan()` 中追加：

```python
# Phase 11 — 代码索引文件监听（仅开发环境启用）
if settings.environment == "development":
    watcher_task = asyncio.create_task(code_indexer.start_file_watcher(interval=30.0))
    _track_task(watcher_task)  # 防 GC
```

**设计决策**：
- **不引入 `watchdog` 依赖**：定时轮询 + hash 对比已够用，且 Docker 容器内 inotify 不可靠
- 仅 `development` 环境启用，生产环境通过 API 手动触发（`POST /code-index/update`）
- 30 秒间隔：在及时性和性能之间取平衡
- 轮询失败不影响主服务

---

### 任务 D-3：API 触发点整合

当现有的代码修改入口被调用时，自动触发增量索引：

**改动文件**：`app/api/routes.py`

**改动点**：在 Skills CRUD 端点（`POST /skills`、`PUT /skills/{id}`、`DELETE /skills/{id}`）的成功返回前追加：

```python
# 技能变更可能涉及代码文件修改，触发索引增量更新
asyncio.create_task(code_indexer.update_incremental())
```

**设计决策**：
- fire-and-forget 模式，不阻塞 API 响应
- Skills CRUD 是项目内最常见的代码变更入口
- 手动编辑的文件变更由 D-2 的定时轮询覆盖

---

### 组 D 验证清单

- [ ] 容器启动日志中出现 `Code index updated: {files_scanned: ...}`
- [ ] 修改一个 .py 文件 → 30 秒内索引自动更新（开发环境）
- [ ] Skills CRUD 后索引立即更新
- [ ] 索引失败时主服务正常运行，日志有 warning

---

## 组 E — Agent Skill 封装（对话中可调用）

### 当前状态

- Phase 10 已实现 Agent System Prompt + Tool Calling 循环
- Agent 对话中可调用 `web_search`、`skill_crud` 等已注册技能
- 无代码索引查询技能

### 目标

将代码索引搜索封装为 `code_search` Skill，使 Agent 在对话中遇到"帮我找到处理 XX 的代码"类请求时，可自主调用索引而非盲目 `read_file`。

---

### 任务 E-1：code_search Skill 定义

**新增文件**：`skills/code_search/SKILL.md`

```yaml
---
id: code_search
name: 代码语义搜索
description: |
  在项目代码库中进行语义搜索，定位函数、类、方法的精确位置。
  返回文件路径、行号范围、函数签名等信息，AI 可据此精准读取代码片段。
  适用于：查找某个功能的实现位置、了解某个模块的结构、定位需要修改的代码。
  注意：此技能搜索的是项目自身的代码，而非互联网内容。
language: python
entrypoint: scripts/main.py
parameters_schema:
  type: object
  properties:
    query:
      type: string
      description: |
        自然语言搜索查询。描述你要找的代码功能，如：
        - "处理 PDF 上传的函数"
        - "ChromaDB 向量检索"
        - "对话消息持久化"
    top_k:
      type: integer
      description: 返回结果数量（默认 5）
    symbol_types:
      type: array
      items:
        type: string
      description: "过滤符号类型：function / method / class / module"
    file_pattern:
      type: string
      description: "文件路径前缀过滤，如 'app/services/'"
  required:
    - query
tags:
  - code
  - search
  - development
  - index
---

## 使用说明

当你需要在项目代码中定位某个功能的实现时，使用此技能。

### 返回格式

每个结果包含：
- `file_path`：相对文件路径
- `symbol_name`：函数/类/方法名
- `line_start` / `line_end`：精确行号范围
- `signature`：完整函数签名
- `docstring`：文档字符串
- `relevance_score`：相关度评分（0-1）

### 最佳实践

1. 先用自然语言描述你要找的功能
2. 拿到结果后，用 `file_path` + `line_start` ~ `line_end` 精准读取代码
3. 如果结果不够精确，尝试换一种描述或缩小 `file_pattern` 范围
```

---

### 任务 E-2：code_search Skill 脚本

**新增文件**：`skills/code_search/scripts/main.py`

```python
"""
代码语义搜索技能脚本。

通过调用本地 API /code-index/search 实现，保持 Skill 进程隔离。
"""
import json
import sys
import httpx

_API_BASE = "http://localhost:18000/api/v1"


def main():
    params = json.loads(sys.argv[1]) if len(sys.argv) > 1 else {}
    query = params.get("query", "")
    if not query:
        print(json.dumps({"error": "query 参数不能为空"}))
        sys.exit(1)

    payload = {
        "query": query,
        "top_k": params.get("top_k", 5),
    }
    if params.get("symbol_types"):
        payload["symbol_types"] = params["symbol_types"]
    if params.get("file_pattern"):
        payload["file_pattern"] = params["file_pattern"]

    resp = httpx.post(f"{_API_BASE}/code-index/search", json=payload, timeout=10)
    resp.raise_for_status()
    data = resp.json()

    # 格式化为 AI 友好的紧凑输出
    output_lines = [f"共索引 {data['total_indexed']} 个符号，"
                    f"找到 {len(data['results'])} 个匹配：\n"]
    for i, r in enumerate(data["results"], 1):
        output_lines.append(
            f"{i}. [{r['symbol_type']}] {r['symbol_name']}\n"
            f"   文件: {r['file_path']}  行: {r['line_start']}-{r['line_end']}\n"
            f"   签名: {r['signature']}\n"
            f"   说明: {r['docstring'][:100]}...\n"
            f"   相关度: {r['relevance_score']:.2f}\n"
        )
    print("\n".join(output_lines))


if __name__ == "__main__":
    main()
```

**设计决策**：
- 通过 HTTP 调用本地 API（而非直接 import），保持 Skill 的进程隔离原则
- 输出格式为 AI 友好的结构化文本（非 JSON），减少 AI 解析负担
- `docstring` 截断至 100 字符，避免工具结果过长爆上下文

---

### 任务 E-3：Agent System Prompt 更新

**改动文件**：`app/api/routes.py`

**改动点**：在 `_AGENT_SYSTEM_PROMPT` 的技能列表中追加：

```python
# 在已有技能列表末尾追加
- 🔍 **代码搜索** (`code_search`) — 在项目代码中语义搜索函数/类/方法的精确位置（文件路径+行号）
```

并在工作准则中追加：

```python
6. **代码定位优先**：当你需要查看或修改项目代码时，优先使用 `code_search` 定位目标函数，
   而非盲目读取整个文件。这可以大幅减少 Token 消耗。
```

---

### 组 E 验证清单

- [ ] `POST /skills/load-dir` 加载 `skills/code_search/` 成功
- [ ] 对话中说"帮我找到处理 PDF 上传的代码" → Agent 自动调用 `code_search`
- [ ] 返回的行号可直接用于 `read_file` 精准读取
- [ ] Agent 在回复中引用了具体的文件路径和行号

---

## 验证场景：端到端工作流

完成 A+B+C+D+E 后，以下场景应可在 AI 对话中完成：

```
用户: "帮我找到处理错题集 CRUD 的所有代码，我想了解一下结构"

Round 1 [Thought]: 用户需要了解错题集 CRUD 的代码结构，
       我先用 code_search 定位相关代码。
Round 2 [Action]: code_search(query="错题集 CRUD 创建 查询 删除", top_k=8)
Round 3 [Observation]:
       共索引 156 个符号，找到 8 个匹配：
       1. [function] create_error_entry
          文件: app/api/routes.py  行: 890-940
          签名: async def create_error_entry(entry: ErrorEntryCreate)
          相关度: 0.95
       2. [function] list_error_entries
          文件: app/api/routes.py  行: 942-970
          ...
       3. [class] ErrorEntry
          文件: app/models/database.py  行: 180-223
          ...
Round 4 [Done]: 向用户展示代码结构概览，包含文件位置和函数签名。

总 Token 消耗：~800（仅索引结果摘要）
传统方式消耗：~8000（读取整个 routes.py 2000+ 行）
节省：~90%
```

---

## 变更文件清单

| 文件 | 组 | 变更类型 |
|------|---|---------|
| `app/models/database.py` | A | 修改（新增 `SymbolType`、`CodeSymbol` 模型） |
| `app/core/db.py` | A | 修改（新增迁移条目） |
| `app/models/schemas.py` | A | 修改（新增 `CodeSymbolResponse`、`CodeSearchRequest` 等） |
| `app/services/code_indexer.py` | B, D | **新增**（核心：AST 解析引擎 + 索引构建 + 文件监听） |
| `app/api/routes.py` | C, D, E | 修改（新增 `/code-index/*` 端点 + Agent Prompt 更新） |
| `app/main.py` | D | 修改（启动时索引 + 文件监听任务） |
| `skills/code_search/SKILL.md` | E | **新增** |
| `skills/code_search/scripts/main.py` | E | **新增** |

**不需要改动**：`requirements.txt`（无新依赖，`ast` 为标准库）、`config.py`（无新配置）、`Dockerfile`（无新系统包）

---

## 与原有系统的兼容性

| 场景 | 影响 |
|------|------|
| 已有对话功能 | ✅ 零影响（索引系统完全独立，不修改 `/chat` 核心逻辑） |
| 已有 Skills CRUD | ✅ 零影响（仅在成功后追加异步索引更新） |
| Worker DAG 任务链路 | ✅ 零影响（Worker 不感知索引系统） |
| ChromaDB 已有集合 | ✅ 零影响（新集合 `code_index_vector`，命名空间隔离） |
| 启动时间 | ⚡ 增加 < 500ms（首次全量索引 ~300ms） |
| 内存占用 | ⚡ 增加 < 5MB（SQLite 行 + ChromaDB 向量） |
| Docker 镜像大小 | ✅ 不变（无新依赖） |

---

## 未来扩展（不在本 Phase 范围内）

1. **跨语言支持**：引入 `tree-sitter` 解析 JavaScript/Shell/YAML 技能脚本
2. **调用关系图**：基于 AST 的 `import` 和函数调用分析，生成模块依赖 DAG
3. **前端代码地图**：在网页端可视化项目结构和函数调用关系
4. **Git 集成**：通过 `pre-commit` hook 在每次提交前自动更新索引
5. **AI 自动更新**：Agent 修改代码后主动调用 `POST /code-index/update`（目前由定时轮询覆盖）
