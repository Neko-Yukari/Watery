from sqlmodel import SQLModel, Field, Column, JSON
from typing import Optional, List, Any, Dict
from enum import Enum
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

class SkillMetadata(SQLModel, table=True):
    id: str = Field(primary_key=True)  # 如 "fetch_webpage"
    name: str
    language: str  # python, shell, nodejs
    entrypoint: str  # 相对路径或命令
    parameters_schema: Dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
