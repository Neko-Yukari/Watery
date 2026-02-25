import asyncio
import logging
from datetime import datetime
from typing import List, Any, Optional

from sqlmodel import Session, select
from app.core.db import engine
from app.models.database import Task, TaskStatus

logger = logging.getLogger(__name__)


class TaskOrchestrator:
    """
    DAG 任务调度核心。
    负责：任务入队、Worker 认领、完成解锁依赖、失败级联、
    以及服务重启时的队列恢复。
    """

    def __init__(self):
        self.task_queue: asyncio.Queue = asyncio.Queue()

    # ------------------------------------------------------------------ #
    # 启动恢复
    # ------------------------------------------------------------------ #

    async def recover_pending_tasks(self):
        """
        应用启动时扫描 SQLite：
        1. 将所有 RUNNING 任务重置为 PENDING（Worker 在重启前中断）。
        2. 将依赖已完全满足的 PENDING 任务重新推入内存队列。
        """
        with Session(engine) as session:
            running_tasks = session.exec(
                select(Task).where(Task.status == TaskStatus.RUNNING)
            ).all()
            for task in running_tasks:
                task.status = TaskStatus.PENDING
                task.updated_at = datetime.utcnow()
                session.add(task)
            if running_tasks:
                session.commit()
                logger.info(f"Reset {len(running_tasks)} stuck RUNNING tasks back to PENDING.")

        with Session(engine) as session:
            pending_tasks = session.exec(
                select(Task).where(Task.status == TaskStatus.PENDING)
            ).all()
            recovered = 0
            for task in pending_tasks:
                all_deps_done = True
                for dep_id in (task.dependencies or []):
                    dep = session.exec(select(Task).where(Task.id == dep_id)).first()
                    if not dep or dep.status != TaskStatus.COMPLETED:
                        all_deps_done = False
                        break
                if all_deps_done:
                    await self._enqueue_task(task.id)
                    recovered += 1
            if recovered:
                logger.info(f"Recovered {recovered} pending tasks into queue on startup.")

    # ------------------------------------------------------------------ #
    # 任务入队
    # ------------------------------------------------------------------ #

    async def add_tasks(self, tasks: List[Task]):
        """将生成的任务图存入数据库，无依赖的任务立即入队。"""
        # 在 session 打开前提取 ID，避免 session.commit() 后 expire_on_commit=True
        # 导致 detached 对象访问 task.dependencies 抛出 DetachedInstanceError
        no_dep_ids = [t.id for t in tasks if not (t.dependencies or [])]

        with Session(engine) as session:
            for task in tasks:
                session.add(task)
            session.commit()

        for task_id in no_dep_ids:
            await self._enqueue_task(task_id)
            logger.info(f"Task {task_id} enqueued (no dependencies).")

    async def _enqueue_task(self, task_id: str):
        """将任务 ID 推入内存工作队列。"""
        await self.task_queue.put(task_id)

    # ------------------------------------------------------------------ #
    # Worker 认领 / 完成 / 失败
    # ------------------------------------------------------------------ #

    async def claim_task(self) -> Optional[Task]:
        """Worker 从队列认领任务，标记为 RUNNING。"""
        try:
            task_id = await asyncio.wait_for(self.task_queue.get(), timeout=1.0)
            with Session(engine) as session:
                task = session.exec(select(Task).where(Task.id == task_id)).first()
                if task:
                    task.status = TaskStatus.RUNNING
                    task.updated_at = datetime.utcnow()
                    session.add(task)
                    session.commit()
                    session.refresh(task)
                    return task
            return None
        except asyncio.TimeoutError:
            return None

    async def complete_task(self, task_id: str, result: Any):
        """
        Worker 完成任务后：
        1. 标记该任务为 COMPLETED 并保存结果。
        2. 解锁所有依赖已全部完成的下游 PENDING 任务，推入队列。
        """
        with Session(engine) as session:
            task = session.exec(select(Task).where(Task.id == task_id)).first()
            if task:
                task.status = TaskStatus.COMPLETED
                task.result = result
                task.updated_at = datetime.utcnow()
                session.add(task)
                session.commit()

                dependant_tasks = session.exec(
                    select(Task).where(Task.status == TaskStatus.PENDING)
                ).all()
                for d_task in dependant_tasks:
                    if task_id in (d_task.dependencies or []):
                        all_finished = True
                        for dep_id in d_task.dependencies:
                            dep = session.exec(select(Task).where(Task.id == dep_id)).first()
                            if not dep or dep.status != TaskStatus.COMPLETED:
                                all_finished = False
                                break
                        if all_finished:
                            await self._enqueue_task(d_task.id)
                            logger.info(f"Task {d_task.id} unlocked and enqueued.")

    async def fail_task(self, task_id: str, error_msg: str):
        """
        Worker 报告任务失败：
        1. 标记当前任务为 FAILED。
        2. 级联失败所有直接依赖此任务的 PENDING 下游任务（递归）。
        3. 自动将错误上下文写入 Error Ledger 向量库。
        """
        task_description = ""
        cascade_ids: List[str] = []

        with Session(engine) as session:
            task = session.exec(select(Task).where(Task.id == task_id)).first()
            if task:
                task_description = task.description
                task.status = TaskStatus.FAILED
                task.error_msg = error_msg
                task.updated_at = datetime.utcnow()
                session.add(task)
                session.commit()
                logger.warning(f"Task {task_id} FAILED: {error_msg}")

                # 收集需要级联失败的下游任务 ID
                pending = session.exec(
                    select(Task).where(Task.status == TaskStatus.PENDING)
                ).all()
                cascade_ids = [
                    t.id for t in pending if task_id in (t.dependencies or [])
                ]

        # session 关闭后递归级联，避免嵌套 session 冲突
        for cid in cascade_ids:
            await self.fail_task(cid, f"上游任务 {task_id} 失败，级联终止")

        # 自动写入 Error Ledger（延迟导入，避免模块循环引用）
        if task_description:
            try:
                from app.services.memory_retriever import memory_retriever
                await memory_retriever.add_error_entry(
                    context=f"任务描述: {task_description}\n错误信息: {error_msg}",
                    correction="请检查任务依赖的技能是否存在，或调整任务描述后重新提交。",
                )
            except Exception as e:
                logger.error(f"Failed to write error entry to ledger: {e}")


# 全局单例
orchestrator = TaskOrchestrator()
