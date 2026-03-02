import logging
import chromadb
from sqlmodel import create_engine, Session, SQLModel
from sqlalchemy import text
from app.core.config import settings
import os

logger = logging.getLogger(__name__)

# SQLite 初始化
engine = create_engine(settings.sqlite_db_path, echo=False)


def _migrate_schema():
    """
    增量 Schema 迁移：为已存在的表安全添加新字段。
    SQLite 仅支持 ADD COLUMN，不需要完整重建表。
    """
    migrations = [
        # (table_name, column_name, column_definition)
        ("task",          "created_at",        "DATETIME"),
        ("task",          "updated_at",        "DATETIME"),
        ("skillmetadata", "description",       "TEXT NOT NULL DEFAULT ''"),
        # Phase 4 溯源字段
        ("skillmetadata", "source_pdf_id",     "TEXT"),
        ("skillmetadata", "source_pages",      "TEXT"),
        ("skillmetadata", "tags",              "JSON DEFAULT '[]'"),
        # Phase 6 B-3 文档型技能
        ("skillmetadata", "skill_type",        "TEXT NOT NULL DEFAULT 'executable'"),
        ("skillmetadata", "knowledge_content", "TEXT"),
        # Phase 7 — 对话 Session 持久化（新表，以下只预留未来扩展列用）
        ("conversation",        "message_count", "INTEGER DEFAULT 0"),
        ("conversation",        "is_archived",   "BOOLEAN DEFAULT 0"),
        ("conversationmessage", "token_count",   "INTEGER"),
        # Phase 8 — 标签化错题集
        ("skillmetadata", "error_tags",          "JSON DEFAULT '[]'"),
        ("errorentry",    "prevention",          "TEXT DEFAULT ''"),
        ("errorentry",    "hit_count",           "INTEGER DEFAULT 0"),
        ("errorentry",    "related_skill_ids",   "JSON DEFAULT '[]'"),
        # Phase 13 — 错题反思归纳（raw / insight + 状态流转）
        ("errorentry",    "entry_type",          "TEXT NOT NULL DEFAULT 'raw'"),
        ("errorentry",    "status",              "TEXT NOT NULL DEFAULT 'active'"),
        ("errorentry",    "summarized_from_ids", "JSON DEFAULT '[]'"),
        # Phase 9 — PDF 大文件处理
        ("pdfdocument",   "processed_chunks",    "INTEGER DEFAULT 0"),
        # Phase 11 — 代码语义索引（codesymbol 为新表，以下预留增量列）
        ("codesymbol",    "decorators",           "JSON DEFAULT '[]'"),
        ("codesymbol",    "imports",              "JSON DEFAULT '[]'"),
        ("codesymbol",    "file_hash",            "TEXT DEFAULT ''"),
        ("codesymbol",    "indexed_at",           "DATETIME"),
    ]
    with engine.connect() as conn:
        for table, column, col_def in migrations:
            result = conn.execute(text(f"PRAGMA table_info({table})"))
            existing_cols = [row[1] for row in result.fetchall()]
            if column not in existing_cols:
                conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {col_def}"))
                conn.commit()
                logger.info(f"DB migration: added column '{column}' to table '{table}'")


def init_db():
    SQLModel.metadata.create_all(engine)
    _migrate_schema()

def get_session():
    with Session(engine) as session:
        yield session

# ChromaDB 初始化 (作为检索用的持久存储)
# 在 Docker 中运行，settings.vector_db_path 应为 /app/data/vector_db
if not os.path.exists(settings.vector_db_path):
    os.makedirs(settings.vector_db_path, exist_ok=True)

chroma_client = chromadb.PersistentClient(path=settings.vector_db_path)

def get_chroma_client():
    return chroma_client
