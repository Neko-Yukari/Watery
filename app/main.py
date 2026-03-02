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

    # ---- error_ledger.md → SQLite + ChromaDB 幂等入库（后台异步，不阻塞 startup）----
    # 设计：SQLite 为唯一写入目标 → 再用 vector_sync 全量重建 ChromaDB
    async def _ingest_ledger():
        import re as _re
        import hashlib as _hashlib
        from sqlmodel import Session, select
        from app.core.db import engine
        from app.models.database import ErrorEntry
        from app.services.vector_sync import vector_sync

        ledger_path = "/app/error_ledger.md" if os.path.exists("/app/error_ledger.md") else "error_ledger.md"

        if not os.path.exists(ledger_path):
            logging.info("error_ledger.md 不存在，跳过迁移。")
            # 仍然全量重建 ChromaDB（可能已有 SQLite 数据）
            n = await vector_sync.full_rebuild_errors()
            logging.info(f"ChromaDB 错题向量已重建：{n} 条。")
            return

        with open(ledger_path, "r", encoding="utf-8") as f:
            content = f.read()

        # ---- 关键词 → 标签推断 ----
        _TAG_KEYWORDS = {
            "docker": ["docker", "container", "容器", "dockerfile", "docker-compose", "镜像"],
            "python": ["python", "pip", "pydantic", "numpy", "module", "importerror", "syntaxerror"],
            "network": ["proxy", "代理", "网络", "http", "api", "502", "timeout", "超时"],
            "database": ["sqlite", "chromadb", "数据库", "db", "unique", "constraint"],
            "encoding": ["编码", "utf-8", "encoding", "乱码", "charset"],
            "deployment": ["部署", "端口", "port", "deploy", "构建", "build"],
            "asyncio": ["asyncio", "queue", "队列", "worker", "任务"],
            "proxy": ["clash", "mihomo", "shadowsocks", "ss-2022", "订阅"],
            "config": ["配置", "config", "env", "环境变量", "settings"],
            "pdf": ["pdf", "pypdf", "pdfplumber"],
            "llm": ["llm", "gemini", "模型", "model", "token"],
        }

        def _infer_tags(text: str) -> list:
            text_lower = text.lower()
            tags = set()
            for tag, keywords in _TAG_KEYWORDS.items():
                for kw in keywords:
                    if kw.lower() in text_lower:
                        tags.add(tag)
                        break
            return sorted(tags) if tags else ["general"]

        field_pattern = _re.compile(r"-\s*\*\*(.+?)\*\*\s*[:：]\s*(.*?)(?=\n-\s*\*\*|\Z)", _re.DOTALL)
        sections = _re.split(r"\n### ", content)
        migrated, skipped = 0, 0

        # ---- Step 1: MD → SQLite（单写）----
        for raw in sections[1:]:
            lines = raw.strip().split("\n")
            raw_title = lines[0].strip()
            body = "\n".join(lines[1:]).strip()
            if not raw_title or not body:
                continue

            doc_id = _hashlib.md5(raw_title.encode("utf-8")).hexdigest()

            with Session(engine) as session:
                if session.get(ErrorEntry, doc_id):
                    skipped += 1
                    continue

            title = _re.sub(r"^\d+\.\s*", "", raw_title)
            fields = {}
            for m in field_pattern.finditer(body):
                fields[m.group(1).strip()] = m.group(2).strip()

            context_parts = []
            for k in ["问题描述", "发现途径", "原因分析"]:
                if k in fields:
                    context_parts.append(f"{k}: {fields[k]}")
            ctx = "\n".join(context_parts) if context_parts else body

            entry = ErrorEntry(
                id=doc_id,
                title=title,
                context=ctx,
                correction=fields.get("解决方案", ""),
                prevention=fields.get("预防建议", ""),
                tags=_infer_tags(raw_title + " " + body),
                severity="info",
                source="manual",
            )
            with Session(engine) as session:
                session.add(entry)
                session.commit()
            migrated += 1

        logging.info(f"Error Ledger → SQLite：迁移 {migrated} 条，跳过 {skipped} 条。")

        # ---- Step 2: SQLite → ChromaDB 全量重建（唯一同步入口）----
        n = await vector_sync.full_rebuild_errors()
        logging.info(f"ChromaDB 错题向量已重建：{n} 条。")

    _track(asyncio.create_task(_ingest_ledger()))

    # ---- Phase 11 — 代码语义索引：启动时增量更新（零 LLM Token 消耗）----
    from app.services.code_indexer import code_indexer
    try:
        _index_stats = await code_indexer.update_incremental()
        logging.info(f"Code index updated on startup: {_index_stats}")
    except Exception as _idx_err:
        logging.warning(f"Code index startup update failed (non-fatal): {_idx_err}")

    # 开发环境：后台定时文件监听（每 30 秒自动增量更新）
    if settings.environment == "development":
        _track(asyncio.create_task(code_indexer.start_file_watcher(interval=30.0)))
        logging.info("Code index file watcher started (development mode).")

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
