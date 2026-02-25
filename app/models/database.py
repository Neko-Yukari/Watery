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


class PDFDocument(SQLModel, table=True):
    """记录已处理的 PDF 文档及其处理状态（Phase 4 新增）。"""
    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    filename: str = Field(index=True)
    file_path: str                              # 容器内存储路径
    file_hash: str = Field(index=True)          # SHA-256（幂等去重）
    page_count: int = Field(default=0)
    total_chunks: int = Field(default=0)
    skills_generated: List[str] = Field(default_factory=list, sa_column=Column(JSON))
    status: str = Field(default="pending")      # pending / processing / completed / failed
    error_msg: Optional[str] = Field(default=None)
    created_at: Optional[datetime] = Field(default_factory=datetime.utcnow)
    completed_at: Optional[datetime] = Field(default=None)
