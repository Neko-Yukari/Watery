# Phase 7 — 对话 Session 持久化 详细任务分解

> **[ARCHITECT] 模式产出**  
> 日期：2026-02-27  
> 目标：将对话历史从前端 localStorage 迁移至后端 SQLite 持久化存储，实现跨设备会话同步、Token 优化与完整的 Tool Calling 上下文保留。
>
> **完成状态：组 A ✅ | 组 B ✅ | 组 C ✅ | 组 D ✅**

---

## 核心需求回顾

**当前状态**：对话历史完全存储在浏览器 `localStorage`（key = `"wateryChats"`），后端 `POST /chat` 无状态。

**核心问题**：
1. `localStorage` 容量上限 ~5-10MB，对话积累后会 `QuotaExceededError`，所有历史丢失
2. 跨设备/跨浏览器不同步（手机端 / VSCode 插件 / 其他浏览器无法访问同一对话历史）
3. 每次请求发送完整消息数组 → 长对话 HTTP body 膨胀 + LLM token 线性增长
4. Tool Calling 中间消息（`tool_calls` / `tool_call_id`）未持久化，刷新后上下文丢失
5. 无对话摘要/截断机制应对上下文窗口溢出

**翻译为工程需求**：
1. 后端 SQLite 新增 `Conversation` + `ConversationMessage` 表
2. 完整的 REST API 管理会话生命周期（CRUD）
3. `/chat` 端点支持 `conversation_id`：从 DB 加载历史 → LLM 调用 → 结果写回 DB
4. 前端从 localStorage 迁移为 API 驱动（读写全走后端）
5. 长对话 Token 优化策略（截断 / 摘要）

---

## 功能分组 & 优先级

| 组 | 名称 | 优先级 | 阻塞关系 |
|---|------|--------|---------|
| **A** | 数据模型 + DB 迁移 | P0 | 无阻塞，可独立开发 |
| **B** | 会话 REST API + `/chat` 改造 | P0 | 依赖 A（DB 模型） |
| **C** | 前端迁移（localStorage → API 驱动） | P0 | 依赖 B（API 就绪） |
| **D** | 长对话 Token 优化（截断 + 可选摘要） | P1 | 依赖 B（消息读取链路） |

**建议实施顺序**：A → B → C → D

---

## 组 A — 数据模型 + DB 迁移

### 当前状态

- `app/models/database.py` 中有 `Task`、`SkillMetadata`、`PDFDocument` 三张表
- `app/core/db.py` 的 `_migrate_schema()` 已实现增量 `ALTER TABLE ADD COLUMN` 模式
- 无任何对话相关的数据模型

### 目标

新增 `Conversation` 和 `ConversationMessage` 两张 SQLite 表，支持完整的 Tool Calling 字段存储。

### 任务 A-1：新增 Conversation 数据模型

**改动文件**：`app/models/database.py`

**新增模型**：
```python
class Conversation(SQLModel, table=True):
    """对话会话。一个 Conversation 包含多条 ConversationMessage。"""
    id: str = Field(
        default_factory=lambda: str(uuid.uuid4()),
        primary_key=True,
        description="会话 UUID",
    )
    title: str = Field(
        default="新对话",
        description="会话标题（通常取第一条用户消息前 20 字）",
    )
    model: str = Field(
        default="ark-code-latest",
        description="该会话使用的模型名称",
    )
    created_at: Optional[datetime] = Field(
        default_factory=datetime.utcnow,
        description="创建时间 (UTC)",
    )
    updated_at: Optional[datetime] = Field(
        default=None,
        description="最后活跃时间 (UTC)，每次追加消息时更新",
    )
    message_count: int = Field(
        default=0,
        description="消息总数（含 system/user/assistant/tool），用于快速统计",
    )
    is_archived: bool = Field(
        default=False,
        description="是否归档（软删除标记）",
    )
```

**设计决策**：
- `id` 使用 UUID 而非时间戳，杜绝并发碰撞
- `model` 字段记住会话使用的模型，避免切换模型后历史上下文混乱
- `message_count` 冗余字段：避免每次列表页都要 `COUNT(*)` 子查询
- `is_archived` 软删除：用户删除时先标记归档，定期清理（防误删）

**验证**：`docker-compose up --build` 后 `PRAGMA table_info(conversation)` 返回所有列

---

### 任务 A-2：新增 ConversationMessage 数据模型

**改动文件**：`app/models/database.py`

**新增模型**：
```python
class ConversationMessage(SQLModel, table=True):
    """
    对话消息。支持四种 role：system / user / assistant / tool
    
    Tool Calling 完整存储：
    - assistant 发起调用：tool_calls 存储 JSON 数组
    - tool 回传结果：tool_call_id 关联对应的 tool_call
    """
    id: str = Field(
        default_factory=lambda: str(uuid.uuid4()),
        primary_key=True,
    )
    conversation_id: str = Field(
        index=True,
        foreign_key="conversation.id",
        description="关联的 Conversation ID",
    )
    role: str = Field(
        description="消息角色：system / user / assistant / tool",
    )
    content: Optional[str] = Field(
        default=None,
        description="消息内容（tool calling 时 assistant content 可能为 null）",
    )
    # Tool Calling 字段 — 存为 JSON 字符串
    tool_calls_json: Optional[str] = Field(
        default=None,
        description="assistant 消息的 tool_calls JSON 数组序列化（原始存储）",
    )
    tool_call_id: Optional[str] = Field(
        default=None,
        description="role=tool 消息关联的 tool_call_id",
    )
    # 元数据
    token_count: Optional[int] = Field(
        default=None,
        description="该消息估算 token 数（用于截断策略）",
    )
    seq: int = Field(
        description="消息在会话内的序号（0-based），用于排序",
    )
    created_at: Optional[datetime] = Field(
        default_factory=datetime.utcnow,
    )
```

**设计决策**：
- `tool_calls_json` 存为 JSON 字符串而非 `sa_column=Column(JSON)` — 原因：SQLite 的 JSON 类型实际只是 TEXT，且 tool_calls 结构复杂（嵌套对象），用字符串显式序列化更可控，读取时 `json.loads()` 即可
- `seq` 序号字段：确保消息顺序稳定（`created_at` 在批量写入时可能相同），查询时 `ORDER BY seq ASC`
- `token_count` 可选字段：为组 D（Token 优化）预留，写入时可用 `len(content) // 4` 粗估
- `foreign_key="conversation.id"` 建立外键关系

**验证**：`PRAGMA table_info(conversationmessage)` 返回所有列；`PRAGMA foreign_key_list(conversationmessage)` 确认外键

---

### 任务 A-3：DB 迁移支持

**改动文件**：`app/core/db.py`

**改动点**：在 `_migrate_schema()` 的 `migrations` 列表中追加 Conversation / ConversationMessage 相关的增量迁移字段（预留未来扩展用）：

```python
migrations = [
    # ... 已有迁移条目 ...
    # Phase 7 — 对话持久化
    ("conversation",        "message_count", "INTEGER DEFAULT 0"),
    ("conversation",        "is_archived",   "BOOLEAN DEFAULT 0"),
    ("conversationmessage", "token_count",   "INTEGER"),
]
```

**注意**：由于 `Conversation` 和 `ConversationMessage` 是全新表，`SQLModel.metadata.create_all(engine)` 会直接创建完整表结构。`_migrate_schema()` 这里只是预防后续迭代新增列的场景。

**验证**：`init_db()` 正常执行无报错，两张新表存在于 SQLite

---

### 组 A 验证清单

- [ ] `docker-compose up --build` 后 `conversation` 表存在且包含所有列
- [ ] `conversationmessage` 表存在且 `foreign_key` 指向 `conversation.id`
- [ ] 已有的 `task` / `skillmetadata` / `pdfdocument` 三表无回归

---

## 组 B — 会话 REST API + `/chat` 端点改造

### 当前状态

- `POST /chat` 接受 `ChatRequest { messages, model, temperature, max_tokens }`
- 后端不持久化任何对话，`messages` 全部由前端传入
- Tool Calling 循环中的中间消息（`role=tool`）仅在单次请求内存中，不回传给前端

### 目标

1. 完整的 Conversation CRUD API
2. `/chat` 端点支持 `conversation_id`（可选）：自动从 DB 加载历史、写回新消息
3. 兼容无 `conversation_id` 的旧模式（前端传完整 messages）

### 任务 B-1：Schema 扩展 — ConversationCreate / ConversationInfo / ChatRequest 改造

**改动文件**：`app/models/schemas.py`

**新增 Schema**：
```python
class ConversationCreate(BaseModel):
    """POST /conversations 请求体。"""
    title: Optional[str] = Field("新对话", description="会话标题")
    model: str = Field("ark-code-latest", description="使用的模型")
    system_prompt: Optional[str] = Field(
        None,
        description="可选的 system prompt，创建时自动作为第一条 system 消息写入",
    )


class ConversationInfo(BaseModel):
    """会话摘要（列表页使用）。"""
    id: str
    title: str
    model: str
    message_count: int
    created_at: Optional[str] = None
    updated_at: Optional[str] = None
    is_archived: bool = False


class ConversationDetail(BaseModel):
    """会话详情（含完整消息列表）。"""
    id: str
    title: str
    model: str
    message_count: int
    created_at: Optional[str] = None
    updated_at: Optional[str] = None
    messages: List[Message] = Field(default_factory=list)


class ConversationUpdate(BaseModel):
    """PATCH /conversations/{id} 请求体。"""
    title: Optional[str] = None
    model: Optional[str] = None
    is_archived: Optional[bool] = None
```

**ChatRequest 改造**：
```python
class ChatRequest(BaseModel):
    messages: Optional[List[Message]] = Field(
        None,
        description="直接传入消息列表（旧模式，与 conversation_id 二选一）",
    )
    conversation_id: Optional[str] = Field(
        None,
        description="会话 ID（新模式，后端自动加载/追加消息）",
    )
    model: str = Field("ark-code-latest")
    temperature: Optional[float] = Field(0.7)
    max_tokens: Optional[int] = Field(2048)
```

**关键决策**：
- `messages` 和 `conversation_id` **二选一**：`conversation_id` 非空时忽略 `messages`，从 DB 加载
- 保持向后兼容：不传 `conversation_id` 时行为与现有完全一致

**验证**：Pydantic 模型序列化/反序列化正确

---

### 任务 B-2：Conversation CRUD API

**改动文件**：`app/api/routes.py`

**新增路由区块**：
```
# ============================================================
# 对话会话管理
# ============================================================

POST   /conversations                → 创建新会话
GET    /conversations                → 列表（支持 ?archived=true 筛选）
GET    /conversations/{id}           → 获取完整会话（含所有消息）
PATCH  /conversations/{id}           → 更新标题/模型/归档状态
DELETE /conversations/{id}           → 硬删除（或标记归档，取决于 ?hard=true）
```

**详细实现**：

```python
@router.post("/conversations", status_code=201, summary="创建新会话")
async def create_conversation(request: ConversationCreate):
    """
    创建新的空会话。
    若提供 system_prompt，自动创建一条 role=system 消息。
    返回 ConversationInfo。
    """
    conv = Conversation(
        title=request.title or "新对话",
        model=request.model,
    )
    with Session(engine) as session:
        session.add(conv)
        session.commit()
        session.refresh(conv)
        conv_id = conv.id

    # 可选 system prompt
    if request.system_prompt:
        msg = ConversationMessage(
            conversation_id=conv_id,
            role="system",
            content=request.system_prompt,
            seq=0,
        )
        with Session(engine) as session:
            session.add(msg)
            conv_db = session.get(Conversation, conv_id)
            conv_db.message_count = 1
            session.add(conv_db)
            session.commit()

    return {"id": conv_id, "title": conv.title, "model": conv.model}


@router.get("/conversations", summary="列出所有会话")
async def list_conversations(archived: bool = False):
    """
    返回会话列表（按 updated_at 倒序）。
    默认只返回未归档的会话；?archived=true 可查看归档。
    """
    with Session(engine) as session:
        stmt = (
            select(Conversation)
            .where(Conversation.is_archived == archived)
            .order_by(Conversation.updated_at.desc(), Conversation.created_at.desc())
        )
        convs = session.exec(stmt).all()
    return {
        "conversations": [
            {
                "id": c.id,
                "title": c.title,
                "model": c.model,
                "message_count": c.message_count,
                "created_at": c.created_at.isoformat() if c.created_at else None,
                "updated_at": c.updated_at.isoformat() if c.updated_at else None,
            }
            for c in convs
        ]
    }


@router.get("/conversations/{conv_id}", summary="获取会话详情（含消息）")
async def get_conversation(conv_id: str):
    """
    返回会话元数据 + 全部消息（按 seq 排序）。
    
    消息中的 tool_calls_json 会被反序列化为 tool_calls 对象列表，
    以便前端直接渲染工具调用卡片。
    """
    with Session(engine) as session:
        conv = session.get(Conversation, conv_id)
        if not conv:
            raise HTTPException(404, f"Conversation '{conv_id}' not found.")
        msgs = session.exec(
            select(ConversationMessage)
            .where(ConversationMessage.conversation_id == conv_id)
            .order_by(ConversationMessage.seq.asc())
        ).all()

    return {
        "id": conv.id,
        "title": conv.title,
        "model": conv.model,
        "message_count": conv.message_count,
        "created_at": conv.created_at.isoformat() if conv.created_at else None,
        "updated_at": conv.updated_at.isoformat() if conv.updated_at else None,
        "messages": [
            {
                "role": m.role,
                "content": m.content,
                "tool_calls": json.loads(m.tool_calls_json) if m.tool_calls_json else None,
                "tool_call_id": m.tool_call_id,
            }
            for m in msgs
        ],
    }


@router.patch("/conversations/{conv_id}", summary="更新会话属性")
async def update_conversation(conv_id: str, request: ConversationUpdate):
    """更新标题、模型或归档状态。仅修改提供的字段。"""
    with Session(engine) as session:
        conv = session.get(Conversation, conv_id)
        if not conv:
            raise HTTPException(404, f"Conversation '{conv_id}' not found.")
        if request.title is not None:
            conv.title = request.title
        if request.model is not None:
            conv.model = request.model
        if request.is_archived is not None:
            conv.is_archived = request.is_archived
        conv.updated_at = datetime.utcnow()
        session.add(conv)
        session.commit()
    return {"status": "updated", "conversation_id": conv_id}


@router.delete("/conversations/{conv_id}", summary="删除会话")
async def delete_conversation(conv_id: str, hard: bool = False):
    """
    删除会话。
    - 默认软删除（标记 is_archived=True）。
    - ?hard=true 时硬删除（同时删除所有关联消息）。
    """
    with Session(engine) as session:
        conv = session.get(Conversation, conv_id)
        if not conv:
            raise HTTPException(404, f"Conversation '{conv_id}' not found.")
        if hard:
            # 先删消息
            msgs = session.exec(
                select(ConversationMessage)
                .where(ConversationMessage.conversation_id == conv_id)
            ).all()
            for m in msgs:
                session.delete(m)
            session.delete(conv)
        else:
            conv.is_archived = True
            conv.updated_at = datetime.utcnow()
            session.add(conv)
        session.commit()
    return {"status": "hard_deleted" if hard else "archived", "conversation_id": conv_id}
```

**验证**：
1. `POST /conversations` → 201，返回 UUID
2. `GET /conversations` → 列表不含归档
3. `GET /conversations/{id}` → 含空 messages 数组
4. `PATCH /conversations/{id}` → 标题更新
5. `DELETE /conversations/{id}` → 软删除后不出现在列表

---

### 任务 B-3：`/chat` 端点改造 — 支持 conversation_id 模式

**改动文件**：`app/api/routes.py`（改写 `chat_endpoint`）

**流程**：

```
                    ┌──────────────────────────────┐
                    │   POST /chat                 │
                    │   { conversation_id?, ...}    │
                    └─────────┬────────────────────┘
                              │
                    ┌─────────▼────────────────────┐
                    │ conversation_id 存在？         │
                    └─────────┬──────────┬─────────┘
                         是   │          │  否（旧模式）
                    ┌─────────▼──────┐  ┌▼──────────────┐
                    │ 从 DB 加载     │  │ 使用 request   │
                    │ 全部消息       │  │ .messages      │
                    │ (含 tool_calls│  └────────────────┘
                    │  完整上下文)   │
                    └────────┬──────┘
                             │
                    ┌────────▼──────────────────────┐
                    │ Tool Calling 循环（不变）       │
                    │ 最终得到 final_response        │
                    └────────┬──────────────────────┘
                             │
                    ┌────────▼──────────────────────┐
                    │ conversation_id 存在？         │
                    └────────┬──────────┬───────────┘
                        是   │          │  否
                    ┌────────▼──────┐   │（直接返回）
                    │ 批量写入 DB:  │   │
                    │ - user msg    │   │
                    │ - assistant   │   │
                    │   tool_calls  │   │
                    │ - tool msgs   │   │
                    │ - final reply │   │
                    │ 更新 conv     │   │
                    │ updated_at +  │   │
                    │ message_count │   │
                    └───────────────┘   │
                             │          │
                    ┌────────▼──────────▼───────────┐
                    │ 返回 ChatResponse（新增        │
                    │ conversation_id 字段）         │
                    └──────────────────────────────┘
```

**关键实现细节**：

```python
@router.post("/chat", response_model=ChatResponse, summary="直连聊天接口（支持 Tool Calling + 会话持久化）")
async def chat_endpoint(request: ChatRequest):
    conv_id = request.conversation_id

    # ---- 1. 加载消息 ----
    if conv_id:
        # 从 DB 加载历史消息（含完整 tool_calls 上下文）
        with Session(engine) as session:
            conv = session.get(Conversation, conv_id)
            if not conv:
                raise HTTPException(404, f"Conversation '{conv_id}' not found.")
            db_msgs = session.exec(
                select(ConversationMessage)
                .where(ConversationMessage.conversation_id == conv_id)
                .order_by(ConversationMessage.seq.asc())
            ).all()

        # 转为 Message 对象列表
        messages = []
        for m in db_msgs:
            msg = Message(
                role=m.role,
                content=m.content,
                tool_call_id=m.tool_call_id,
            )
            if m.tool_calls_json:
                msg.tool_calls = [
                    ToolCall(**tc) for tc in json.loads(m.tool_calls_json)
                ]
            messages.append(msg)

        # 注意：conversation_id 模式下，前端只需发送最新那条 user 消息
        # 后端加载 DB 历史 + 追加新消息
        if request.messages:
            # 前端可能通过 messages 发过来新的 user 消息
            new_user_msgs = [m for m in request.messages if m.role == "user"]
            messages.extend(new_user_msgs)
    else:
        # 旧模式：前端传完整 messages
        if not request.messages:
            raise HTTPException(400, "必须提供 messages 或 conversation_id。")
        messages = list(request.messages)

    # ---- 2. Tool Calling 循环（与现有逻辑完全一致）----
    # ... (现有逻辑不变) ...

    # ---- 3. 写回 DB ----
    if conv_id:
        _persist_chat_turn(conv_id, new_messages_in_this_turn, final_response)

    return response
```

**辅助函数 `_persist_chat_turn()`**：

```python
def _persist_chat_turn(
    conv_id: str,
    new_messages: List[Message],
    tool_results: List[dict],
):
    """
    将本次对话轮次中产生的所有新消息（user + assistant + tool）
    批量写入 ConversationMessage 表，并更新 Conversation 元数据。
    """
    from datetime import datetime
    with Session(engine) as session:
        conv = session.get(Conversation, conv_id)
        if not conv:
            return

        current_seq = conv.message_count  # 从当前最大 seq 继续

        for msg in new_messages:
            db_msg = ConversationMessage(
                conversation_id=conv_id,
                role=msg.role,
                content=msg.content,
                tool_calls_json=(
                    json.dumps([tc.model_dump() for tc in msg.tool_calls], ensure_ascii=False)
                    if msg.tool_calls else None
                ),
                tool_call_id=msg.tool_call_id,
                token_count=len(msg.content or "") // 4,  # 粗估
                seq=current_seq,
            )
            session.add(db_msg)
            current_seq += 1

        conv.message_count = current_seq
        conv.updated_at = datetime.utcnow()

        # 自动更新标题（若仍为默认标题且有 user 消息）
        if conv.title == "新对话":
            first_user = next((m for m in new_messages if m.role == "user"), None)
            if first_user and first_user.content:
                conv.title = first_user.content[:20] + ("..." if len(first_user.content) > 20 else "")

        session.add(conv)
        session.commit()
```

**ChatResponse 扩展**：
```python
class ChatResponse(BaseModel):
    # ... 已有字段 ...
    conversation_id: Optional[str] = Field(
        None,
        description="关联的会话 ID（conversation_id 模式下填充）",
    )
```

**向后兼容保障**：
- 不传 `conversation_id` → 行为与现有完全一致（前端传 messages，后端不持久化）
- 传了 `conversation_id` → 后端加载 + 追加 + 持久化
- 现有前端在迁移前仍然正常工作

**验证**：
1. 不传 `conversation_id` 的老请求 → 行为不变
2. 传 `conversation_id` → DB 中消息正确累积
3. Tool Calling 场景 → `tool_calls_json` / `tool_call_id` 正确写入
4. `GET /conversations/{id}` 返回完整消息（含 Tool Calling 上下文）

---

### 任务 B-4：Schema 导入与路由注册清理

**改动文件**：
- `app/api/routes.py` — 顶部 import 补充新 Schema 和 DB 模型
- `app/models/schemas.py` — 确保新增的 Schema 能被正确导入

**具体内容**：
```python
# routes.py 顶部追加
from app.models.schemas import (
    # ... 已有 ...
    ConversationCreate,
    ConversationUpdate,
    ConversationInfo,
    ConversationDetail,
)
from app.models.database import (
    # ... 已有 ...
    Conversation,
    ConversationMessage,
)
```

**注意**：新增的 `/conversations` 路由要放在 `/chat` 和 `/intention` 之后、`/tasks` 之前，保持路由分区清晰。

**验证**：`docker-compose up --build` 无 ImportError

---

### 组 B 验证清单

- [ ] `POST /conversations` → 201，返回新会话 ID
- [ ] `GET /conversations` → 返回按时间倒序的会话列表
- [ ] `POST /chat { conversation_id, messages: [{role: "user", content: "你好"}] }` → 消息写入 DB
- [ ] `GET /conversations/{id}` → 返回 messages 数组含刚发的消息 + AI 回复
- [ ] 不传 `conversation_id` 的旧请求 → 行为完全不变（零回归）
- [ ] Tool Calling 场景 → `tool_calls_json` 和 `tool_call_id` 正确持久化
- [ ] 多轮对话 → seq 序号递增正确，消息顺序一致
- [ ] 自动标题 → 第一条用户消息自动设置为会话标题

---

## 组 C — 前端迁移（localStorage → API 驱动）

### 当前状态

- `index.html` 中 JS 全局变量 `chats = {}` 存储所有会话
- `saveChats()` → `localStorage.setItem('wateryChats', JSON.stringify(chats))`
- `loadChat(chatId)` → 从内存 `chats` 对象读取
- `sendMessage()` → 将 `chats[currentChatId].messages` 完整发给 `/chat`

### 目标

将前端所有会话管理操作改为调用后端 API，localStorage 仅保留 `lastConversationId`（记住上次打开的会话）。

### 任务 C-1：重写会话管理函数

**改动文件**：`app/web/index.html`

**替换范围**：JS 中的 `chats`、`currentChatId`、`saveChats()`、`createNewChat()`、`loadChat()`、`deleteCurrentChat()`、`renderHistoryList()` 全部重写。

**核心变更**：

| 原实现 | 新实现 |
|--------|--------|
| `chats = {}` 内存对象 | 删除，不再缓存全量数据 |
| `saveChats()` → `localStorage` | 删除，每次操作调 API |
| `createNewChat()` | `await fetch('/api/v1/conversations', {method: 'POST'})` |
| `loadChat(chatId)` | `await fetch('/api/v1/conversations/${chatId}')` |
| `deleteCurrentChat()` | `await fetch('/api/v1/conversations/${id}', {method: 'DELETE'})` |
| `renderHistoryList()` | `await fetch('/api/v1/conversations')` → 渲染列表 |
| `window.onload` | 加载列表 → 读 `localStorage.lastConversationId` → 加载 |

**新增全局变量**:
```javascript
let currentConversationId = null;  // 替代 currentChatId

// 唯一用到 localStorage 的地方：记住上次打开的会话
function rememberLastConversation(id) {
    localStorage.setItem('wateryLastConversationId', id);
}
function getLastConversationId() {
    return localStorage.getItem('wateryLastConversationId');
}
```

**验证**：
- 新建会话 → 后端 DB 有记录
- 刷新页面 → 恢复上次会话
- 不同浏览器 → 看到相同会话列表

---

### 任务 C-2：重写 `sendMessage()` — 使用 conversation_id 模式

**改动文件**：`app/web/index.html`

**核心变更**：

```javascript
async function sendMessage() {
    const text = userInput.value.trim();
    if (!text) return;
    userInput.value = '';

    // 如果还没有会话，先创建
    if (!currentConversationId) {
        const res = await fetch('/api/v1/conversations', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                title: text.substring(0, 20),
                model: modelSelect.value,
            }),
        });
        const data = await res.json();
        currentConversationId = data.id;
        rememberLastConversation(currentConversationId);
        await refreshConversationList();
    }

    appendMessage('user', text);

    // 只发送新的 user 消息 + conversation_id
    const body = isIntentionMode
        ? JSON.stringify({ intention: text })
        : JSON.stringify({
            conversation_id: currentConversationId,
            messages: [{ role: 'user', content: text }],
            model: modelSelect.value,
        });

    // ... fetch + 处理响应 (基本不变) ...
    // 移除：chats[currentChatId].messages.push(...) 
    // 移除：saveChats()
    // 新增：响应后刷新侧边栏（标题可能更新了）
    await refreshConversationList();
}
```

**关键变更**：
- 不再维护 `chats[currentChatId].messages` 内存数组
- 不再 `saveChats()` 到 localStorage
- 前端只发当前这条 user 消息，后端从 DB 加载完整历史
- 响应后刷新侧边栏（更新标题、时间等）

**验证**：
1. 发消息 → AI 回复显示正确
2. 刷新页面 → `loadChat()` 从 API 加载完整历史展示

---

### 任务 C-3：Tool Calling 可视化兼容

**改动文件**：`app/web/index.html`

**分析**：`GET /conversations/{id}` 返回的 messages 中已包含 `tool_calls` 和 `tool_call_id` 字段。

**改动**：调整 `loadChat()` 中的消息渲染逻辑：
- `role=assistant` 且有 `tool_calls` → 显示工具调用卡片（调用 `appendMessageWithTools()`）
- `role=tool` → 以折叠形式显示工具返回结果（或跳过不显示，因为最终 AI 回复已经整合了结果）

**决策点**：
- Tool Calling 中间消息（`role=tool`）在历史回看时**默认折叠/隐藏**，只展示最终 AI 总结
- 用户可点击"展开工具调用详情"查看完整链路

**验证**：加载含 Tool Calling 的历史会话 → 正确显示工具卡片

---

### 任务 C-4：localStorage 数据迁移（一次性）

**改动文件**：`app/web/index.html`

**目的**：为已有用户从 localStorage 迁移历史数据到后端 DB

**实现**：在 `window.onload` 中检测旧数据并迁移：

```javascript
async function migrateLocalStorageToBackend() {
    const saved = localStorage.getItem('wateryChats');
    if (!saved) return;  // 无旧数据，跳过

    try {
        const oldChats = JSON.parse(saved);
        const chatIds = Object.keys(oldChats);
        if (chatIds.length === 0) return;

        console.log(`[Migration] Found ${chatIds.length} chats in localStorage, migrating...`);

        for (const chatId of chatIds) {
            const chat = oldChats[chatId];
            // 创建会话
            const res = await fetch('/api/v1/conversations', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ title: chat.title || '迁移对话' }),
            });
            const conv = await res.json();

            // 逐条写入消息（通过一次 /chat 调用无法做到，需要新增批量导入端点或逐条写）
            // 简化方案：直接丢弃旧 Tool Calling 上下文，
            // 只保留 user/assistant 的 content 文本
            // 因为旧数据本身就没有存 tool_calls
            if (chat.messages && chat.messages.length > 0) {
                await fetch(`/api/v1/conversations/${conv.id}/import`, {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ messages: chat.messages }),
                });
            }
        }

        // 迁移完成后清除旧数据
        localStorage.removeItem('wateryChats');
        console.log('[Migration] Done. localStorage cleared.');
    } catch (e) {
        console.error('[Migration] Failed:', e);
        // 不删除旧数据，下次再试
    }
}
```

**后端辅助端点**（B 组追加）：

```python
@router.post("/conversations/{conv_id}/import", summary="批量导入历史消息（迁移用）")
async def import_messages(conv_id: str, messages: List[Message]):
    """一次性导入多条消息到指定会话（仅用于 localStorage → DB 迁移）。"""
    # 写入 DB，seq 从 0 递增
    # 此端点迁移完成后可考虑移除
```

**验证**：
1. 旧浏览器（有 localStorage 数据）→ 打开页面 → 自动迁移 → localStorage 清空
2. 新浏览器（无 localStorage）→ 无迁移行为
3. 迁移后 `GET /conversations` 显示所有旧会话

---

### 组 C 验证清单

- [ ] 新建会话 → 后端有记录 → 侧边栏立即显示
- [ ] 发消息 → AI 回复正常 → 刷新页面恢复完整历史
- [ ] 切换会话 → 消息区域更新
- [ ] 删除会话 → 侧边栏移除
- [ ] 不同浏览器打开 → 看到相同会话列表（跨设备同步）
- [ ] Tool Calling 历史 → 正确显示工具卡片
- [ ] localStorage 旧数据自动迁移完成

---

## 组 D — 长对话 Token 优化

### 当前状态

- 每次 `/chat` 请求将全部历史消息发给 LLM
- 无截断、无摘要、无 token 统计
- 长对话会导致：(1) LLM 上下文窗口溢出 400 错误 (2) token 成本线性增长

### 目标

在发送给 LLM 前，对消息历史进行智能截断，确保不超过模型上下文窗口的阈值。

### 任务 D-1：Token 估算工具

**改动文件**：`app/services/model_router.py`（新增辅助方法）

**实现**：
```python
def estimate_tokens(text: str) -> int:
    """
    快速估算文本 token 数（不依赖 tiktoken 等外部库）。
    
    规则：
    - 英文按 1 word ≈ 1.3 token 估算
    - 中文按 1 字 ≈ 2 token 估算（中文字符 UTF-8 编码后 tokenize 通常 1.5-2 个 token）
    - 混合内容按 len(text) / 2 粗估（偏保守，宁截多不截少）
    """
    if not text:
        return 0
    # 简单策略：UTF-8 字节数 / 3（适用于中英混合）
    return max(1, len(text.encode('utf-8')) // 3)


def estimate_messages_tokens(messages: List[Message]) -> int:
    """估算消息列表的总 token 数。每条消息额外加 4 token 的格式开销。"""
    total = 0
    for msg in messages:
        total += 4  # role + 格式开销
        total += estimate_tokens(msg.content or "")
        if msg.tool_calls:
            total += estimate_tokens(json.dumps([tc.model_dump() for tc in msg.tool_calls]))
    return total
```

**验证**：单元测试 — 几种典型文本的估算结果在合理范围内

---

### 任务 D-2：消息截断策略

**改动文件**：`app/api/routes.py`（在 `/chat` 端点中调用）

**新增函数**：
```python
def truncate_messages(
    messages: List[Message],
    max_tokens: int = 28000,  # 预留约 4K 给模型输出
    keep_system: bool = True,
) -> List[Message]:
    """
    智能截断消息列表以适应 LLM 上下文窗口。
    
    策略（按优先级）：
    1. 永远保留 system 消息（如果有）
    2. 永远保留最近 2 轮对话（最新 4 条 user+assistant）
    3. 从最早的非 system 消息开始删除，直到总 token 数 < max_tokens
    4. 在截断点插入一条"[系统提示: 更早的 N 条消息已省略]"摘要消息
    
    返回：截断后的消息列表
    """
    total = estimate_messages_tokens(messages)
    if total <= max_tokens:
        return messages  # 不需要截断
    
    # 分离 system 消息和其余消息
    system_msgs = [m for m in messages if m.role == "system"] if keep_system else []
    other_msgs = [m for m in messages if m.role != "system"]
    
    # 保留最近 N 条（至少保留最近 2 轮 = 约 4 条）
    min_keep = min(4, len(other_msgs))
    recent = other_msgs[-min_keep:]
    candidates = other_msgs[:-min_keep]  # 可被截断的部分
    
    # 从最早的开始移除
    removed = 0
    while candidates and estimate_messages_tokens(system_msgs + candidates + recent) > max_tokens:
        candidates.pop(0)
        removed += 1
    
    # 插入截断提示
    result = list(system_msgs)
    if removed > 0:
        result.append(Message(
            role="system",
            content=f"[注意: 更早的 {removed} 条消息已因上下文长度限制被省略。以下是最近的对话。]",
        ))
    result.extend(candidates)
    result.extend(recent)
    
    return result
```

**在 `/chat` 中的调用位置**：Tool Calling 循环之前

```python
# 在实际调用 LLM 前截断
# 根据模型确定上下文窗口大小
context_limits = {
    "ark-code-latest": 28000,      # 32K 窗口，预留 4K 输出
    "gemini-2.5-flash": 100000,    # 1M 窗口，保守上限
    "gemini-2.5-pro": 100000,
}
max_ctx = context_limits.get(request.model, 28000)
messages = truncate_messages(messages, max_tokens=max_ctx)
```

**验证**：
1. 构造超长对话（例如 100 轮）→ 截断后 token 数在阈值内
2. system 消息始终保留
3. 最近 2 轮对话始终保留

---

### 任务 D-3：Token 统计写入 ConversationMessage

**改动文件**：`app/api/routes.py`（B-3 的 `_persist_chat_turn` 中）

**改动**：在写入 `ConversationMessage` 时，用 `estimate_tokens()` 填充 `token_count` 字段。

```python
db_msg = ConversationMessage(
    # ... 其他字段 ...
    token_count=estimate_tokens(msg.content or ""),
)
```

**用途**：
- `GET /conversations` 可返回每个会话的总 token 消耗估算
- 前端可显示"本次对话已使用约 X token"
- 未来可据此触发自动摘要（当总 token 超过阈值时，LLM 自动生成对话摘要替代早期消息）

**验证**：`GET /conversations/{id}` 中每条消息的 `token_count` 非空

---

### 组 D 验证清单

- [ ] 100 轮对话后 → LLM 调用不报上下文溢出错误
- [ ] 截断后 system 消息保留、最近对话保留
- [ ] 截断提示消息出现在对话中
- [ ] ConversationMessage.token_count 被正确填充
- [ ] 不同模型使用不同的上下文窗口限制

---

## 附录：改动文件汇总

| 文件 | 改动类型 | 涉及任务 |
|------|---------|---------|
| `app/models/database.py` | 新增 2 个 SQLModel | A-1, A-2 |
| `app/core/db.py` | 追加 migration 条目 | A-3 |
| `app/models/schemas.py` | 新增 4 个 Pydantic 模型 + ChatRequest/ChatResponse 扩展 | B-1 |
| `app/api/routes.py` | 新增 ~7 个路由 + 改写 chat_endpoint | B-2, B-3, B-4 |
| `app/web/index.html` | 重写 JS 会话管理逻辑 | C-1, C-2, C-3, C-4 |
| `app/services/model_router.py` | 新增 token 估算方法 | D-1 |

## 附录：新增 API 端点汇总

| 方法 | 路径 | 说明 | 任务 |
|------|------|------|------|
| `POST` | `/api/v1/conversations` | 创建新会话 | B-2 |
| `GET` | `/api/v1/conversations` | 列出会话 | B-2 |
| `GET` | `/api/v1/conversations/{id}` | 获取会话详情+消息 | B-2 |
| `PATCH` | `/api/v1/conversations/{id}` | 更新会话属性 | B-2 |
| `DELETE` | `/api/v1/conversations/{id}` | 删除会话 | B-2 |
| `POST` | `/api/v1/conversations/{id}/import` | 批量导入消息（迁移） | C-4 |
| `POST` | `/api/v1/chat` | 原有端点，新增 conversation_id 支持 | B-3 |
