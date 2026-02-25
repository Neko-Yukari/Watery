import asyncio
import json
import logging
import os
import uuid
from typing import Optional, Set

from fastapi import APIRouter, Body, File, HTTPException, UploadFile

from app.models.schemas import (
    ChatRequest,
    ChatResponse,
    CodeGenRequest,
    CodeGenResponse,
    DeepResearchRequest,
    DeepResearchResponse,
    IntentionRequest,
    Message,
    MSAgentTaskListItem,
    MSAgentTaskStatus,
    PDFToSkillsRequest,
    SkillCreate,
    SkillUpdate,
)
from app.services.model_router import model_router
from app.services.manager import manager_agent
from app.services.proxy_manager import proxy_manager
from app.services.memory_retriever import memory_retriever
from app.services.executor import skill_executor
from app.services.skill_loader import skill_loader
from app.services.ms_agent_service import ms_agent_service
from app.services.tool_registry import tool_registry
from sqlmodel import Session, select
from app.core.db import engine
from app.models.database import PDFDocument, Task, SkillMetadata

router = APIRouter()
logger = logging.getLogger(__name__)

# ---- asyncio.create_task 引用持有集合（防止 GC 静默取消后台任务）----
_background_tasks: Set[asyncio.Task] = set()

def _track_task(t: asyncio.Task) -> asyncio.Task:
    """注册后台 Task，完成后自动从集合移除。"""
    _background_tasks.add(t)
    t.add_done_callback(_background_tasks.discard)
    return t


# ============================================================
# 对话 / 意图
# ============================================================

# 工具调用最大循环轮次（防止 LLM 无限循环调用工具）
_MAX_TOOL_ROUNDS = 5


@router.post("/chat", response_model=ChatResponse, summary="直连聊天接口（支持 Tool Calling）")
async def chat_endpoint(request: ChatRequest):
    """
    通过 ModelRouter 动态路由到合适的模型提供商。

    **Tool Calling 流程**（当技能库已有注册技能时自动启用）：
    1. 获取全部注册技能（SQLite），转换为 OpenAI tool definitions
    2. 将 tools 列表随请求一起发给 LLM
    3. LLM 返回 `finish_reason=tool_calls` 时：
       - 解析 function.name 找到对应 SkillMetadata
       - 解析 function.arguments 作为 params
       - 调用 SkillExecutor 执行对应技能脚本
       - 将执行结果以 role=tool 消息追加到对话
       - 重新调用 LLM 生成最终回复
    4. LLM 返回 `finish_reason=stop` 时直接返回

    工具库为空时直接返回纯文本（兼容现有行为）。
    """
    try:
        # 获取工具定义（TTL 缓存，不频繁查库）
        tools = tool_registry.get_tool_definitions()

        messages: list = list(request.messages)
        all_tool_results: list = []
        last_response: Optional[ChatResponse] = None

        for round_num in range(_MAX_TOOL_ROUNDS):
            last_response = await model_router.generate(
                messages=messages,
                model=request.model,
                temperature=request.temperature,
                max_tokens=request.max_tokens,
                tools=tools if tools else None,
            )

            # 无工具调用，直接返回
            if not last_response.tool_calls:
                last_response.tool_results = all_tool_results or None
                return last_response

            logger.info(
                f"chat_endpoint: round {round_num + 1}, "
                f"{len(last_response.tool_calls)} tool call(s) requested."
            )

            # 将 assistant 的 tool_calls 消息追加到对话
            messages.append(
                Message(
                    role="assistant",
                    content=last_response.content,
                    tool_calls=last_response.tool_calls,
                )
            )

            # 逐一执行工具调用
            for tc in last_response.tool_calls:
                skill = tool_registry.get_tool_by_name(tc.function.name)

                if skill:
                    try:
                        params = json.loads(tc.function.arguments)
                    except json.JSONDecodeError:
                        params = {}
                        logger.warning(
                            f"chat_endpoint: invalid JSON arguments for tool "
                            f"'{tc.function.name}': {tc.function.arguments!r}"
                        )

                    exec_result = await skill_executor.run(
                        language=skill.language,
                        entrypoint=skill.entrypoint,
                        params=params,
                        timeout=60,
                    )
                    logger.info(
                        f"chat_endpoint: tool '{tc.function.name}' "
                        f"status={exec_result.get('status')}"
                    )
                else:
                    exec_result = {
                        "status": "error",
                        "message": f"Tool '{tc.function.name}' not found in skill registry.",
                    }
                    logger.warning(
                        f"chat_endpoint: unknown tool '{tc.function.name}' requested by LLM."
                    )

                all_tool_results.append({
                    "tool_call_id": tc.id,
                    "tool_name": tc.function.name,
                    "result": exec_result,
                })

                # 将工具执行结果以 role=tool 消息写回对话
                messages.append(
                    Message(
                        role="tool",
                        content=json.dumps(exec_result, ensure_ascii=False),
                        tool_call_id=tc.id,
                    )
                )

        # 超出最大轮次，返回最后一次响应（附带工具结果）
        logger.warning(
            f"chat_endpoint: reached max tool rounds ({_MAX_TOOL_ROUNDS}), returning last response."
        )
        if last_response:
            last_response.tool_results = all_tool_results or None
            return last_response

        raise HTTPException(status_code=500, detail="Tool calling loop exhausted without a response.")

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Chat endpoint error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/intention", summary="意图分发接口")
async def intention_endpoint(request: IntentionRequest):
    """
    接收用户意图，Manager Agent 同步拆解任务并返回 task_ids。
    前端可据此轮询各任务完成状态，最终将结果回显在对话中。
    """
    result = await manager_agent.process_intention(request.intention)
    return result


# ============================================================
# 任务看板
# ============================================================

@router.get("/tasks", summary="查看所有任务状态")
async def get_tasks():
    """返回全量任务列表（含状态、依赖、时间戳、结果）。"""
    with Session(engine) as session:
        tasks = session.exec(select(Task)).all()
        return tasks


# ============================================================
# 技能库 CRUD
# ============================================================

@router.get("/skills", summary="获取技能库列表")
async def get_skills():
    """返回 SQLite 中注册的所有技能。"""
    with Session(engine) as session:
        skills = session.exec(select(SkillMetadata)).all()
        return {"skills": skills}


@router.post("/skills", summary="注册新技能", status_code=201)
async def create_skill(request: SkillCreate):
    """
    注册一个新技能：
    1. 写入 SQLite SkillMetadata 表（含 description 字段用于向量匹配）。
    2. 写入 ChromaDB skills_vector 集合。
    3. 若提供 script_content 则自动将内容写入 entrypoint 指定的文件。
    """
    # ---- 检查 ID 冲突 ----
    with Session(engine) as session:
        existing = session.exec(
            select(SkillMetadata).where(SkillMetadata.id == request.id)
        ).first()
        if existing:
            raise HTTPException(
                status_code=409, detail=f"Skill '{request.id}' already exists."
            )

    # ---- 写入脚本文件（可选）----
    if request.script_content:
        script_path = os.path.join(
            "/app" if os.path.exists("/app") else ".", request.entrypoint
        )
        os.makedirs(os.path.dirname(script_path), exist_ok=True)
        with open(script_path, "w", encoding="utf-8") as f:
            f.write(request.script_content)
        logger.info(f"Skill script written to {script_path}")

    # ---- 写入 SQLite ----
    skill_meta = SkillMetadata(
        id=request.id,
        name=request.name,
        language=request.language,
        entrypoint=request.entrypoint,
        description=request.description,
        parameters_schema=request.parameters_schema,
        skill_type=request.skill_type,
        knowledge_content=request.knowledge_content,
    )
    with Session(engine) as session:
        session.add(skill_meta)
        session.commit()

    # ---- 写入 ChromaDB（via SkillManifest 兼容接口）----
    from app.models.schemas import SkillManifest
    skill_manifest = SkillManifest(
        id=request.id,
        name=request.name,
        description=request.description,
        language=request.language,
        content=request.script_content or "",
    )
    await memory_retriever.add_skill(skill_manifest)
    tool_registry.invalidate_cache()
    logger.info(f"Skill '{request.id}' registered successfully.")
    return {"status": "created", "skill_id": request.id}


# ---- 注意：以下固定路径路由必须在 /{skill_id} 参数路由之前 ----

@router.post("/skills/load-dir", summary="从 SKILL.md 目录批量导入技能", status_code=201)
async def load_skills_from_dir(skills_dir: str = "/app/skills"):
    """
    扫描 skills_dir 下所有含 SKILL.md 的子目录，解析并注册到系统中。
    遵循 Anthropic Agent Skills 协议文件夹格式：
        skill-name/
        ├── SKILL.md          # 必需，含 YAML frontmatter + Markdown 正文
        ├── scripts/          # 可选，可执行脚本
        └── resources/        # 可选，资源文件

    已注册的技能会跳过（不覆盖）；若要更新请先删除再重新导入。
    """
    parsed = skill_loader.load_dir(skills_dir)
    if not parsed:
        return {"status": "ok", "loaded": 0, "skipped": 0, "errors": [],
                "message": f"No SKILL.md found in {skills_dir}"}

    loaded, skipped, errors = 0, 0, []
    from app.models.schemas import SkillManifest
    for req in parsed:
        # 检查是否已存在
        with Session(engine) as session:
            if session.exec(select(SkillMetadata).where(SkillMetadata.id == req.id)).first():
                skipped += 1
                continue
        # 写入脚本文件
        if req.script_content and req.entrypoint:
            try:
                os.makedirs(os.path.dirname(req.entrypoint), exist_ok=True)
                with open(req.entrypoint, "w", encoding="utf-8") as f:
                    f.write(req.script_content)
            except OSError as e:
                errors.append(f"{req.id}: script write failed: {e}")
        # 写入 SQLite
        skill_meta = SkillMetadata(
            id=req.id, name=req.name, language=req.language,
            entrypoint=req.entrypoint, description=req.description,
            parameters_schema=req.parameters_schema,
        )
        with Session(engine) as session:
            session.add(skill_meta)
            session.commit()
        # 写入 ChromaDB
        skill_manifest = SkillManifest(
            id=req.id, name=req.name, description=req.description,
            language=req.language, content=req.script_content or "",
        )
        await memory_retriever.add_skill(skill_manifest)
        loaded += 1
        logger.info(f"Skill '{req.id}' imported from SKILL.md.")

    tool_registry.invalidate_cache()
    return {"status": "ok", "loaded": loaded, "skipped": skipped, "errors": errors}


@router.get("/skills/{skill_id}", summary="获取单个技能详情")
async def get_skill(skill_id: str):
    """返回 SQLite 中指定技能的完整元数据。"""
    with Session(engine) as session:
        skill = session.exec(
            select(SkillMetadata).where(SkillMetadata.id == skill_id)
        ).first()
    if not skill:
        raise HTTPException(status_code=404, detail=f"Skill '{skill_id}' not found.")
    return skill


@router.post("/skills/{skill_id}/run", summary="直接执行技能")
async def run_skill(skill_id: str, params: Optional[dict] = Body(default=None)):
    """
    直接调用指定技能的脚本并返回执行结果。

    请求体（可选 JSON 对象）会作为参数透传给脚本，格式应符合该技能
    `parameters_schema` 的定义。若不传则使用空参数 `{}`。

    返回：
        {"status": "success", "result": ..., "skill_id": ..., "language": ...}
        {"status": "error",   "message": ..., "skill_id": ...}
    """
    if params is None:
        params = {}

    with Session(engine) as session:
        skill = session.exec(
            select(SkillMetadata).where(SkillMetadata.id == skill_id)
        ).first()
    if not skill:
        raise HTTPException(status_code=404, detail=f"Skill '{skill_id}' not found.")

    exec_result = await skill_executor.run(
        language=skill.language,
        entrypoint=skill.entrypoint,
        params=params,
        timeout=60,
    )
    return {
        **exec_result,
        "skill_id": skill_id,
        "language": skill.language,
    }


@router.delete("/skills/{skill_id}", summary="删除技能")
async def delete_skill(skill_id: str):
    """从 SQLite 和 ChromaDB 中同步删除指定技能。"""
    with Session(engine) as session:
        skill = session.exec(
            select(SkillMetadata).where(SkillMetadata.id == skill_id)
        ).first()
        if not skill:
            raise HTTPException(status_code=404, detail=f"Skill '{skill_id}' not found.")
        session.delete(skill)
        session.commit()

    await memory_retriever.delete_skill(skill_id)
    tool_registry.invalidate_cache()
    logger.info(f"Skill '{skill_id}' deleted.")
    return {"status": "deleted", "skill_id": skill_id}


@router.put("/skills/{skill_id}", summary="更新已有技能（PATCH 语义）")
async def update_skill(skill_id: str, request: SkillUpdate):
    """
    就地更新指定技能（Phase 4 — 技能自修正核心端点）。

    只更新请求体中明确提供的字段（None 字段不修改），
    同步更新 SQLite SkillMetadata 表 + ChromaDB skills_vector 集合。

    - `script_content` 非空时会覆盖 entrypoint 指向的脚本文件。
    - 适用场景：Agent 发现技能内容过时/不准确时自主调用修正。
    """
    with Session(engine) as session:
        skill = session.exec(
            select(SkillMetadata).where(SkillMetadata.id == skill_id)
        ).first()
        if not skill:
            raise HTTPException(status_code=404, detail=f"Skill '{skill_id}' not found.")

        # 更新提供的字段
        if request.name is not None:
            skill.name = request.name
        if request.description is not None:
            skill.description = request.description
        if request.language is not None:
            skill.language = request.language
        if request.entrypoint is not None:
            skill.entrypoint = request.entrypoint
        if request.parameters_schema is not None:
            skill.parameters_schema = request.parameters_schema
        if request.tags is not None:
            skill.tags = request.tags
        if request.skill_type is not None:
            skill.skill_type = request.skill_type
        if request.knowledge_content is not None:
            skill.knowledge_content = request.knowledge_content

        # 覆写脚本文件（可选）
        if request.script_content is not None:
            script_path = os.path.join(
                "/app" if os.path.exists("/app") else ".", skill.entrypoint
            )
            os.makedirs(os.path.dirname(script_path), exist_ok=True)
            with open(script_path, "w", encoding="utf-8") as f:
                f.write(request.script_content)
            logger.info(f"Skill '{skill_id}' script updated at {script_path}")

        session.add(skill)
        session.commit()
        session.refresh(skill)

    # 同步更新 ChromaDB（upsert）
    from app.models.schemas import SkillManifest
    skill_manifest = SkillManifest(
        id=skill_id,
        name=skill.name,
        description=skill.description,
        language=skill.language,
        content=request.script_content or "",
    )
    await memory_retriever.add_skill(skill_manifest)
    tool_registry.invalidate_cache()
    logger.info(f"Skill '{skill_id}' updated.")
    return {"status": "updated", "skill_id": skill_id}


# ============================================================
# 代理 / 模型 / 知识库 / 错题集
# ============================================================

@router.get("/proxy/status", summary="查看代理健康状态")
async def get_proxy_status():
    """返回当前代理池的连接健康状态。"""
    return await proxy_manager.get_health_status()


@router.get("/models", summary="获取可用模型列表")
async def get_models():
    return {
        "available_models": model_router.available_models,
        "default_model": model_router.default_model,
    }


@router.get("/knowledge", summary="获取知识库列表")
async def get_knowledge():
    """返回根目录下的 Markdown 文档作为知识库（不含 error_ledger.md）。"""
    root_dir = "/app" if os.path.exists("/app/features.md") else "."
    knowledge_files = [
        f for f in os.listdir(root_dir)
        if f.endswith(".md") and f != "error_ledger.md"
    ]
    return {"knowledge_files": knowledge_files}


@router.get("/errors", summary="获取错题集")
async def get_errors():
    """返回 error_ledger.md 文件内容。"""
    error_file = (
        "/app/error_ledger.md"
        if os.path.exists("/app/error_ledger.md")
        else "error_ledger.md"
    )
    if os.path.exists(error_file):
        with open(error_file, "r", encoding="utf-8") as f:
            return {"content": f.read()}
    return {"content": "暂无错题集记录。"}


@router.post("/errors/ingest", summary="将 error_ledger.md 重新写入向量库")
async def ingest_errors():
    """
    重新解析 error_ledger.md 并以幂等方式写入 ChromaDB error_ledger_vector 集合。
    可在修改 error_ledger.md 后手动调用以同步最新内容。
    """
    error_file = (
        "/app/error_ledger.md"
        if os.path.exists("/app/error_ledger.md")
        else "error_ledger.md"
    )
    count = await memory_retriever.ingest_error_ledger(error_file)
    return {"status": "ok", "entries_ingested": count}


# ============================================================
# Phase 4 — PDF-to-Skills 流水线
# ============================================================

_PDF_UPLOAD_DIR = "/app/data/pdfs"


@router.post("/pdf/upload", summary="上传 PDF 文件", status_code=201)
async def upload_pdf(file: UploadFile = File(...)):
    """
    上传 PDF 文件到服务器，返回 doc_id 供后续流水线调用。

    - 自动计算 SHA-256 哈希，同一文件不重复入库（幂等）。
    - 使用 multipart/form-data 上传；前端用 `<input type="file">` 或 curl `-F` 发送。

    Response::

        {
            "doc_id": "uuid",
            "filename": "example.pdf",
            "file_path": "/app/data/pdfs/uuid.pdf",
            "page_count": 320,
            "file_hash": "sha256..."
        }
    """
    from app.services.pdf_processor import pdf_processor

    if not file.filename or not file.filename.lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="只支持 PDF 文件（.pdf 后缀）。")

    os.makedirs(_PDF_UPLOAD_DIR, exist_ok=True)

    # 临时写入磁盘
    tmp_path = os.path.join(_PDF_UPLOAD_DIR, f"tmp_{uuid.uuid4().hex}.pdf")
    try:
        content = await file.read()
        with open(tmp_path, "wb") as f:
            f.write(content)

        file_hash = pdf_processor.compute_file_hash(tmp_path)

        # 幂等检查：同一哈希是否已上传
        with Session(engine) as session:
            existing = session.exec(
                select(PDFDocument).where(PDFDocument.file_hash == file_hash)
            ).first()
        if existing:
            os.remove(tmp_path)
            logger.info(f"PDF already uploaded: {existing.id}")
            return {
                "doc_id": existing.id,
                "filename": existing.filename,
                "file_path": existing.file_path,
                "page_count": existing.page_count,
                "file_hash": existing.file_hash,
                "note": "File already uploaded, returning existing doc_id.",
            }

        # 正式存储
        doc_id = str(uuid.uuid4())
        final_path = os.path.join(_PDF_UPLOAD_DIR, f"{doc_id}.pdf")
        os.rename(tmp_path, final_path)

        # 快速获取页数（不阻塞）
        import asyncio as _asyncio
        loop = _asyncio.get_event_loop()
        page_count = await loop.run_in_executor(None, _count_pdf_pages, final_path)

        # 写入 PDFDocument
        doc = PDFDocument(
            id=doc_id,
            filename=file.filename,
            file_path=final_path,
            file_hash=file_hash,
            page_count=page_count,
            status="pending",
        )
        with Session(engine) as session:
            session.add(doc)
            session.commit()

        logger.info(f"PDF uploaded: {doc_id} ({file.filename}, {page_count} pages)")
        return {
            "doc_id": doc_id,
            "filename": file.filename,
            "file_path": final_path,
            "page_count": page_count,
            "file_hash": file_hash,
        }
    except Exception as e:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
        logger.error(f"PDF upload failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


def _count_pdf_pages(path: str) -> int:
    """同步快速统计 PDF 页数（run_in_executor 调用）。"""
    try:
        import pypdf
        with pypdf.PdfReader(path) as r:
            return len(r.pages)
    except Exception:
        return 0


@router.post("/pdf/to-skills", summary="触发 PDF→Skills 异步流水线")
async def pdf_to_skills(request: PDFToSkillsRequest):
    """
    触发 PDF-to-Skills 全流水线（异步，立即返回）。

    参数二选一：
    - `doc_id`  — 来自 `/pdf/upload` 返回的 doc_id（推荐）
    - `pdf_path` — 容器内 PDF 文件路径（直接指定）

    流水线在后台执行，通过 `GET /pdf/status/{doc_id}` 查询进度。

    Response::

        {"status": "accepted", "doc_id": "uuid"}
    """
    from app.services.pdf_processor import pdf_processor

    pdf_path: Optional[str] = None
    doc_id: Optional[str] = request.doc_id

    if doc_id:
        with Session(engine) as session:
            doc = session.exec(
                select(PDFDocument).where(PDFDocument.id == doc_id)
            ).first()
        if not doc:
            raise HTTPException(status_code=404, detail=f"PDFDocument '{doc_id}' not found.")
        pdf_path = doc.file_path
    elif request.pdf_path:
        pdf_path = request.pdf_path
        # 如果没有 doc_id，创建一个新的 PDFDocument 记录
        file_hash = pdf_processor.compute_file_hash(pdf_path)
        doc_id = str(uuid.uuid4())
        page_count = _count_pdf_pages(pdf_path)
        doc = PDFDocument(
            id=doc_id,
            filename=os.path.basename(pdf_path),
            file_path=pdf_path,
            file_hash=file_hash,
            page_count=page_count,
            status="pending",
        )
        with Session(engine) as session:
            session.add(doc)
            session.commit()
    else:
        raise HTTPException(status_code=400, detail="必须提供 doc_id 或 pdf_path 之一。")

    if not pdf_path or not os.path.exists(pdf_path):
        raise HTTPException(status_code=404, detail=f"PDF 文件不存在: {pdf_path}")

    # 标记为处理中
    with Session(engine) as session:
        doc = session.exec(select(PDFDocument).where(PDFDocument.id == doc_id)).first()
        if doc:
            doc.status = "processing"
            session.add(doc)
            session.commit()

    # 后台异步执行流水线
    async def _run_pipeline():
        try:
            result = await pdf_processor.pdf_to_skills(
                pdf_path=pdf_path,
                output_dir=request.output_dir,
                skill_prefix=request.skill_prefix,
                max_tokens_per_chunk=request.max_tokens_per_chunk,
                doc_id=doc_id,
            )
            logger.info(
                f"Pipeline completed for doc {doc_id}: "
                f"{result.skills_registered} skills registered."
            )
        except Exception as e:
            logger.error(f"Pipeline failed for doc {doc_id}: {e}")
            with Session(engine) as session:
                d = session.exec(select(PDFDocument).where(PDFDocument.id == doc_id)).first()
                if d:
                    d.status = "failed"
                    d.error_msg = str(e)
                    session.add(d)
                    session.commit()

    _track_task(asyncio.create_task(_run_pipeline()))
    return {"status": "accepted", "doc_id": doc_id}


@router.get("/pdf/status/{doc_id}", summary="查询 PDF 流水线处理进度")
async def get_pdf_status(doc_id: str):
    """
    查询指定 PDFDocument 的流水线处理状态与结果摘要。

    Status 值说明：
    - `pending`    — 已上传，等待触发
    - `processing` — 流水线运行中
    - `completed`  — 已完成，可查看 skills_generated
    - `failed`     — 流水线失败，见 error_msg
    """
    with Session(engine) as session:
        doc = session.exec(
            select(PDFDocument).where(PDFDocument.id == doc_id)
        ).first()
    if not doc:
        raise HTTPException(status_code=404, detail=f"PDFDocument '{doc_id}' not found.")
    return doc


@router.get("/pdf", summary="列出所有已上传的 PDF 文档")
async def list_pdfs():
    """返回所有 PDFDocument 记录（用于前端管理界面）。"""
    with Session(engine) as session:
        docs = session.exec(select(PDFDocument)).all()
    return {"documents": docs}


# ============================================================
# Phase 5 — ms-agent 深度能力
# ============================================================

@router.post(
    "/research/deep",
    response_model=DeepResearchResponse,
    summary="[ms-agent] 触发 deep_research v2 深度研究任务",
    tags=["msagent"],
)
async def start_deep_research(request: DeepResearchRequest):
    """
    启动一个 **deep_research v2** 深度研究任务（异步 fire-and-forget）。

    工作流（ms-agent 内部）：
    1. ResearcherAgent 制定研究计划
    2. SearcherAgent 并行检索（arxiv / EXA / SerpAPI）
    3. ReporterAgent 汇总证据，生成结构化报告

    产物文件（容器内 `/app/data/outputs/research/{task_id}/`）：
    - `final_report.md`  — 最终研究报告（Markdown）
    - `plan.json`        — 研究计划
    - `evidence/`        — 原始证据片段
    - `reports/`         — 中间分析报告

    立即返回 `task_id`，通过 `GET /research/{task_id}` 轮询状态和读取报告。

    > 注意：任务运行时间通常 5-30 分钟，取决于研究深度和搜索引擎可用性。
    """
    try:
        result = await ms_agent_service.run_deep_research(
            query=request.query,
            model=request.model,
            exa_api_key=request.exa_api_key,
            serpapi_api_key=request.serpapi_api_key,
            max_rounds=request.max_rounds,
        )
        return DeepResearchResponse(**result)
    except Exception as e:
        logger.error(f"start_deep_research error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get(
    "/research/{task_id}",
    response_model=MSAgentTaskStatus,
    summary="[ms-agent] 查询深度研究任务状态 + 读取报告",
    tags=["msagent"],
)
async def get_research_status(task_id: str):
    """
    查询深度研究任务的当前状态，并在完成后返回 `report` 字段（full Markdown）。

    `status` 枚举：
    - `pending`    — 目录已建，进程尚未产出文件
    - `running`    — 进程运行中
    - `completed`  — 正常结束（returncode=0）
    - `failed`     — 异常退出（见 `stderr_tail` 了解原因）
    - `not_found`  — 任务 ID 不存在
    """
    status = ms_agent_service.get_task_status("research", task_id)
    return MSAgentTaskStatus(**status)


@router.get(
    "/research",
    summary="[ms-agent] 列出所有深度研究任务",
    tags=["msagent"],
)
async def list_research_tasks():
    """返回所有深度研究任务的摘要列表（按创建时间倒序）。"""
    tasks = ms_agent_service.list_tasks("research")
    return {"tasks": tasks, "total": len(tasks)}


@router.post(
    "/code/generate",
    response_model=CodeGenResponse,
    summary="[ms-agent] 触发 code_genesis 代码生成任务",
    tags=["msagent"],
)
async def start_code_genesis(request: CodeGenRequest):
    """
    启动一个 **code_genesis** 复杂代码生成任务（异步 fire-and-forget）。

    工作流（7 阶段 DAG）：
    > user_story → architect → file_design → file_order → install → coding → refine

    输出（容器内 `/app/data/outputs/code/{task_id}/output/`）：
    - 完整可运行项目目录（含依赖安装脚本、README.md 等）

    立即返回 `task_id`，通过 `GET /code/{task_id}` 轮询状态。

    适用场景：
    - 多文件、多模块的复杂系统开发
    - 需要自动设计文件结构的项目
    - 需要依赖安装和代码精炼的完整工作流

    > 注意：任务运行时间通常 10-60 分钟，取决于项目复杂度。
    """
    try:
        result = await ms_agent_service.run_code_genesis(
            query=request.query,
            model=request.model,
        )
        return CodeGenResponse(**result)
    except Exception as e:
        logger.error(f"start_code_genesis error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get(
    "/code/{task_id}",
    response_model=MSAgentTaskStatus,
    summary="[ms-agent] 查询代码生成任务状态 + 产物列表",
    tags=["msagent"],
)
async def get_code_status(task_id: str):
    """
    查询代码生成任务的当前状态，在完成后通过 `output_files` 返回所有产物文件路径。
    """
    status = ms_agent_service.get_task_status("code", task_id)
    return MSAgentTaskStatus(**status)


@router.get(
    "/code",
    summary="[ms-agent] 列出所有代码生成任务",
    tags=["msagent"],
)
async def list_code_tasks():
    """返回所有代码生成任务的摘要列表（按创建时间倒序）。"""
    tasks = ms_agent_service.list_tasks("code")
    return {"tasks": tasks, "total": len(tasks)}
