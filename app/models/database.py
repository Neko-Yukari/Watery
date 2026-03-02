from sqlmodel import SQLModel, Field, Column, JSON
from typing import Optional, List, Any, Dict
from enum import Enum
from datetime import datetime
import uuid

class TaskStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"

class Task(SQLModel, table=True):
    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    parent_id: Optional[str] = Field(default=None, index=True)
    description: str
    status: TaskStatus = Field(default=TaskStatus.PENDING)
    dependencies: List[str] = Field(default_factory=list, sa_column=Column(JSON))
    result: Optional[Any] = Field(default=None, sa_column=Column(JSON))
    error_msg: Optional[str] = Field(default=None)
    created_at: Optional[datetime] = Field(default_factory=datetime.utcnow)
    updated_at: Optional[datetime] = Field(default=None)

class SkillMetadata(SQLModel, table=True):
    id: str = Field(primary_key=True)  # 如 "fetch_webpage"
    name: str
    language: str  # python, shell, nodejs
    entrypoint: str  # 相对路径或命令
    description: str = Field(default="")  # 自然语言描述，用于向量化语义检索
    parameters_schema: Dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    # Phase 4 溯源字段（ALTER TABLE 平滑升级）
    source_pdf_id: Optional[str] = Field(default=None, description="来源 PDF 文档 ID")
    source_pages: Optional[str] = Field(default=None, description="来源页码范围，如 '12-18'")
    tags: List[str] = Field(default_factory=list, sa_column=Column(JSON))
    # Phase 6 B-3 — 技能类型与文档型技能内容
    skill_type: str = Field(default="executable", description="executable | knowledge")
    knowledge_content: Optional[str] = Field(default=None, description="文档型技能的纯文本知识内容（skill_type=knowledge 时使用）")
    # Phase 8 — 标签化错题集关联
    error_tags: List[str] = Field(
        default_factory=list,
        sa_column=Column(JSON),
        description="该技能执行时需要参考的错题标签列表，如 ['python', 'file-io', 'encoding']",
    )


# ============================================================
# Phase 7 — 对话 Session 持久化
# ============================================================

class Conversation(SQLModel, table=True):
    """
    对话会话。一个 Conversation 包含多条 ConversationMessage。

    设计决策：
    - id 使用 UUID，杜绝并发碰撞（替代旧前端 'chat_' + Date.now() 方案）
    - model 字段记住会话使用的模型，避免切换模型后历史上下文混乱
    - message_count 冗余字段：避免列表页每次都需要 COUNT(*) 子查询
    - is_archived 软删除：防止误删，定期清理
    """
    id: str = Field(
        default_factory=lambda: str(uuid.uuid4()),
        primary_key=True,
        description="会话 UUID",
    )
    title: str = Field(
        default="新对话",
        description="会话标题（通常取第一条用户消息前 20 字自动生成）",
    )
    model: str = Field(
        default="ark-code-latest",
        description="该会话绑定的模型名称",
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
        description="消息总数（含所有 role），冗余字段供列表页快速展示",
    )
    is_archived: bool = Field(
        default=False,
        description="是否已归档（软删除标记）",
    )


class ConversationMessage(SQLModel, table=True):
    """
    对话消息。支持四种 role：system / user / assistant / tool

    Tool Calling 完整存储：
    - assistant 发起调用时：tool_calls_json 存储 JSON 数组字符串
    - tool 回传结果时：tool_call_id 关联对应的 tool_call ID

    设计说明：
    - tool_calls_json 使用字符串而非 Column(JSON)，便于精确控制序列化
    - seq 序号保证多条消息同时写入时顺序稳定（不依赖时间戳）
    - token_count 用于 Phase 7 D 组 Token 截断策略
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
    tool_calls_json: Optional[str] = Field(
        default=None,
        description="assistant 消息的 tool_calls JSON 数组序列化字符串",
    )
    tool_call_id: Optional[str] = Field(
        default=None,
        description="role=tool 消息关联的 tool_call_id",
    )
    token_count: Optional[int] = Field(
        default=None,
        description="该消息估算 token 数（用于截断策略，D-3）",
    )
    seq: int = Field(
        default=0,
        description="消息在会话内的序号（0-based），ORDER BY seq ASC 保证顺序",
    )
    created_at: Optional[datetime] = Field(
        default_factory=datetime.utcnow,
    )


class RuntimeSetting(SQLModel, table=True):
    """
    运行时设置（Phase 9 新增）。

    持久化存储用户在 Web UI 中调整的系统级配置项。
    每条记录为一个 key-value 对，支持 JSON 值。
    """
    key: str = Field(primary_key=True, description="设置项唯一标识，如 'vision_model'")
    value: str = Field(default="", description="设置值（JSON 序列化字符串）")
    description: str = Field(default="", description="设置项的可读描述")
    updated_at: Optional[datetime] = Field(default_factory=datetime.utcnow)


class PDFDocument(SQLModel, table=True):
    """记录已处理的 PDF 文档及其处理状态（Phase 4 新增）。"""
    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    filename: str = Field(index=True)
    file_path: str                              # 容器内存储路径
    file_hash: str = Field(index=True)          # SHA-256（幂等去重）
    page_count: int = Field(default=0)
    total_chunks: int = Field(default=0)
    processed_chunks: int = Field(default=0, description="已处理的 Chunk 数量（实时进度，Phase 9 新增）")
    skills_generated: List[str] = Field(default_factory=list, sa_column=Column(JSON))
    status: str = Field(default="pending")      # pending / processing / completed / failed
    error_msg: Optional[str] = Field(default=None)
    created_at: Optional[datetime] = Field(default_factory=datetime.utcnow)
    completed_at: Optional[datetime] = Field(default=None)


# ============================================================
# Phase 8 — 标签化错题集
# ============================================================

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
        description="来源：manual / auto / task_failure",
    )
    related_skill_ids: List[str] = Field(
        default_factory=list,
        sa_column=Column(JSON),
        description="关联的 Skill ID 数组",
    )
    hit_count: int = Field(
        default=0,
        description="被 Worker 命中并注入上下文的次数",
    )
    created_at: Optional[datetime] = Field(
        default_factory=datetime.utcnow,
    )


# ============================================================
# Phase 11 — 代码语义索引
# ============================================================

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
        description="符号名称，方法格式为 'ClassName.method_name'，顶层格式为 'func_name'",
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
        description="该文件依赖的导入模块（仅 module 级别记录）",
    )
    file_hash: str = Field(
        default="",
        description="源文件的 SHA-256 哈希（用于增量更新检测）",
    )
    indexed_at: Optional[datetime] = Field(
        default_factory=datetime.utcnow,
        description="索引时间",
    )
