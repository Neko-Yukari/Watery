import logging
import uuid
from typing import List, Dict, Any, Optional
from app.core.db import get_chroma_client
from app.models.schemas import SkillManifest
from app.core.config import settings

logger = logging.getLogger(__name__)

class MemoryRetriever:
    def __init__(self):
        self.chroma_client = get_chroma_client()
        # 获取或创建集合
        self.skills_col = self.chroma_client.get_or_create_collection(name="skills_vector")
        self.error_ledger_col = self.chroma_client.get_or_create_collection(name="error_ledger_vector")

    async def add_skill(self, skill: SkillManifest):
        """将技能添加到向量库中"""
        self.skills_col.add(
            ids=[skill.id],
            documents=[f"Name: {skill.name}\nDescription: {skill.description}\nLanguage: {skill.language}"],
            metadatas=[{"id": skill.id, "language": skill.language}]
        )
        logger.info(f"Skill {skill.id} added to vector store.")

    async def add_error_entry(self, context: str, correction: str, skill_id: Optional[str] = None):
        """将错误记录添加到 Error Ledger"""
        doc_id = str(uuid.uuid4())
        self.error_ledger_col.add(
            ids=[doc_id],
            documents=[context],
            metadatas=[{"correction": correction, "skill_id": skill_id or "general"}]
        )
        logger.info(f"Error entry {doc_id} added to ledger.")

    async def retrieve_context(self, task_description: str, top_k_skills: int = 3, top_k_errors: int = 2) -> Dict[str, Any]:
        """
        进行语义检索，获取相关的技能和防错提示。
        """
        # 检索相关技能
        skill_results = self.skills_col.query(
            query_texts=[task_description],
            n_results=top_k_skills
        )
        
        # 检索相关错误案例
        error_results = self.error_ledger_col.query(
            query_texts=[task_description],
            n_results=top_k_errors
        )
        
        # 整理结果
        relevant_skills = []
        if skill_results['ids'] and len(skill_results['ids'][0]) > 0:
            for i in range(len(skill_results['ids'][0])):
                relevant_skills.append({
                    "id": skill_results['ids'][0][i],
                    "metadata": skill_results['metadatas'][0][i] if skill_results['metadatas'] else {}
                })
        
        error_warnings = []
        if error_results['documents'] and len(error_results['documents'][0]) > 0:
            for i in range(len(error_results['documents'][0])):
                context = error_results['documents'][0][i]
                correction = error_results['metadatas'][0][i].get('correction', '')
                error_warnings.append(f"Context: {context}\nCorrection: {correction}")

        return {
            "relevant_skills_ids": [s['id'] for s in relevant_skills],
            "error_warnings": error_warnings
        }

# 全局单例
memory_retriever = MemoryRetriever()
