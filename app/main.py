from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from app.api.routes import router as api_router
from app.core.config import settings
from app.core.db import init_db
from app.services.worker import worker_agent
from app.services.proxy_manager import proxy_manager
import logging
import os
import asyncio

# 配置日志
logging.basicConfig(
    level=getattr(logging, settings.log_level.upper(), logging.INFO),
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)

app = FastAPI(
    title="Watery AI Agent System",
    description="高度自动化的个人 AI 代理系统后端 API",
    version="0.1.0",
)

@app.on_event("startup")
async def startup_event():
    # 1. 初始化数据库
    init_db()
    logging.info("SQLite 数据库初始化完成。")
    
    # 2. 在后台进程启动一个 Worker Agent 
    asyncio.create_task(worker_agent.start())
    logging.info("Worker-01 启动。")

    # 3. 启动代理管理器定期更新订阅和测速
    asyncio.create_task(proxy_manager.start_loop())
    logging.info("ProxyManager 后台同步已启用。")

# 注册路由
app.include_router(api_router, prefix="/api/v1")

# 挂载静态文件和前端页面
@app.get("/", response_class=HTMLResponse, summary="前端主页")
async def get_index():
    index_path = os.path.join(os.path.dirname(__file__), "web", "index.html")
    with open(index_path, "r", encoding="utf-8") as f:
        return f.read()

@app.get("/health", summary="健康检查")
async def health_check():
    return {"status": "ok", "environment": settings.environment}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app.main:app", host="0.0.0.0", port=8000, reload=True)
