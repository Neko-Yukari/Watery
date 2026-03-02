import asyncio
import hashlib
import logging
import re
import uuid
from functools import partial
from typing import List, Dict, Any, Optional

from app.core.db import get_chroma_client
from app.models.schemas import SkillManifest
from app.core.config import settings

logger = logging.getLogger(__name__)


class MemoryRetriever:
    """
    RAG 记忆检索层。
    所有 ChromaDB 调用均通过 run_in_executor 推入线程池，
    避免同步阻塞 asyncio 事件循环。
    """

    def __init__(self):
        self.chroma_client = get_chroma_client()
        self.skills_col = self.chroma_client.get_or_create_collection(name="skills_vector")
        self.error_ledger_col = self.chroma_client.get_or_create_collection(name="error_ledger_vector")

    # ------------------------------------------------------------------ #
    # 内部工具：线程池包装器
    # ------------------------------------------------------------------ #

    async def _run_sync(self, fn, *args, **kwargs):
        """在默认线程池中运行同步函数，返回其结果。"""
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, partial(fn, *args, **kwargs))

    # ------------------------------------------------------------------ #
    # Skills（技能库）
    # ------------------------------------------------------------------ #

    async def add_skill(self, skill: SkillManifest) -> None:
        """将技能写入向量库（若已存在则更新）。"""
        doc = (
            f"Name: {skill.name}\n"
            f"Description: {skill.description}\n"
            f"Language: {skill.language}"
        )
        meta = {"language": skill.language, "name": skill.name}

        existing = await self._run_sync(self.skills_col.get, ids=[skill.id])
        if existing["ids"]:
            await self._run_sync(
                self.skills_col.update,
                ids=[skill.id],
                documents=[doc],
                metadatas=[meta],
            )
            logger.info(f"Skill {skill.id} updated in vector store.")
        else:
            await self._run_sync(
                self.skills_col.add,
                ids=[skill.id],
                documents=[doc],
                metadatas=[meta],
            )
            logger.info(f"Skill {skill.id} added to vector store.")

    async def delete_skill(self, skill_id: str) -> None:
        """从向量库中删除技能。"""
        await self._run_sync(self.skills_col.delete, ids=[skill_id])
        logger.info(f"Skill {skill_id} removed from vector store.")

    # ------------------------------------------------------------------ #
    # Error Ledger（错题集）
    # ------------------------------------------------------------------ #

    async def add_error_entry(
        self,
        context: str,
        correction: str,
        skill_id: Optional[str] = None,
    ) -> None:
        """将一条错误经历写入 Error Ledger 向量库（旧接口，向后兼容）。"""
        doc_id = str(uuid.uuid4())
        meta = {
            "correction": correction,
            "skill_id": skill_id or "general",
            "tags": "",
            "severity": "warning",
            "entry_id": doc_id,
        }
        await self._run_sync(
            self.error_ledger_col.add,
            ids=[doc_id],
            documents=[context],
            metadatas=[meta],
        )
        logger.info(f"Error entry {doc_id} added to ledger.")

    async def add_error_entry_v2(
        self,
        entry_id: str,
        context: str,
        correction: str,
        tags: List[str],
        severity: str = "warning",
        entry_type: str = "raw",
        status: str = "active",
    ) -> None:
        """
        将错题写入 ChromaDB error_ledger_vector（带 tags metadata，Phase 8）。
        tags 存为逗号分隔字符串供 ChromaDB where 过滤使用。
        """
        doc_text = f"{context}\nCorrection: {correction}"
        meta = {
            "correction": correction,
            "tags": ",".join(tags) if tags else "",
            "severity": severity,
            "entry_id": entry_id,
            "entry_type": entry_type,
            "status": status,
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
        logger.info(f"Error entry v2 {entry_id} synced to ChromaDB (tags={tags}).")

    async def delete_error_entry(self, entry_id: str) -> None:
        """从 ChromaDB 删除错题条目。"""
        try:
            await self._run_sync(self.error_ledger_col.delete, ids=[entry_id])
            logger.info(f"Error entry {entry_id} deleted from ChromaDB.")
        except Exception as e:
            logger.warning(f"delete_error_entry: {e}")

    async def retrieve_errors_by_tags(
        self,
        task_description: str,
        tags: List[str],
        top_k: int = 3,
    ) -> List[Dict[str, Any]]:
        """
        按标签精确筛选 + 语义排序，返回最相关的错题列表（Phase 8）。

        流程：
          1. 用 ChromaDB where 过滤：tags 字段包含指定标签（OR 逻辑）
          2. 在候选集内做语义相似度排序
          3. 返回 top_k 条

        Returns:
            [{"entry_id": ..., "context": ..., "correction": ..., "tags": [...]}, ...]
        """
        error_count = await self._run_sync(self.error_ledger_col.count)
        if error_count == 0:
            return []

        k = min(top_k, error_count)

        # 构建 where 过滤：tags 逗号字符串包含任一指定标签
        where_filter: Optional[Dict] = None
        if tags:
            if len(tags) == 1:
                where_filter = {"tags": {"$contains": tags[0]}}
            else:
                where_filter = {"$or": [{"tags": {"$contains": t}} for t in tags]}

        try:
            results = await self._run_sync(
                self.error_ledger_col.query,
                query_texts=[task_description],
                n_results=k,
                where=where_filter,
                include=["documents", "metadatas"],
            )
        except Exception as e:
            logger.warning(f"retrieve_errors_by_tags filtered query failed ({e}), falling back to unfiltered")
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
                raw_tags = meta.get("tags", "")
                entries.append({
                    "entry_id": meta.get("entry_id", doc_id),
                    "context": (results["documents"][0][i] if results["documents"] else ""),
                    "correction": meta.get("correction", ""),
                    "tags": [t for t in raw_tags.split(",") if t] if raw_tags else [],
                    "entry_type": meta.get("entry_type", "raw"),
                    "status": meta.get("status", "active"),
                })
        return entries

    # ------------------------------------------------------------------ #
    # Context Retrieval（上下文检索）
    # ------------------------------------------------------------------ #

    async def retrieve_context(
        self,
        task_description: str,
        top_k_skills: int = 3,
        top_k_errors: int = 2,
    ) -> Dict[str, Any]:
        """
        语义检索与当前任务最相关的技能 ID 列表和历史错误警告。
        若集合为空则安全返回空结果，不抛出异常。
        """
        # ---- Skills ----
        skill_count = await self._run_sync(self.skills_col.count)
        if skill_count == 0:
            skill_results: Dict = {"ids": [[]], "metadatas": [[]], "distances": [[]]}
        else:
            k = min(top_k_skills, skill_count)
            skill_results = await self._run_sync(
                self.skills_col.query,
                query_texts=[task_description],
                n_results=k,
                include=["metadatas", "distances"],
            )

        # ---- Errors ----
        error_count = await self._run_sync(self.error_ledger_col.count)
        if error_count == 0:
            error_results: Dict = {"documents": [[]], "metadatas": [[]]}
        else:
            k = min(top_k_errors, error_count)
            error_results = await self._run_sync(
                self.error_ledger_col.query,
                query_texts=[task_description],
                n_results=k,
                where={"status": "active"},
            )

        # ---- 整理结果 ----
        relevant_skill_ids: List[str] = []
        skill_distances: List[float] = []

        if skill_results["ids"] and skill_results["ids"][0]:
            relevant_skill_ids = skill_results["ids"][0]
            skill_distances = (skill_results.get("distances") or [[]])[0]

        error_warnings: List[str] = []
        if error_results["documents"] and error_results["documents"][0]:
            for i, doc in enumerate(error_results["documents"][0]):
                correction = ""
                if error_results["metadatas"] and error_results["metadatas"][0]:
                    correction = error_results["metadatas"][0][i].get("correction", "")
                error_warnings.append(f"Context: {doc}\nCorrection: {correction}")

        return {
            "relevant_skills_ids": relevant_skill_ids,
            "skill_distances": skill_distances,   # L2 distances（越小越相似）
            "error_warnings": error_warnings,
        }

    # ------------------------------------------------------------------ #
    # Error Ledger Ingest（批量入库 error_ledger.md）
    # ------------------------------------------------------------------ #

    async def ingest_error_ledger(self, file_path: str) -> int:
        """
        解析 error_ledger.md 并将每个 ### 级条目以幂等方式写入向量库。

        每条条目用标题的 MD5 作为 doc_id（确保重复调用不产生重复记录）。
        返回成功写入/更新的条目数。
        """
        if not __import__("os").path.exists(file_path):
            logger.warning(f"Error ledger file not found: {file_path}")
            return 0

        with open(file_path, "r", encoding="utf-8") as f:
            content = f.read()

        # 按 "### " 分割，取每个 h3 段落
        sections = re.split(r"\n### ", content)
        count = 0
        for raw in sections[1:]:  # sections[0] 是文件头部，跳过
            lines = raw.strip().split("\n")
            title = lines[0].strip()
            body = "\n".join(lines[1:]).strip()
            if not title or not body:
                continue

            doc_id = hashlib.md5(title.encode("utf-8")).hexdigest()
            doc_text = f"{title}\n{body}"
            meta = {"correction": title, "skill_id": "ledger"}

            existing = await self._run_sync(self.error_ledger_col.get, ids=[doc_id])
            if existing["ids"]:
                await self._run_sync(
                    self.error_ledger_col.update,
                    ids=[doc_id],
                    documents=[doc_text],
                    metadatas=[meta],
                )
            else:
                await self._run_sync(
                    self.error_ledger_col.add,
                    ids=[doc_id],
                    documents=[doc_text],
                    metadatas=[meta],
                )
            count += 1

        logger.info(f"Error ledger ingested: {count} entries from {file_path}")
        return count


# 全局单例
memory_retriever = MemoryRetriever()
