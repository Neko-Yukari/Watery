import logging
import json
from typing import List, Dict, Any, Optional
from app.services.model_router import model_router
from app.services.memory_retriever import memory_retriever
from app.services.orchestrator import orchestrator
from app.models.database import Task, TaskStatus
from app.models.schemas import Message, ChatResponse

logger = logging.getLogger(__name__)

MANAGER_PROMPT = """你是一个任务编排专家 (Manager Agent)。你的任务是接收用户意图，结合可用技能和历史防错经验，将其拆解为一个有序的任务图 (DAG)。

### 可用技能 (Skills):
{skills_context}

### 历史防错经验 (Error Ledger):
{error_context}

### 用户意图:
{user_intention}

### 任务拆解要求:
1. 请将任务拆解为若干个最小可执行单元。
2. 每个任务必须指明其名称、描述以及它所依赖的其他子任务 ID（如果没有依赖则为空列表）。
3. 务必参考“历史防错经验”在任务描述中包含对应的注意事项。
4. 仅输出一个 JSON 对象，格式如下：
{{
  "tasks": [
    {{
      "id": "task_1",
      "description": "任务描述",
      "dependencies": []
    }},
    ...
  ]
}}
"""

class ManagerAgent:
    async def process_intention(self, intention: str):
        """处理用户意图并分发任务"""
        logger.info(f"Processing intention: {intention}")

        # 1. 语义检索上下文 (按需获取信息)
        context = await memory_retriever.retrieve_context(intention)
        
        # 2. 构造 Prompt
        prompt = MANAGER_PROMPT.format(
            skills_context=json.dumps(context.get("relevant_skills_ids", []), ensure_ascii=False),
            error_context="\n---\n".join(context.get("error_warnings", [])),
            user_intention=intention
        )

        # 3. 调用大模型进行拆解
        messages = [
            Message(role="system", content=prompt),
            Message(role="user", content="请根据以上信息进行任务拆解。")
        ]
        
        response: ChatResponse = await model_router.generate(messages=messages)
        
        try:
            # 提取 JSON (有时模型会返回 markdown，我们做一个简单清理)
            content = response.content.strip()
            if "```json" in content:
                content = content.split("```json")[-1].split("```")[0].strip()
            elif "```" in content:
                content = content.split("```")[-1].split("```")[0].strip()
                
            task_data = json.loads(content)
            
            # 4. 转化为 Task 对象并提交给编排器
            tasks = []
            for t_item in task_data.get("tasks", []):
                task = Task(
                    id=t_item.get("id"),
                    description=t_item.get("description"),
                    dependencies=t_item.get("dependencies", [])
                )
                tasks.append(task)
            
            if tasks:
                await orchestrator.add_tasks(tasks)
                logger.info(f"Successfully split and enqueued {len(tasks)} tasks.")
                return {"status": "success", "tasks_count": len(tasks)}
            else:
                return {"status": "error", "message": "No tasks generated."}
                
        except Exception as e:
            logger.error(f"Failed to parse task split: {str(e)}\nResponse: {response.content}")
            return {"status": "error", "message": f"Parse error: {str(e)}"}

manager_agent = ManagerAgent()
