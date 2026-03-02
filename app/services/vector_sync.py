"""
向量同步服务（Vector Sync Service）

设计原则：
  SQLite 是唯一写入目标（Source of Truth），
  ChromaDB 是可随时重建的派生语义索引。

本模块负责 SQLite → ChromaDB 的单向同步，提供三种机制：
  1. sync_error_entry()      — 单条增量同步（写入/更新后调用）
  2. remove_error_entry()    — 单条删除同步
  3. full_rebuild_errors()   — 全量重建（启动时或手动触发）
  4. sync_skill() / remove_skill() / full_rebuild_skills() — 技能同理

所有函数都是 fire-and-forget 安全的（内部 catch 异常，不会阻断主流程）。
"""

import asyncio
import logging
from typing import List, Optional

logger = logging.getLogger(__name__)


class VectorSyncService:
    """SQLite → ChromaDB 单向同步器。"""

    def __init__(self):
        self._retriever = None  # lazy init，避免循环导入

    @property
    def retriever(self):
        if self._retriever is None:
            from app.services.memory_retriever import memory_retriever
            self._retriever = memory_retriever
        return self._retriever

    # ================================================================
    # 错题集同步
    # ================================================================

    async def sync_error_entry(
        self,
        entry_id: str,
        context: str,
        correction: str,
        tags: List[str],
        severity: str = "warning",
        entry_type: str = "raw",
        status: str = "active",
    ) -> None:
        """单条错题 SQLite → ChromaDB 增量同步。"""
        try:
            await self.retriever.add_error_entry_v2(
                entry_id=entry_id,
                context=context,
                correction=correction,
                tags=tags,
                severity=severity,
                entry_type=entry_type,
                status=status,
            )
        except Exception as e:
            logger.warning(f"[VectorSync] sync_error_entry({entry_id}) failed: {e}")

    async def remove_error_entry(self, entry_id: str) -> None:
        """单条错题从 ChromaDB 删除。"""
        try:
            await self.retriever.delete_error_entry(entry_id)
        except Exception as e:
            logger.warning(f"[VectorSync] remove_error_entry({entry_id}) failed: {e}")

    async def full_rebuild_errors(self) -> int:
        """
        全量重建 ChromaDB error_ledger_vector：
        清空 → 从 SQLite ErrorEntry 表逐条写入。

        返回同步的条目数。适用于：
          - 应用启动时
          - 手动调用 POST /errors/sync
          - 发现数据不一致时
        """
        from sqlmodel import Session, select
        from app.core.db import engine
        from app.models.database import ErrorEntry

        try:
            # 1. 读取 SQLite 全部错题
            with Session(engine) as session:
                entries = session.exec(select(ErrorEntry)).all()
                # 在 session 内序列化，避免 detached 问题
                data = [
                    {
                        "id": e.id,
                        "context": e.context or "",
                        "correction": e.correction or "",
                        "tags": e.tags or [],
                        "severity": e.severity or "warning",
                        "entry_type": e.entry_type or "raw",
                        "status": e.status or "active",
                    }
                    for e in entries
                    if (e.status or "active") == "active"
                ]

            # 2. 清空 ChromaDB collection 并重写
            col = self.retriever.error_ledger_col
            # ChromaDB 没有 truncate，用 delete 全部 ids
            existing = await self.retriever._run_sync(col.get)
            if existing["ids"]:
                await self.retriever._run_sync(col.delete, ids=existing["ids"])

            # 3. 批量写入
            count = 0
            for d in data:
                doc_text = f"{d['context']}\nCorrection: {d['correction']}"
                meta = {
                    "correction": d["correction"],
                    "tags": ",".join(d["tags"]) if d["tags"] else "",
                    "severity": d["severity"],
                    "entry_id": d["id"],
                    "entry_type": d["entry_type"],
                    "status": d["status"],
                }
                await self.retriever._run_sync(
                    col.add,
                    ids=[d["id"]],
                    documents=[doc_text],
                    metadatas=[meta],
                )
                count += 1

            logger.info(f"[VectorSync] full_rebuild_errors completed: {count} entries synced.")
            return count

        except Exception as e:
            logger.error(f"[VectorSync] full_rebuild_errors failed: {e}")
            return 0

    # ================================================================
    # 技能同步
    # ================================================================

    async def sync_skill(self, skill_id: str, name: str, description: str, language: str) -> None:
        """单条技能 SQLite → ChromaDB 增量同步。"""
        from app.models.schemas import SkillManifest
        try:
            manifest = SkillManifest(
                id=skill_id,
                name=name,
                description=description,
                language=language,
                entrypoint="",
            )
            await self.retriever.add_skill(manifest)
        except Exception as e:
            logger.warning(f"[VectorSync] sync_skill({skill_id}) failed: {e}")

    async def remove_skill(self, skill_id: str) -> None:
        """单条技能从 ChromaDB 删除。"""
        try:
            await self.retriever.delete_skill(skill_id)
        except Exception as e:
            logger.warning(f"[VectorSync] remove_skill({skill_id}) failed: {e}")

    async def full_rebuild_skills(self) -> int:
        """全量重建 ChromaDB skills_vector。"""
        from sqlmodel import Session, select
        from app.core.db import engine
        from app.models.database import SkillMetadata

        try:
            with Session(engine) as session:
                skills = session.exec(select(SkillMetadata)).all()
                data = [
                    {
                        "id": s.id,
                        "name": s.name,
                        "description": s.description or "",
                        "language": s.language or "python",
                    }
                    for s in skills
                ]

            col = self.retriever.skills_col
            existing = await self.retriever._run_sync(col.get)
            if existing["ids"]:
                await self.retriever._run_sync(col.delete, ids=existing["ids"])

            count = 0
            for d in data:
                doc = f"Name: {d['name']}\nDescription: {d['description']}\nLanguage: {d['language']}"
                meta = {"language": d["language"], "name": d["name"]}
                await self.retriever._run_sync(
                    col.add,
                    ids=[d["id"]],
                    documents=[doc],
                    metadatas=[meta],
                )
                count += 1

            logger.info(f"[VectorSync] full_rebuild_skills completed: {count} skills synced.")
            return count

        except Exception as e:
            logger.error(f"[VectorSync] full_rebuild_skills failed: {e}")
            return 0


# 全局单例
vector_sync = VectorSyncService()
