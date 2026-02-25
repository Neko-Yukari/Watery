import asyncio
import logging
import os
from contextlib import asynccontextmanager
from typing import Set

from fastapi import FastAPI
from fastapi.responses import HTMLResponse

from app.api.routes import router as api_router
from app.core.config import settings
from app.core.db import init_db
from app.services.proxy_manager import proxy_manager

# ---- 日志 ----
logging.basicConfig(
    level=getattr(logging, settings.log_level.upper(), logging.INFO),
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)


# ============================================================
# Lifespan：替代废弃的 @app.on_event("startup")
# 同时持有 asyncio.Task 引用防止 GC 静默取消
# ============================================================

_background_tasks: Set[asyncio.Task] = set()

def _track(t: asyncio.Task) -> asyncio.Task:
    _background_tasks.add(t)
    t.add_done_callback(_background_tasks.discard)
    return t


@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    应用生命周期钩子。
    startup：初始化 DB → 恢复任务队列 → 启动 3 个 Worker → 启动 ProxyManager。
    shutdown：优雅取消所有后台 Task，避免任务状态脏写。
    """
    # ---- startup ----
    init_db()
    logging.info("SQLite 数据库初始化完成。")

    from app.services.orchestrator import orchestrator
    await orchestrator.recover_pending_tasks()
    logging.info("任务队列恢复完成。")

    from app.services.worker import WorkerAgent
    for i in range(1, 4):
        _track(asyncio.create_task(WorkerAgent(name=f"Worker-{i:02d}").start()))
    logging.info("Worker-01 ~ Worker-03 已启动。")

    _track(asyncio.create_task(proxy_manager.start_loop()))
    logging.info("ProxyManager 后台同步已启用。")

    # ---- error_ledger.md → ChromaDB 幂等入库 ----
    from app.services.memory_retriever import memory_retriever
    ledger_path = "/app/error_ledger.md" if os.path.exists("/app/error_ledger.md") else "error_ledger.md"
    n = await memory_retriever.ingest_error_ledger(ledger_path)
    logging.info(f"Error Ledger 已入库 ChromaDB：{n} 条条目。")

    yield  # ← 应用正常运行期间停在这里

    # ---- shutdown ----
    logging.info(f"正在取消 {len(_background_tasks)} 个后台任务...")
    tasks_to_cancel = list(_background_tasks)
    for t in tasks_to_cancel:
        t.cancel()
    await asyncio.gather(*tasks_to_cancel, return_exceptions=True)
    logging.info("所有后台任务已取消，服务优雅退出。")


# ============================================================
# FastAPI 应用
# ============================================================

app = FastAPI(
    title="Watery AI Agent System",
    description="高度自动化的个人 AI 代理系统后端 API",
    version="0.2.0",
    lifespan=lifespan,
)

# 注册 API 路由
app.include_router(api_router, prefix="/api/v1")


@app.get("/", response_class=HTMLResponse, summary="前端主页")
async def get_index():
    index_path = os.path.join(os.path.dirname(__file__), "web", "index.html")
    with open(index_path, "r", encoding="utf-8") as f:
        return f.read()


@app.get("/health", summary="健康检查")
async def health_check():
    return {"status": "ok", "version": "0.2.0", "environment": settings.environment}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app.main:app", host="0.0.0.0", port=18000, reload=True)
