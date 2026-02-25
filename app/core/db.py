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
