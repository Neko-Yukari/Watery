import chromadb
from sqlmodel import create_engine, Session, SQLModel
from sqlalchemy import select
from app.core.config import settings
import os

# SQLite 初始化
engine = create_engine(settings.sqlite_db_path, echo=False)

def init_db():
    SQLModel.metadata.create_all(engine)

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
