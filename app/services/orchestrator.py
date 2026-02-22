import asyncio
import uuid
import logging
from typing import List, Dict, Any, Optional
from sqlmodel import Session, select, func
from app.core.db import engine
from app.models.database import Task, TaskStatus

logger = logging.getLogger(__name__)

class TaskOrchestrator:
    def __init__(self):
        self.task_queue = asyncio.Queue()  # 单机模式下的轻量级任务队列

    async def add_tasks(self, tasks: List[Task]):
        """将生成的任务图存入数据库并分析依赖"""
        with Session(engine) as session:
            for task in tasks:
                session.add(task)
            session.commit()
            
            # 记录此时所有任务，找出无依赖的推送进队列
            for task in tasks:
                if not task.dependencies:
                    # 将无依赖任务（或是已经解锁的任务）推入就绪队列
                    await self._enqueue_task(task.id)
                    logger.info(f"Task {task.id} enqueued (no dependencies).")

    async def _enqueue_task(self, task_id: str):
        # 将任务 ID 推送进工作池供 Worker 取用
        await self.task_queue.put(task_id)

    async def claim_task(self) -> Optional[Task]:
        """Worker 认领任务"""
        try:
            task_id = await asyncio.wait_for(self.task_queue.get(), timeout=1.0)
            with Session(engine) as session:
                task = session.exec(select(Task).where(Task.id == task_id)).first()
                if task:
                    task.status = TaskStatus.RUNNING
                    session.add(task)
                    session.commit()
                    session.refresh(task)
                    return task
            return None
        except asyncio.TimeoutError:
            return None

    async def complete_task(self, task_id: str, result: Any):
        """Worker 完成任务并解锁依赖于该任务的其他子任务"""
        with Session(engine) as session:
            task = session.exec(select(Task).where(Task.id == task_id)).first()
            if task:
                task.status = TaskStatus.COMPLETED
                task.result = result
                session.add(task)
                session.commit()
                
                # 解锁依赖于本任务的其他任务
                # 这里是一个简单的逻辑：查询所有处于 PENDING 且依赖包含本 ID 的任务
                # 如果这些任务的所有依赖都已经 COMPLETED，则推入 Queue
                dependant_tasks = session.exec(
                    select(Task).where(Task.status == TaskStatus.PENDING)
                ).all()
                
                for d_task in dependant_tasks:
                    if task_id in d_task.dependencies:
                        # 检查此任务的所有依赖是否完成
                        all_deps_finished = True
                        for dep_id in d_task.dependencies:
                            dep = session.exec(select(Task).where(Task.id == dep_id)).first()
                            if not dep or dep.status != TaskStatus.COMPLETED:
                                all_deps_finished = False
                                break
                        
                        if all_deps_finished:
                            await self._enqueue_task(d_task.id)
                            logger.info(f"Task {d_task.id} unlocked and enqueued.")

    async def fail_task(self, task_id: str, error_msg: str):
        """Worker 报告任务失败"""
        with Session(engine) as session:
            task = session.exec(select(Task).where(Task.id == task_id)).first()
            if task:
                task.status = TaskStatus.FAILED
                task.error_msg = error_msg
                session.add(task)
                session.commit()
                logger.error(f"Task {task_id} failed: {error_msg}")

orchestrator = TaskOrchestrator()
