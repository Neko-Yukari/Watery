from fastapi import APIRouter, HTTPException
from app.models.schemas import ChatRequest, ChatResponse, IntentionRequest
from app.services.model_router import model_router
from app.services.manager import manager_agent
from app.services.proxy_manager import proxy_manager
from sqlmodel import Session, select
from app.core.db import engine
from app.models.database import Task
import logging

router = APIRouter()
logger = logging.getLogger(__name__)

@router.post("/chat", response_model=ChatResponse, summary="直连聊天接口")
async def chat_endpoint(request: ChatRequest):
    """
    接收聊天请求，通过 ModelRouter 动态路由到合适的模型提供商。
    """
    try:
        response = await model_router.generate(
            messages=request.messages,
            model=request.model,
            temperature=request.temperature,
            max_tokens=request.max_tokens
        )
        return response
    except Exception as e:
        logger.error(f"Chat endpoint error: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))

@router.post("/intention", summary="意图分发接口 (Phase 2)")
async def intention_endpoint(request: IntentionRequest):
    """
    接收用户大的意图需求，由 Manager Agent 拆解任务并分发至工作池。
    """
    try:
        result = await manager_agent.process_intention(request.intention)
        return result
    except Exception as e:
        logger.error(f"Intention endpoint error: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))

@router.get("/tasks", summary="查看所有任务状态")
async def get_tasks():
    with Session(engine) as session:
        tasks = session.exec(select(Task)).all()
        return tasks

@router.get("/proxy/status", summary="查看代理健康状态")
async def get_proxy_status():
    """返回当前代理池的连接健康状态"""
    return await proxy_manager.get_health_status()

@router.get("/models", summary="获取可用模型列表")
async def get_models():
    """
    返回当前系统支持的模型列表。
    """
    return {
        "available_models": model_router.available_models,
        "default_model": model_router.default_model
    }
