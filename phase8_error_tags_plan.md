# Phase 8 — 标签化错题集 + Skill 关联 + 网页查询 详细任务分解

> **[ARCHITECT] 模式产出**  
> 日期：2026-03-02  
> 目标：将错题集从「扁平 Markdown + 无分类向量」升级为「SQLite 结构化表 + 标签化向量索引」，  
> 实现 Skill ↔ Error 自动关联、按标签精确检索、网页端可视化查询，  
> 全流程由 AI 运行时自动完成标签生成与维护。
>
> **完成状态：组 A 🔲 | 组 B 🔲 | 组 C 🔲 | 组 D 🔲 | 组 E 🔲**

---

## 核心需求回顾

**当前状态**：
- `error_ledger.md` 是项目初期的手动记录文件，**不再维护**
- ChromaDB `error_ledger_vector` 集合：无标签 metadata，只存 `{"correction": title, "skill_id": "ledger"}`
- `fail_task()` 运行时写入的错题 `skill_id` 固定为 `"general"`，无分类
- Worker 检索时 `retrieve_context()` 纯语义模糊匹配 top-2，无法按领域过滤
- 前端「📓 错题集」只是原样渲染 `error_ledger.md` 全文，无搜索/筛选能力

**工程需求**：
1. 新增 `ErrorEntry` SQLite 表（带 `tags` JSON 数组列）
2. `SkillMetadata` 新增 `error_tags` 列（声明关联的错题标签）
3. Worker 执行技能前按 `error_tags` 精确筛选相关错题
4. `fail_task()` 时 AI 自动生成标签化错题条目
5. 技能注册时 AI 自动推断 `error_tags`
6. 前端网页提供错题查询界面（搜索 + 标签筛选 + 详情查看）
7. 启动时一次性将旧 `error_ledger.md` 和已有 ChromaDB 数据迁移到新表

---

## 功能分组 & 优先级

| 组 | 名称 | 优先级 | 阻塞关系 |
|---|------|--------|---------|
| **A** | ErrorEntry 数据模型 + DB 迁移 + SkillMetadata 扩展 | P0 | 无阻塞 |
| **B** | 错题 CRUD API + 旧数据迁移 | P0 | 依赖 A |
| **C** | Worker 链路改造（按 tag 精确检索错题） | P0 | 依赖 A + B |
| **D** | AI 自动化（fail_task 自动打标签 + 技能注册自动推断 error_tags） | P1 | 依赖 A + B |
| **E** | 前端错题查询界面 | P1 | 依赖 B（API 就绪） |

**建议实施顺序**：A → B → C → D → E

---

## 组 A — ErrorEntry 数据模型 + SkillMetadata 扩展

### 当前状态

- `app/models/database.py` 中有 `Task`、`SkillMetadata`、`PDFDocument`、`Conversation`、`ConversationMessage` 五张表
- `SkillMetadata` 已有 `tags` 列（JSON），但没有 `error_tags`
- 无任何错题相关的结构化数据表
- `app/core/db.py` 的 `_migrate_schema()` 已实现增量 `ALTER TABLE ADD COLUMN` 模式

### 任务 A-1：新增 ErrorEntry 数据模型

**改动文件**：`app/models/database.py`

**新增模型**（追加到文件末尾，`PDFDocument` 类之后）：
```python
class ErrorEntry(SQLModel, table=True):
    """
    结构化错题条目。替代旧的 error_ledger.md 纯文本存储。

    每条错题携带 tags 标签数组，支持按标签精确筛选。
    Worker 执行技能时，根据 Skill.error_tags 过滤相关错题注入上下文。
    """
    id: str = Field(
        default_factory=lambda: str(uuid.uuid4()),
        primary_key=True,
    )
    title: str = Field(
        description="简短标题（一句话概括错误）",
    )
    context: str = Field(
        description="触发错误的完整上下文描述（包括场景、操作、报错信息等）",
    )
    correction: str = Field(
        description="纠正方案（正确做法 / 解决步骤）",
    )
    prevention: str = Field(
        default="",
        description="预防建议（避免再犯的注意事项）",
    )
    tags: List[str] = Field(
        default_factory=list,
        sa_column=Column(JSON),
        description="标签数组，如 ['docker', 'python', 'proxy', 'encoding']",
    )
    severity: str = Field(
        default="warning",
        description="严重程度：critical / warning / info",
    )
    source: str = Field(
        default="auto",
        description="来源：manual（人工/迁移）/ auto（AI 生成）/ task_failure（任务失败自动记录）",
    )
    related_skill_ids: List[str] = Field(
        default_factory=list,
        sa_column=Column(JSON),
        description="关联的 Skill ID 数组（双向关联）",
    )
    hit_count: int = Field(
        default=0,
        description="被 Worker 命中并注入上下文的次数（越高越重要）",
    )
    created_at: Optional[datetime] = Field(
        default_factory=datetime.utcnow,
    )
```

**设计决策**：
- `tags` 使用 JSON 数组，ChromaDB 写入时同步存为 metadata（支持 `where` 过滤）
- `severity` 三档：`critical`（必须阅读）> `warning`（建议阅读）> `info`（参考）
- `source` 区分来源：便于统计人工 vs AI 生成的比例
- `hit_count` 追踪命中次数：高频命中的错题可以在 UI 中优先展示
- `related_skill_ids` 双向关联：错题知道自己和哪些技能相关

### 任务 A-2：SkillMetadata 新增 error_tags 列

**改动文件**：`app/models/database.py`

在 `SkillMetadata` 类中，`knowledge_content` 字段后追加：
```python
    # Phase 8 — 标签化错题集关联
    error_tags: List[str] = Field(
        default_factory=list,
        sa_column=Column(JSON),
        description="该技能执行时需要参考的错题标签列表，如 ['python', 'file-io', 'encoding']",
    )
```

**含义**：当 Worker 即将调用此 Skill 时，从 ErrorEntry 表中筛选 `tags` 包含这些标签的条目作为防错提示。

### 任务 A-3：DB 迁移

**改动文件**：`app/core/db.py`

在 `_migrate_schema()` 的 `migrations` 列表末尾追加：
```python
    # Phase 8 — 标签化错题集
    ("skillmetadata", "error_tags",          "JSON DEFAULT '[]'"),
    ("errorentry",    "prevention",          "TEXT DEFAULT ''"),
    ("errorentry",    "hit_count",           "INTEGER DEFAULT 0"),
    ("errorentry",    "related_skill_ids",   "JSON DEFAULT '[]'"),
```

**注意**：`ErrorEntry` 是全新表，`SQLModel.metadata.create_all(engine)` 会自动创建完整结构。这里的 migration 只预留未来迭代新增列。

### 任务 A-4：Schema 扩展

**改动文件**：`app/models/schemas.py`

**新增**（追加到文件末尾）：
```python
# ============================================================
# Phase 8 — 标签化错题集
# ============================================================

class ErrorEntryCreate(BaseModel):
    """POST /errors/entries 请求体（手动添加或 AI 自动生成）。"""
    title: str = Field(..., description="简短标题")
    context: str = Field(..., description="错误上下文")
    correction: str = Field(..., description="纠正方案")
    prevention: str = Field("", description="预防建议")
    tags: List[str] = Field(default_factory=list, description="标签数组")
    severity: str = Field("warning", description="critical / warning / info")
    source: str = Field("manual", description="manual / auto / task_failure")
    related_skill_ids: List[str] = Field(default_factory=list, description="关联 Skill ID")


class ErrorEntryInfo(BaseModel):
    """错题摘要（列表页使用）。"""
    id: str
    title: str
    tags: List[str] = []
    severity: str = "warning"
    source: str = "manual"
    hit_count: int = 0
    created_at: Optional[str] = None


class ErrorEntryDetail(BaseModel):
    """错题完整详情。"""
    id: str
    title: str
    context: str
    correction: str
    prevention: str = ""
    tags: List[str] = []
    severity: str = "warning"
    source: str = "manual"
    related_skill_ids: List[str] = []
    hit_count: int = 0
    created_at: Optional[str] = None
```

**SkillCreate 扩展**（在现有 `SkillCreate` 类中追加字段）：
```python
    # Phase 8 — 标签化错题集关联
    error_tags: List[str] = Field(
        default_factory=list,
        description="该技能关联的错题标签列表（不填时 AI 自动推断）",
    )
```

**SkillUpdate 扩展**（在现有 `SkillUpdate` 类中追加字段）：
```python
    error_tags: Optional[List[str]] = None
```

### 组 A 验证清单

- [ ] `docker-compose up --build` 后 `errorentry` 表存在且包含全部列
- [ ] `skillmetadata` 表含新列 `error_tags`
- [ ] 已有的 5 张旧表无回归

---

## 组 B — 错题 CRUD API + 向量同步 + 旧数据迁移

### 当前状态

- `GET /api/v1/errors` 返回 `error_ledger.md` 原文
- `POST /api/v1/errors/ingest` 将 md 解析后写入 ChromaDB
- `memory_retriever.add_error_entry()` 只写 ChromaDB，无结构化

### 目标

1. 完整的 ErrorEntry CRUD API（含标签筛选）
2. 写入 ErrorEntry 时同步写 ChromaDB（带 tags metadata）
3. 启动时一次性从 `error_ledger.md` 迁移旧数据到 ErrorEntry 表

### 任务 B-1：ErrorEntry CRUD API

**改动文件**：`app/api/routes.py`

**新增路由区块**（替换掉现有的 `GET /errors` 和 `POST /errors/ingest`）：
```
# ============================================================
# 错题集管理（Phase 8）
# ============================================================

POST   /errors/entries            → 创建错题条目
GET    /errors/entries            → 列表（支持 ?tags=python,docker 筛选 + ?search=关键词）
GET    /errors/entries/{id}       → 获取单条错题详情
DELETE /errors/entries/{id}       → 删除错题
GET    /errors/tags               → 列出所有已有标签及其出现频次
POST   /errors/migrate            → 一次性从 error_ledger.md 迁移旧数据（LLM 自动打标签）
```

**详细实现**：

#### `POST /errors/entries`
```python
@router.post("/errors/entries", status_code=201, summary="创建错题条目")
async def create_error_entry(request: ErrorEntryCreate):
    """
    创建一条结构化错题。同时写入 SQLite 和 ChromaDB（带 tags metadata）。
    来源可以是手动添加、AI 自动生成、或任务失败自动记录。
    """
    entry = ErrorEntry(
        title=request.title,
        context=request.context,
        correction=request.correction,
        prevention=request.prevention,
        tags=request.tags,
        severity=request.severity,
        source=request.source,
        related_skill_ids=request.related_skill_ids,
    )
    with Session(engine) as session:
        session.add(entry)
        session.commit()
        session.refresh(entry)
        entry_id = entry.id

    # 同步写入 ChromaDB（带 tags metadata 以支持 where 过滤）
    await memory_retriever.add_error_entry_v2(
        entry_id=entry_id,
        context=request.context,
        correction=request.correction,
        tags=request.tags,
        severity=request.severity,
    )

    return {"id": entry_id, "title": request.title, "tags": request.tags}
```

#### `GET /errors/entries`
```python
@router.get("/errors/entries", summary="列出错题条目（支持标签筛选 + 关键词搜索）")
async def list_error_entries(
    tags: Optional[str] = None,
    search: Optional[str] = None,
    severity: Optional[str] = None,
    limit: int = 50,
    offset: int = 0,
):
    """
    返回错题列表。

    - ``?tags=python,docker`` — 按标签筛选（OR 逻辑，命中任一标签即返回）
    - ``?search=关键词`` — 全文模糊搜索 title / context
    - ``?severity=critical`` — 按严重程度筛选
    - 默认按 hit_count DESC + created_at DESC 排序（高频命中优先）
    """
    with Session(engine) as session:
        stmt = select(ErrorEntry)

        # 标签筛选：SQLite JSON 列包含检测
        # 注意：SQLite 的 JSON 查询能力有限，这里用 Python 侧过滤
        entries = session.exec(stmt).all()

    # Python 侧过滤与排序
    result = list(entries)

    if tags:
        tag_list = [t.strip() for t in tags.split(",") if t.strip()]
        result = [e for e in result if any(t in (e.tags or []) for t in tag_list)]

    if severity:
        result = [e for e in result if e.severity == severity]

    if search:
        kw = search.lower()
        result = [e for e in result if kw in (e.title or "").lower() or kw in (e.context or "").lower()]

    # 排序：hit_count DESC → created_at DESC
    result.sort(key=lambda e: (e.hit_count or 0, e.created_at or ""), reverse=True)

    # 分页
    total = len(result)
    result = result[offset: offset + limit]

    return {
        "total": total,
        "entries": [
            {
                "id": e.id,
                "title": e.title,
                "tags": e.tags or [],
                "severity": e.severity,
                "source": e.source,
                "hit_count": e.hit_count or 0,
                "created_at": e.created_at.isoformat() if e.created_at else None,
            }
            for e in result
        ],
    }
```

#### `GET /errors/entries/{id}`
```python
@router.get("/errors/entries/{entry_id}", summary="获取错题详情")
async def get_error_entry(entry_id: str):
    with Session(engine) as session:
        entry = session.get(ErrorEntry, entry_id)
        if not entry:
            raise HTTPException(404, f"ErrorEntry '{entry_id}' not found.")
    return {
        "id": entry.id,
        "title": entry.title,
        "context": entry.context,
        "correction": entry.correction,
        "prevention": entry.prevention,
        "tags": entry.tags or [],
        "severity": entry.severity,
        "source": entry.source,
        "related_skill_ids": entry.related_skill_ids or [],
        "hit_count": entry.hit_count or 0,
        "created_at": entry.created_at.isoformat() if entry.created_at else None,
    }
```

#### `DELETE /errors/entries/{id}`
```python
@router.delete("/errors/entries/{entry_id}", summary="删除错题")
async def delete_error_entry(entry_id: str):
    with Session(engine) as session:
        entry = session.get(ErrorEntry, entry_id)
        if not entry:
            raise HTTPException(404, f"ErrorEntry '{entry_id}' not found.")
        session.delete(entry)
        session.commit()
    # 同步从 ChromaDB 删除
    await memory_retriever.delete_error_entry(entry_id)
    return {"status": "deleted", "id": entry_id}
```

#### `GET /errors/tags`
```python
@router.get("/errors/tags", summary="列出所有错题标签及频次")
async def list_error_tags():
    """返回所有标签及其出现次数，供前端标签云 / 筛选器使用。"""
    with Session(engine) as session:
        entries = session.exec(select(ErrorEntry)).all()
    tag_count = {}
    for e in entries:
        for t in (e.tags or []):
            tag_count[t] = tag_count.get(t, 0) + 1
    # 按频次降序
    sorted_tags = sorted(tag_count.items(), key=lambda x: x[1], reverse=True)
    return {"tags": [{"name": t, "count": c} for t, c in sorted_tags]}
```

**保留旧接口兼容**（不删除，标记废弃）：

旧 `GET /errors` 保留不动（返回 md 原文），旧 `POST /errors/ingest` 保留但追加逻辑——除了写 ChromaDB，同时迁移写入 ErrorEntry 表。

### 任务 B-2：memory_retriever 新增方法

**改动文件**：`app/services/memory_retriever.py`

新增三个方法：

```python
async def add_error_entry_v2(
    self,
    entry_id: str,
    context: str,
    correction: str,
    tags: List[str],
    severity: str = "warning",
) -> None:
    """
    将错题写入 ChromaDB error_ledger_vector 集合（带 tags metadata）。
    metadata 中存储 tags 的逗号拼接字符串（ChromaDB where 过滤用）
    和 severity。
    """
    doc_text = f"{context}\nCorrection: {correction}"
    # ChromaDB metadata 不支持数组，tags 存为逗号分隔字符串
    meta = {
        "correction": correction,
        "tags": ",".join(tags) if tags else "",
        "severity": severity,
        "entry_id": entry_id,
    }
    existing = await self._run_sync(self.error_ledger_col.get, ids=[entry_id])
    if existing["ids"]:
        await self._run_sync(
            self.error_ledger_col.update,
            ids=[entry_id],
            documents=[doc_text],
            metadatas=[meta],
        )
    else:
        await self._run_sync(
            self.error_ledger_col.add,
            ids=[entry_id],
            documents=[doc_text],
            metadatas=[meta],
        )


async def delete_error_entry(self, entry_id: str) -> None:
    """从 ChromaDB 删除错题条目。"""
    try:
        await self._run_sync(self.error_ledger_col.delete, ids=[entry_id])
    except Exception:
        pass


async def retrieve_errors_by_tags(
    self,
    task_description: str,
    tags: List[str],
    top_k: int = 3,
) -> List[Dict[str, Any]]:
    """
    按标签精确筛选 + 语义排序，返回与任务最相关的错题。

    流程：
    1. 构建 ChromaDB where 过滤条件：tags 字段包含指定标签（OR 逻辑）
    2. 在过滤后的候选集内做语义相似度排序
    3. 返回 top_k 条

    Args:
        task_description: 当前任务描述（用于语义排序）
        tags:             要筛选的标签列表（来自 Skill.error_tags）
        top_k:            返回条数

    Returns:
        [{"entry_id": ..., "context": ..., "correction": ..., "tags": ...}, ...]
    """
    error_count = await self._run_sync(self.error_ledger_col.count)
    if error_count == 0:
        return []

    # 构建 where 过滤：tags 字段包含任一指定标签
    # ChromaDB where 支持 $contains 操作符（匹配字符串子串）
    # 由于 tags 存为逗号分隔字符串，用 $or + $contains 实现 OR 逻辑
    if tags:
        where_filter = {
            "$or": [
                {"tags": {"$contains": tag}} for tag in tags
            ]
        } if len(tags) > 1 else {"tags": {"$contains": tags[0]}}
    else:
        where_filter = None

    k = min(top_k, error_count)
    try:
        results = await self._run_sync(
            self.error_ledger_col.query,
            query_texts=[task_description],
            n_results=k,
            where=where_filter,
            include=["documents", "metadatas"],
        )
    except Exception as e:
        logger.warning(f"retrieve_errors_by_tags failed: {e}, falling back to unfiltered")
        results = await self._run_sync(
            self.error_ledger_col.query,
            query_texts=[task_description],
            n_results=k,
            include=["documents", "metadatas"],
        )

    entries = []
    if results["ids"] and results["ids"][0]:
        for i, doc_id in enumerate(results["ids"][0]):
            meta = results["metadatas"][0][i] if results["metadatas"] else {}
            entries.append({
                "entry_id": meta.get("entry_id", doc_id),
                "context": (results["documents"][0][i] if results["documents"] else ""),
                "correction": meta.get("correction", ""),
                "tags": meta.get("tags", "").split(",") if meta.get("tags") else [],
            })
    return entries
```

### 任务 B-3：旧数据迁移端点

**改动文件**：`app/api/routes.py`

```python
@router.post("/errors/migrate", summary="从 error_ledger.md 迁移旧数据到 ErrorEntry 表（LLM 自动打标签）")
async def migrate_error_ledger():
    """
    一次性解析 error_ledger.md，用 LLM 为每条 ### 级条目自动生成 tags，
    写入 ErrorEntry SQLite 表 + ChromaDB。
    幂等：已存在的条目（按 title MD5 去重）跳过不重复写入。
    """
    # 实现细节见 D 组 —— 调用 _auto_tag_error() LLM 辅助函数
```

### 组 B 验证清单

- [ ] `POST /errors/entries` → 201，SQLite + ChromaDB 均有记录
- [ ] `GET /errors/entries?tags=python,docker` → 按标签筛选正确
- [ ] `GET /errors/entries?search=proxy` → 关键词搜索命中
- [ ] `GET /errors/entries/{id}` → 返回完整详情
- [ ] `DELETE /errors/entries/{id}` → SQLite + ChromaDB 同步删除
- [ ] `GET /errors/tags` → 返回标签频次统计
- [ ] 旧 `GET /errors` 仍然返回 md 原文（向后兼容）

---

## 组 C — Worker 链路改造（按 tag 精确检索错题）

### 当前状态

- `worker.py` 的 `execute_task()` 中通过 `memory_retriever.retrieve_context()` 检索 top-3 error
- 检索结果完全依赖语义相似度，无标签过滤，经常命中不相关的错题
- `error_warnings` 作为纯文本注入 LLM system prompt

### 目标

Worker 执行技能前，先读取 Skill 的 `error_tags`，按标签精确筛选候选集后再语义排序。

### 任务 C-1：Worker execute_task 改造

**改动文件**：`app/services/worker.py`

**改动位置**：`execute_task()` 方法中 Step 1（RAG 语义检索）和 Step 4（LLM Fallback）之间。

**改动逻辑**：

```python
# ---- 原有 Step 1 保持不变（获取 skill_ids + skill_distances + error_warnings）----

# ---- 新增 Step 1.5：按 Skill.error_tags 精确检索相关错题 ----
targeted_error_warnings = []
if skill_ids:
    top_skill_id = skill_ids[0]
    with Session(engine) as session:
        skill_meta = session.exec(
            select(SkillMetadata).where(SkillMetadata.id == top_skill_id)
        ).first()

    if skill_meta:
        skill_error_tags = getattr(skill_meta, "error_tags", []) or []
        if skill_error_tags:
            # 按标签精确筛选 + 语义排序
            tagged_errors = await memory_retriever.retrieve_errors_by_tags(
                task_description=task.description,
                tags=skill_error_tags,
                top_k=3,
            )
            for te in tagged_errors:
                targeted_error_warnings.append(
                    f"[{','.join(te['tags'])}] {te['context']}\n→ Correction: {te['correction']}"
                )
            # 更新 hit_count
            for te in tagged_errors:
                _increment_hit_count(te["entry_id"])

# 合并：优先使用标签化错题，旧的语义错题作为补充
if targeted_error_warnings:
    final_error_warnings = targeted_error_warnings
elif error_warnings:
    final_error_warnings = error_warnings
else:
    final_error_warnings = []
```

**辅助函数** `_increment_hit_count`（worker.py 模块级）：
```python
def _increment_hit_count(entry_id: str) -> None:
    """原子性递增 ErrorEntry 的 hit_count。"""
    try:
        with Session(engine) as session:
            entry = session.get(ErrorEntry, entry_id)
            if entry:
                entry.hit_count = (entry.hit_count or 0) + 1
                session.add(entry)
                session.commit()
    except Exception:
        pass
```

**Step 4 LLM Fallback 改造**：将 `error_hint` 改用 `final_error_warnings`：
```python
error_hint = (
    "\n".join(final_error_warnings)
    if final_error_warnings
    else "无相关错题记录。"
)
```

### 组 C 验证清单

- [ ] 有 `error_tags` 的 Skill 被匹配时 → Worker 日志显示 "retrieve_errors_by_tags" 调用
- [ ] 标签化错题正确注入 LLM prompt（日志可见）
- [ ] ErrorEntry.hit_count 递增
- [ ] 无 error_tags 的 Skill → 回退到旧的语义检索逻辑（零回归）
- [ ] 无错题时 → 正常执行不报错

---

## 组 D — AI 自动化（打标签 + 推断 error_tags）

### 任务 D-1：fail_task 自动生成标签化错题

**改动文件**：`app/services/orchestrator.py`

**改动位置**：`fail_task()` 方法末尾，替换现有的 `add_error_entry()` 调用。

**新逻辑**：
```python
# 替换旧的 add_error_entry 调用
if task_description:
    try:
        # 调用 LLM 为失败任务自动生成结构化错题
        tagged_entry = await _auto_generate_error_entry(
            task_description=task_description,
            error_msg=error_msg,
        )
        # 写入 SQLite
        from app.models.database import ErrorEntry
        entry = ErrorEntry(
            title=tagged_entry["title"],
            context=tagged_entry["context"],
            correction=tagged_entry["correction"],
            prevention=tagged_entry.get("prevention", ""),
            tags=tagged_entry["tags"],
            severity=tagged_entry.get("severity", "warning"),
            source="task_failure",
        )
        with Session(engine) as session:
            session.add(entry)
            session.commit()
            session.refresh(entry)
            entry_id = entry.id
        # 同步写入 ChromaDB
        await memory_retriever.add_error_entry_v2(
            entry_id=entry_id,
            context=tagged_entry["context"],
            correction=tagged_entry["correction"],
            tags=tagged_entry["tags"],
            severity=tagged_entry.get("severity", "warning"),
        )
    except Exception as e:
        logger.error(f"Auto error entry generation failed: {e}")
```

**LLM 辅助函数** `_auto_generate_error_entry`（orchestrator.py 内部或独立 utils）：
```python
async def _auto_generate_error_entry(task_description: str, error_msg: str) -> dict:
    """
    调用 LLM 为失败任务自动生成结构化错题（含标签）。

    Prompt 要求 LLM 输出 JSON：
    {
        "title": "简短标题（10-20字）",
        "context": "完整错误上下文",
        "correction": "纠正方案",
        "prevention": "预防建议",
        "tags": ["tag1", "tag2", "tag3"],
        "severity": "warning"
    }
    """
    prompt = f"""你是一个错误分析助手。请分析以下任务失败信息，生成一条结构化的错题记录。

任务描述：{task_description}
错误信息：{error_msg}

请以 JSON 格式输出（不要输出其他内容）：
{{
    "title": "简短标题（10-20字概括错误）",
    "context": "详细的错误上下文描述",
    "correction": "正确的解决方案",
    "prevention": "预防再次发生的建议",
    "tags": ["3-5个分类标签，如 python, docker, api, encoding, timeout 等"],
    "severity": "critical 或 warning 或 info"
}}"""

    from app.services.model_router import model_router
    from app.models.schemas import Message
    response = await model_router.generate(
        messages=[Message(role="user", content=prompt)],
        temperature=0.3,
        max_tokens=500,
    )
    # 解析 JSON（容错处理）
    import json, re
    text = response.content or ""
    # 尝试提取 JSON 块
    json_match = re.search(r'\{.*\}', text, re.DOTALL)
    if json_match:
        return json.loads(json_match.group())
    # fallback：最基础的结构
    return {
        "title": task_description[:20],
        "context": f"任务描述: {task_description}\n错误信息: {error_msg}",
        "correction": "请检查任务依赖的技能是否存在，或调整任务描述后重新提交。",
        "prevention": "",
        "tags": ["general"],
        "severity": "warning",
    }
```

### 任务 D-2：技能注册时自动推断 error_tags

**改动文件**：`app/api/routes.py`

**改动位置**：`POST /skills` 端点和 `POST /skills/load-dir` 端点。

**逻辑**：如果注册技能时 `error_tags` 为空，调用 LLM 推断：

```python
async def _infer_error_tags(skill_name: str, description: str, language: str, tags: list) -> list:
    """调用 LLM 推断技能关联的错题标签。"""
    prompt = f"""你是一个软件工程助手。请根据以下技能信息，推断执行此技能时可能遇到的错误类别。

技能名称：{skill_name}
技能描述：{description}
语言：{language}
标签：{', '.join(tags) if tags else '无'}

请只输出一个 JSON 数组，包含 3-8 个错误类别标签，例如：
["python", "file-io", "encoding", "timeout", "dependency"]

不要输出其他内容。"""

    from app.services.model_router import model_router
    from app.models.schemas import Message
    response = await model_router.generate(
        messages=[Message(role="user", content=prompt)],
        temperature=0.2,
        max_tokens=100,
    )
    import json, re
    text = response.content or ""
    arr_match = re.search(r'\[.*\]', text, re.DOTALL)
    if arr_match:
        try:
            return json.loads(arr_match.group())
        except Exception:
            pass
    # fallback：用技能自身的 tags + language
    return list(set((tags or []) + [language]))
```

**在 POST /skills 中调用**：
```python
# 在写入 SkillMetadata 之前：
if not request.error_tags:
    request.error_tags = await _infer_error_tags(
        skill_name=request.name,
        description=request.description,
        language=request.language,
        tags=[], # 或从 request 中获取
    )
```

### 任务 D-3：旧数据迁移时 LLM 自动打标签

**改动文件**：`app/api/routes.py`

`POST /errors/migrate` 的实现：

```python
@router.post("/errors/migrate", summary="从 error_ledger.md 迁移旧数据（LLM 自动打标签）")
async def migrate_error_ledger():
    error_file = "/app/error_ledger.md" if os.path.exists("/app/error_ledger.md") else "error_ledger.md"
    if not os.path.exists(error_file):
        return {"status": "skipped", "message": "error_ledger.md not found"}

    with open(error_file, "r", encoding="utf-8") as f:
        content = f.read()

    import re, hashlib
    sections = re.split(r"\n### ", content)
    migrated, skipped = 0, 0

    for raw in sections[1:]:
        lines = raw.strip().split("\n")
        title = lines[0].strip()
        body = "\n".join(lines[1:]).strip()
        if not title or not body:
            continue

        # 幂等：按 title MD5 检测是否已迁移
        doc_id = hashlib.md5(title.encode("utf-8")).hexdigest()
        with Session(engine) as session:
            existing = session.get(ErrorEntry, doc_id)
            if existing:
                skipped += 1
                continue

        # LLM 自动生成 tags
        tagged = await _auto_generate_error_entry(
            task_description=title,
            error_msg=body,
        )

        entry = ErrorEntry(
            id=doc_id,
            title=tagged["title"],
            context=tagged["context"],
            correction=tagged["correction"],
            prevention=tagged.get("prevention", ""),
            tags=tagged["tags"],
            severity=tagged.get("severity", "warning"),
            source="manual",
        )
        with Session(engine) as session:
            session.add(entry)
            session.commit()

        await memory_retriever.add_error_entry_v2(
            entry_id=doc_id,
            context=tagged["context"],
            correction=tagged["correction"],
            tags=tagged["tags"],
            severity=tagged.get("severity", "warning"),
        )
        migrated += 1

    return {"status": "ok", "migrated": migrated, "skipped": skipped}
```

### 组 D 验证清单

- [ ] 手动让一个任务失败 → ErrorEntry 表中自动出现带 tags 的新条目
- [ ] 新注册一个不带 error_tags 的技能 → SkillMetadata.error_tags 被 LLM 自动填充
- [ ] `POST /errors/migrate` → 旧 md 数据迁移完成，每条都有 tags
- [ ] LLM 调用失败时 → fallback 正常工作，不阻塞主流程

---

## 组 E — 前端错题查询界面

### 当前状态

- 左侧栏「📓 错题集」点击后弹出模态框，直接渲染 `error_ledger.md` 原文
- 无搜索、无标签筛选、无分页

### 目标

将模态框替换为完整的错题查询界面：标签云筛选 + 关键词搜索 + 列表 + 详情展开。

### 任务 E-1：重写 openModal('errors') 逻辑

**改动文件**：`app/web/index.html`

**改动位置**：JS 函数 `openModal(type)` 中 `else if (type === 'errors')` 分支。

**替换为**：

```javascript
} else if (type === 'errors') {
    title.textContent = '📓 错题集';
    body.innerHTML = `
        <div style="margin-bottom:12px;display:flex;gap:8px;flex-wrap:wrap;">
            <input type="text" id="errorSearch" placeholder="搜索关键词..." 
                style="flex:1;min-width:200px;padding:8px 12px;border:1px solid #ddd;border-radius:6px;font-size:14px;"
                oninput="searchErrors()">
            <select id="errorSeverityFilter" onchange="searchErrors()"
                style="padding:8px;border:1px solid #ddd;border-radius:6px;font-size:14px;">
                <option value="">全部严重程度</option>
                <option value="critical">🔴 Critical</option>
                <option value="warning">🟡 Warning</option>
                <option value="info">🔵 Info</option>
            </select>
        </div>
        <div id="errorTagCloud" style="margin-bottom:12px;display:flex;gap:6px;flex-wrap:wrap;"></div>
        <div id="errorList" style="max-height:60vh;overflow-y:auto;">加载中...</div>
    `;
    // 加载标签云
    loadErrorTags();
    // 加载列表
    searchErrors();
}
```

### 任务 E-2：新增 JS 函数

**改动文件**：`app/web/index.html`

```javascript
// ── Phase 8: 错题集查询 ──────────────────────────
let selectedErrorTags = new Set();

async function loadErrorTags() {
    try {
        const res = await fetch('/api/v1/errors/tags');
        const data = await res.json();
        const cloud = document.getElementById('errorTagCloud');
        if (!cloud) return;
        cloud.innerHTML = (data.tags || []).map(t =>
            `<span class="error-tag" onclick="toggleErrorTag('${t.name}')" 
                  id="etag-${t.name}"
                  style="padding:4px 10px;border-radius:12px;font-size:12px;
                         cursor:pointer;border:1px solid #ddd;
                         background:${selectedErrorTags.has(t.name) ? '#4a90d9' : '#f5f5f5'};
                         color:${selectedErrorTags.has(t.name) ? '#fff' : '#333'};">
                ${t.name} (${t.count})
            </span>`
        ).join('');
    } catch(e) { console.error('loadErrorTags failed:', e); }
}

function toggleErrorTag(tag) {
    if (selectedErrorTags.has(tag)) {
        selectedErrorTags.delete(tag);
    } else {
        selectedErrorTags.add(tag);
    }
    loadErrorTags();
    searchErrors();
}

async function searchErrors() {
    const search = document.getElementById('errorSearch')?.value || '';
    const severity = document.getElementById('errorSeverityFilter')?.value || '';
    const tags = Array.from(selectedErrorTags).join(',');
    const params = new URLSearchParams();
    if (search) params.set('search', search);
    if (severity) params.set('severity', severity);
    if (tags) params.set('tags', tags);

    const list = document.getElementById('errorList');
    if (!list) return;

    try {
        const res = await fetch(`/api/v1/errors/entries?${params}`);
        const data = await res.json();
        if (!data.entries || data.entries.length === 0) {
            list.innerHTML = '<div style="text-align:center;color:#999;padding:20px;">暂无匹配的错题记录</div>';
            return;
        }
        list.innerHTML = data.entries.map(e => {
            const severityIcon = {critical:'🔴', warning:'🟡', info:'🔵'}[e.severity] || '⚪';
            const tagsHtml = (e.tags||[]).map(t =>
                `<span style="padding:2px 6px;border-radius:8px;font-size:11px;
                        background:#e8f0fe;color:#1967d2;">${t}</span>`
            ).join(' ');
            return `
                <div style="border:1px solid #eee;padding:12px;margin-bottom:8px;border-radius:8px;cursor:pointer;"
                     onclick="showErrorDetail('${e.id}')">
                    <div style="display:flex;justify-content:space-between;align-items:center;">
                        <strong>${severityIcon} ${e.title}</strong>
                        <span style="font-size:11px;color:#999;">命中 ${e.hit_count} 次</span>
                    </div>
                    <div style="margin-top:6px;display:flex;gap:4px;flex-wrap:wrap;">${tagsHtml}</div>
                    <div style="margin-top:4px;font-size:12px;color:#999;">
                        ${e.source === 'task_failure' ? '🤖 自动记录' : e.source === 'auto' ? '🤖 AI生成' : '✍️ 人工录入'}
                        · ${e.created_at ? new Date(e.created_at).toLocaleDateString() : ''}
                    </div>
                </div>
            `;
        }).join('');
        // 显示总数
        list.innerHTML = `<div style="font-size:12px;color:#999;margin-bottom:8px;">共 ${data.total} 条记录</div>` + list.innerHTML;
    } catch(e) {
        list.innerHTML = `<span style="color:red">加载失败: ${e.message}</span>`;
    }
}

async function showErrorDetail(entryId) {
    try {
        const res = await fetch(`/api/v1/errors/entries/${entryId}`);
        const e = await res.json();
        const severityIcon = {critical:'🔴', warning:'🟡', info:'🔵'}[e.severity] || '⚪';
        const tagsHtml = (e.tags||[]).map(t =>
            `<span style="padding:2px 8px;border-radius:8px;font-size:12px;
                    background:#e8f0fe;color:#1967d2;">${t}</span>`
        ).join(' ');
        const body = document.getElementById('modalBody');
        body.innerHTML = `
            <div style="margin-bottom:16px;">
                <a href="#" onclick="searchErrors();loadErrorTags();return false;" 
                   style="color:#4a90d9;text-decoration:none;">← 返回列表</a>
            </div>
            <h3>${severityIcon} ${e.title}</h3>
            <div style="margin:8px 0;display:flex;gap:4px;flex-wrap:wrap;">${tagsHtml}</div>
            <div style="margin-top:16px;">
                <h4 style="color:#666;">📋 错误上下文</h4>
                <pre style="white-space:pre-wrap;background:#f8f8f8;padding:12px;border-radius:6px;font-size:13px;">${(e.context||'').replace(/</g,'&lt;')}</pre>
            </div>
            <div style="margin-top:12px;">
                <h4 style="color:#22863a;">✅ 纠正方案</h4>
                <pre style="white-space:pre-wrap;background:#f0fff0;padding:12px;border-radius:6px;font-size:13px;">${(e.correction||'').replace(/</g,'&lt;')}</pre>
            </div>
            ${e.prevention ? `
            <div style="margin-top:12px;">
                <h4 style="color:#b08800;">⚠️ 预防建议</h4>
                <pre style="white-space:pre-wrap;background:#fffde7;padding:12px;border-radius:6px;font-size:13px;">${e.prevention.replace(/</g,'&lt;')}</pre>
            </div>` : ''}
            <div style="margin-top:12px;font-size:12px;color:#999;">
                来源: ${e.source} · 命中次数: ${e.hit_count} · ID: ${e.id}
                ${e.related_skill_ids?.length ? '<br>关联技能: ' + e.related_skill_ids.join(', ') : ''}
            </div>
        `;
    } catch(e) {
        console.error('showErrorDetail failed:', e);
    }
}
```

### 组 E 验证清单

- [ ] 点击「📓 错题集」→ 弹出标签云 + 搜索框 + 列表
- [ ] 点击标签 → 高亮 + 列表筛选
- [ ] 输入关键词 → 实时搜索
- [ ] 点击条目 → 展开完整详情（上下文 / 纠正方案 / 预防建议）
- [ ] 点击「← 返回列表」→ 回到列表视图
- [ ] 无数据时 → 显示空状态提示

---

## 附录：改动文件汇总

| 文件 | 改动类型 | 涉及任务 |
|------|---------|---------|
| `app/models/database.py` | 新增 `ErrorEntry` 模型 + `SkillMetadata` 扩展 | A-1, A-2 |
| `app/core/db.py` | 追加 migration 条目 | A-3 |
| `app/models/schemas.py` | 新增 3 个 Schema + 扩展 SkillCreate/SkillUpdate | A-4 |
| `app/api/routes.py` | 新增 ~7 个路由 + 迁移端点 + `_infer_error_tags` / `_auto_generate_error_entry` | B-1, D-1, D-2, D-3 |
| `app/services/memory_retriever.py` | 新增 `add_error_entry_v2` / `delete_error_entry` / `retrieve_errors_by_tags` | B-2 |
| `app/services/orchestrator.py` | `fail_task()` 改造为 LLM 自动生成标签化错题 | D-1 |
| `app/services/worker.py` | 新增 error_tags 精确检索 + hit_count 递增 | C-1 |
| `app/web/index.html` | 重写错题集模态框为查询界面 | E-1, E-2 |

## 附录：新增/改动 API 端点汇总

| 方法 | 路径 | 说明 | 任务 |
|------|------|------|------|
| `POST` | `/api/v1/errors/entries` | 创建错题条目（SQLite + ChromaDB） | B-1 |
| `GET` | `/api/v1/errors/entries` | 列出错题（标签筛选 + 搜索 + 分页） | B-1 |
| `GET` | `/api/v1/errors/entries/{id}` | 获取错题详情 | B-1 |
| `DELETE` | `/api/v1/errors/entries/{id}` | 删除错题 | B-1 |
| `GET` | `/api/v1/errors/tags` | 列出所有标签及频次 | B-1 |
| `POST` | `/api/v1/errors/migrate` | 从 error_ledger.md 迁移（LLM 打标签） | D-3 |
| _(保留)_ | `GET /api/v1/errors` | 旧接口，返回 md 原文 | 兼容 |

## 附录：执行顺序参考

```
EXECUTOR 应按以下顺序逐步实现：

1. A-1  → database.py 新增 ErrorEntry
2. A-2  → database.py SkillMetadata 新增 error_tags
3. A-3  → db.py 迁移条目
4. A-4  → schemas.py 新增/扩展
   ── 此时可 docker-compose up --build 验证表结构 ──

5. B-2  → memory_retriever.py 新增 3 个方法
6. B-1  → routes.py 错题 CRUD API（6 个端点）
   ── 此时可用 curl/Postman 测试 API ──

7. C-1  → worker.py 链路改造
   ── 此时 Worker 已支持按 tag 精确检索 ──

8. D-1  → orchestrator.py fail_task 改造
9. D-2  → routes.py POST /skills 自动推断 error_tags
10. D-3 → routes.py POST /errors/migrate
    ── 此时全自动化链路完成 ──

11. E-1 → index.html 重写错题集模态框
12. E-2 → index.html 新增 JS 函数
    ── 此时前端查询界面完成 ──
```
