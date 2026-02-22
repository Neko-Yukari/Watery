import sys
import os

# 将项目根目录添加到 python 路径
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from app.core.db import init_db, get_chroma_client
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

def main():
    logger.info("初始化数据库和向量检索集合...")
    
    # 初始化 SQLite
    init_db()
    
    # 初始化 ChromaDB
    client = get_chroma_client()
    
    # 创建 Skills 集合
    skills_collection = client.get_or_create_collection(name="skills_vector")
    logger.info(f"集合 {skills_collection.name} 已就绪。")
    
    # 创建 Error Ledger 集合
    error_ledger_collection = client.get_or_create_collection(name="error_ledger_vector")
    logger.info(f"集合 {error_ledger_collection.name} 已就绪。")
    
    logger.info("初始化完成。")

if __name__ == "__main__":
    main()
