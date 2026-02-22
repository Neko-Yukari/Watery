import asyncio
import logging
from typing import List, Dict, Any, Optional
from app.services.orchestrator import orchestrator
from app.services.executor import skill_executor
from app.services.model_router import model_router
from app.models.database import Task, TaskStatus

logger = logging.getLogger(__name__)

class WorkerAgent:
    def __init__(self, name: str = "Worker-01"):
        self.name = name
        self.running = False

    async def start(self):
        """开始执行轮询，获取任务并执行"""
        self.running = True
        logger.info(f"{self.name} started.")
        
        while self.running:
            task = await orchestrator.claim_task()
            if task:
                logger.info(f"{self.name} claimed task: {task.id}")
                await self.execute_task(task)
            else:
                # 如果没有任务，则稍作等待
                await asyncio.sleep(2)

    async def execute_task(self, task: Task):
        """
        根据任务描述执行具体的操作。
        在 Phase 2 中，我们可以先实现一个基础的逻辑：
        Worker 使用大模型解释任务，并决定调用哪个技能（如果有可用技能的话）。
        现在我们先实现一个最简化的：直接执行描述。
        """
        try:
            # 第一阶段：先简单地将任务描述发送给大模型处理并记录结果
            # 未来这里将整合 Skill 匹配和调用逻辑
            from app.models.schemas import Message, ChatResponse
            
            prompt = f"你是执行单元 {self.name}。你的当前任务是：\n{task.description}\n\n请直接模拟执行该任务并返回结果（如果是复杂任务，请尽量详细）。"
            
            messages = [
                Message(role="system", content=prompt),
                Message(role="user", content="开始执行。")
            ]
            
            response: ChatResponse = await model_router.generate(messages=messages)
            
            # TODO: 真正执行 Skill (如果存在匹配的 Skill)
            # 在 Phase 2 中我们先模拟成功
            
            await orchestrator.complete_task(task.id, response.content)
            logger.info(f"{self.name} completed task: {task.id}")
            
        except Exception as e:
            logger.error(f"Worker {self.name} error: {str(e)}")
            await orchestrator.fail_task(task.id, str(e))

worker_agent = WorkerAgent()
